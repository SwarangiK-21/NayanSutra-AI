"""Hybrid retrieval: exact citation lookup + dense vectors + BM25 (SQLite FTS5),
fused with weighted Reciprocal Rank Fusion, then a cross-encoder rerank.

Why each part matters for law:
* exact lookup — "Section 138 NI Act" must return s.138 of the NI Act, always;
* BM25        — defined terms, case names and rare words are lexical;
* dense       — paraphrased questions ("can I get my deposit back if…");
* reranker    — a cross-encoder reads question + passage together and is far
                better than either retriever at deciding what actually answers it;
                its score also tells the agent when evidence is too weak.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import citations, config, db, embeddings
from .vectorindex import index

log = logging.getLogger(__name__)

LIBRARY = {"bare_act", "judgment", "draft"}
_STOP = set("""a an and are as at be by can could do does for from had has have how i if in into is it its
may me my of on or our shall should so that the their them then there these this to under upon was what when
where which who whom why will with would you your section sections act article rule order""".split())


@dataclass
class Hit:
    chunk_id: int
    doc_id: str
    title: str
    collection: str
    doc_type: str
    header: str
    text: str
    label: str | None
    section: str | None
    page_start: int | None
    page_end: int | None
    score: float = 0.0
    exact: bool = False
    extra: dict = field(default_factory=dict)

    def citation(self) -> str:
        parts = [self.title]
        if self.label and self.collection != "draft":
            parts.append(self.label)
        if self.page_start:
            parts.append(f"p. {self.page_start}" if self.page_start == self.page_end or not self.page_end
                         else f"pp. {self.page_start}–{self.page_end}")
        return ", ".join(parts)


def _scope_sql(scope: str, case_id: str | None, doc_ids: set[str] | None,
               exclude: frozenset[str] = frozenset()) -> tuple[str, list]:
    """SQL filter on documents alias d, mirroring VectorIndex.search filters."""
    clauses, args = ["d.status='ready'"], []
    if exclude:
        clauses.append(f"d.collection NOT IN ({','.join('?' * len(exclude))})")
        args += sorted(exclude)
    if doc_ids:
        clauses.append(f"d.id IN ({','.join('?' * len(doc_ids))})")
        args += list(doc_ids)
    if scope == "case":
        clauses.append("d.collection='case' AND d.case_id=?")
        args.append(case_id or "")
    elif scope == "library":
        clauses.append("d.collection IN ('bare_act','judgment','draft')")
    else:
        clauses.append("(d.collection IN ('bare_act','judgment','draft') OR (d.collection='case' AND d.case_id=?))")
        args.append(case_id or "")
    return " AND ".join(clauses), args


def _collections(scope: str, exclude: frozenset[str] = frozenset()) -> set[str] | None:
    base = {"case"} if scope == "case" else set(LIBRARY) if scope == "library" else {"case", *LIBRARY}
    return base - exclude


def _fts_tokens(q: str) -> list[str]:
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", q.lower()) if t not in _STOP and len(t) > 1]
    return list(dict.fromkeys(toks))[:14]


def fts_query(q: str) -> str:
    """Keyword query in the active engine's syntax: FTS5 ('"a" OR "b"') or Postgres tsquery ('a | b').
    Tokens are reduced to [a-z0-9], so user input can never inject query syntax."""
    toks = _fts_tokens(q)
    if db.backend() == "postgres":
        return " | ".join(toks)
    return " OR ".join(f'"{t}"' for t in toks)


def _fts_sql(where: str) -> str:
    """Ranked keyword match; the header is weighted above the body (FTS5 bm25 3:1, Postgres weights A > B)."""
    if db.backend() == "postgres":
        return (f"SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id, to_tsquery('english', ?) q "
                f"WHERE c.fts @@ q AND {where} ORDER BY ts_rank_cd(c.fts, q) DESC LIMIT ?")
    return (f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid JOIN documents d ON d.id=c.doc_id "
            f"WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts, 3.0, 1.0) LIMIT ?")


def keyword_ids(q: str, where: str, args: list, k: int) -> list[int]:
    match = fts_query(q)
    if not match:
        return []
    try:
        return [r[0] for r in db.conn().execute(_fts_sql(where), [match, *args, k])]
    except Exception as exc:
        log.warning("Keyword query failed (%s): %s", match, exc)
        return []


def lexical(q: str, k: int, scope: str, case_id: str | None, doc_ids: set[str] | None,
            exclude: frozenset[str] = frozenset()) -> list[int]:
    where, args = _scope_sql(scope, case_id, doc_ids, exclude)
    return keyword_ids(q, where, args, k)


def exact(refs: list[citations.Ref], scope: str, case_id: str | None, doc_ids: set[str] | None,
          exclude: frozenset[str] = frozenset({"draft"})) -> list[int]:
    """Chunks whose provision key matches a cited section/article/rule (in the named act if given)."""
    if not refs:
        return []
    where, args = _scope_sql(scope, case_id, doc_ids, exclude)
    out: list[int] = []
    for r in refs[:8]:
        sql = f"SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.section=? AND {where}"
        params = [r.key, *args]
        if r.act:
            sql += " AND d.title LIKE ?"
            params.append(f"%{r.act}%")
        sql += " ORDER BY d.collection != 'bare_act', c.ord LIMIT 6"
        out += [row[0] for row in db.conn().execute(sql, params)]
    return list(dict.fromkeys(out))


def act_doc_ids(acts: list[str]) -> set[str]:
    ids: set[str] = set()
    for a in acts:
        ids |= {r[0] for r in db.conn().execute(
            "SELECT id FROM documents WHERE collection='bare_act' AND title LIKE ?", (f"%{a}%",))}
    return ids


def _terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 2}


def _best_window(text: str, q_terms: set[str], size: int = config.RERANK_CHARS) -> str:
    """The cross-encoder only reads ~size chars (for speed), so give it the window of the
    passage that overlaps the question most, not blindly the opening."""
    if len(text) <= size or not q_terms:
        return text[:size]
    step = size // 2
    best, best_score = 0, -1
    for start in range(0, len(text) - step, step):
        score = len(q_terms & _terms(text[start:start + size]))
        if score > best_score:
            best, best_score = start, score
    return text[best:best + size]


def rrf(lists: list[tuple[list[int], float]], k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for ids, w in lists:
        for rank, cid in enumerate(ids):
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank + 1)
    return sorted(scores, key=scores.__getitem__, reverse=True)


def load_hits(ids: list[int]) -> list[Hit]:
    if not ids:
        return []
    rows = db.conn().execute(
        f"SELECT c.id, c.doc_id, c.header, c.text, c.label, c.section, c.page_start, c.page_end, "
        f"d.title, d.collection, d.doc_type FROM chunks c JOIN documents d ON d.id=c.doc_id "
        f"WHERE c.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [Hit(chunk_id=r["id"], doc_id=r["doc_id"], title=r["title"], collection=r["collection"],
                doc_type=r["doc_type"], header=r["header"], text=r["text"], label=r["label"], section=r["section"],
                page_start=r["page_start"], page_end=r["page_end"]) for i in ids if (r := by_id.get(i))]


def _merge_parts(hits: list[Hit]) -> list[Hit]:
    """Fold split parts of the same provision into one passage so the LLM sees it whole."""
    out: list[Hit] = []
    seen: dict[tuple[str, str], Hit] = {}
    for h in hits:
        key = (h.doc_id, h.section) if h.section else None
        if key and key in seen:
            first = seen[key]
            if len(first.text) + len(h.text) < 3200 and h.text not in first.text:
                first.text += "\n…\n" + h.text
                first.page_start = min(first.page_start or h.page_start or 0, h.page_start or first.page_start or 0)
                first.page_end = max(first.page_end or 0, h.page_end or 0)
            continue
        if key:
            seen[key] = h
        out.append(h)
    return out


def search(query: str, *, scope: str = "all", case_id: str | None = None, doc_ids: set[str] | None = None,
           k: int = config.CONTEXT_PASSAGES, extra_queries: list[str] | None = None,
           exclude: frozenset[str] = frozenset({"draft"}), hint: str = "",
           context: str = "") -> tuple[list[Hit], dict]:
    """Return (hits, diagnostics). diagnostics['confidence'] is the top reranker logit (or None).

    Draft templates are excluded by default (they are model text, not authority);
    the drafting flow searches them explicitly with exclude=frozenset()."""
    if scope == "case" and not case_id:
        return [], {"reason": "no case selected", "confidence": None, "exact_hits": 0}
    index.wait_ready()
    # `hint` is the user's own wording: provisions and acts named there win over the planner's rewrite.
    # Only references tied to an act that is actually in the library are trusted for exact lookup —
    # a bare "Section 12" (or a citation the planner guessed) would otherwise match every act.
    raw_refs = citations.parse_refs(hint) + citations.parse_refs(query) if hint else citations.parse_refs(query)
    refs = [r for r in dict.fromkeys(raw_refs) if r.act and act_doc_ids([r.act])]
    acts = citations.mentioned_acts(hint) or citations.mentioned_acts(query)
    # `context` = provisions noted on the case file: a moderate signal, not a guarantee.
    ctx_refs = [r for r in citations.parse_refs(context) if r.act and r not in refs] if context else []
    qv = embeddings.embed_query(query)
    colls = _collections(scope, exclude)
    inc_case = scope in ("all", "case")

    dense = [cid for cid, _ in index.search(qv, config.CANDIDATES, case_id=case_id, collections=colls,
                                             doc_ids=doc_ids, include_case_docs=inc_case)]
    lists: list[tuple[list[int], float]] = [
        (dense, 1.0), (lexical(query, config.CANDIDATES, scope, case_id, doc_ids, exclude), 1.0)]
    exact_ids = exact(refs, scope, case_id, doc_ids)
    if exact_ids:
        lists.append((exact_ids, 3.0))
    ctx_ids = exact(ctx_refs, scope, case_id, doc_ids) if ctx_refs else []
    if ctx_ids:
        lists.append((ctx_ids, 1.5))
    focus: set[str] = set()
    if acts and scope != "case":
        focus = act_doc_ids(acts)
        if doc_ids:
            focus &= doc_ids
        if focus:
            lists.append(([cid for cid, _ in index.search(qv, 20, doc_ids=focus, include_case_docs=False)], 2.0))
            lists.append((lexical(query, 20, "library", None, focus), 1.0))
    for q2 in extra_queries or []:
        lists.append(([cid for cid, _ in index.search(embeddings.embed_query(q2), 20, case_id=case_id,
                                                      collections=colls, doc_ids=doc_ids,
                                                      include_case_docs=inc_case)], 0.7))

    fused = rrf(lists)[: config.RERANK_POOL]
    hits = load_hits(fused)
    exact_set, ctx_set = set(exact_ids), set(ctx_ids)
    for h in hits:
        h.exact = h.chunk_id in exact_set

    q_terms = _terms(query + " " + hint)
    scores = embeddings.rerank(query, [h.header + "\n" + _best_window(h.text, q_terms) for h in hits])
    if scores is not None:
        n = max(1, len(hits))
        for rank, (h, s) in enumerate(zip(hits, scores)):
            # Blend: the cross-encoder (web-trained) under-scores statutory language, so passages
            # that several retrievers agreed on (high fused rank) keep part of their credit.
            s += 3.0 * (1 - rank / n)
            # An explicitly cited provision always makes the cut; a named act is preferred.
            h.score = (s + (4.0 if h.exact else 0.0) + (2.5 if h.doc_id in focus else 0.0)
                       + (3.0 if h.chunk_id in ctx_set else 0.0))
        hits.sort(key=lambda h: h.score, reverse=True)
        confidence = max(scores) if scores else None
    else:
        for rank, h in enumerate(hits):
            h.score = 1.0 / (rank + 1)
        confidence = None

    hits = _merge_parts(hits)
    # When a case is open, its best-matching document passages always get seats next to the law.
    if scope == "all" and case_id:
        good_case = [h for h in hits if h.collection == "case" and (scores is None or h.score >= config.RELEVANCE_FLOOR)][:3]
        hits = good_case + [h for h in hits if h not in good_case]
    # Diversity cap per document, then the context budget.
    picked, used, per_doc = [], 0, {}
    single_doc = bool(doc_ids) and len(doc_ids) == 1
    for h in hits:
        if len(picked) >= k:
            break
        if not single_doc and per_doc.get(h.doc_id, 0) >= config.MAX_PER_DOC:
            continue
        if used + len(h.text) > config.CONTEXT_CHAR_BUDGET and picked:
            continue
        picked.append(h)
        used += len(h.text)
        per_doc[h.doc_id] = per_doc.get(h.doc_id, 0) + 1
    diag = {"refs": [r.key for r in refs], "acts": acts, "candidates": len(fused), "confidence": confidence,
            "exact_hits": len(exact_ids), "indexed": len(index)}
    return picked, diag
