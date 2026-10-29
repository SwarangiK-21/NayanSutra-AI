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
    jurisdiction: str = ""
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
               exclude: frozenset[str] = frozenset(), jurisdiction: str | None = None) -> tuple[str, list]:
    """SQL filter on documents alias d, mirroring VectorIndex.search filters."""
    clauses, args = ["d.status='ready'"], []
    if jurisdiction:   # central law + that state's own acts; other states' acts are left out
        clauses.append("(d.collection <> 'bare_act' OR d.jurisdiction IN ('', 'Central', ?))")
        args.append(jurisdiction)
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


# Words in a large share of all passages (every Supreme Court catalogue entry says "Supreme Court of India", "case",
# "bench", "citation"…). As keyword terms they match tens of thousands of rows and make ranking take seconds while
# telling the search nothing; meaning search and the re-ranker still use them. From ts_stat on the library.
_FTS_COMMON = set("""case cases court courts india indian author date dated official number decision decisions judge
judges judgment judgments judgement decided supreme summary bench strength versus citation citations cnr abridged
coram headnote headnotes insc scr outcome appeal appeals appellate appellant appellants civil state states allowed
held part dismissed chapter code high person persons law laws legal""".split())


def _fts_tokens(q: str) -> list[str]:
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", q.lower()) if t not in _STOP and t not in _FTS_COMMON and len(t) > 1]
    return list(dict.fromkeys(toks))[:14]


def fts_query(q: str) -> str:
    """Keyword query in the active engine's syntax: FTS5 ('"a" OR "b"') or Postgres tsquery ('a | b').
    Tokens are reduced to [a-z0-9], so user input can never inject query syntax."""
    toks = _fts_tokens(q)
    if db.backend() == "postgres":
        return " | ".join(toks)
    return " OR ".join(f'"{t}"' for t in toks)


_NOT_NAMES = {"what", "which", "when", "where", "who", "whom", "how", "why", "can", "could", "should", "would", "does",
              "did", "is", "are", "the", "tell", "explain", "give", "list", "please", "bharatiya", "nyaya", "nagarik",
              "suraksha", "sanhita", "sakshya", "adhiniyam", "penal", "constitution", "article", "union", "government",
              "high", "district", "sessions", "hindi", "english", "marathi", "under", "section"}


def proper_names(text: str) -> list[str]:
    """Capitalised words that look like party or place names (e.g. "Kasab", "Puttaswamy", "Maneka"), lower-cased;
    question words, act names and near-universal words are left out."""
    words = re.findall(r"\b[A-Z][a-z]{3,}\b", text or "")
    out = [w.lower() for w in words]
    return list(dict.fromkeys(w for w in out if w not in _NOT_NAMES and w not in _FTS_COMMON and w not in _STOP))[:4]


PAIR_TERMS = 7   # Postgres: with 3+ keywords a passage must contain at least two of the first 7 (or a name)


def pg_filter(q: str) -> str:
    """Postgres match condition. "Any word" over ~60,000 passages matches most of them, and ranking that many takes
    seconds on a small database; "at least two words" (or a capitalised name on its own) uses the index instead."""
    toks = _fts_tokens(q)
    if len(toks) < 3:
        return " | ".join(toks)
    names = [t for t in dict.fromkeys(w.lower() for w in re.findall(r"(?<!^)(?<![.?!]\s)\b[A-Z][a-z]{3,}", q))
             if t in toks]   # proper nouns mid-sentence: party names, places
    head = toks[:PAIR_TERMS]
    pairs = [f"({a} & {b})" for i, a in enumerate(head) for b in head[i + 1:]]
    return " | ".join(pairs + names)


def _fts_sql(where: str) -> str:
    """Ranked keyword match; the header is weighted above the body (FTS5 bm25 3:1, Postgres weights A > B)."""
    if db.backend() == "postgres":   # filter with the index-friendly condition, rank with the plain one (ts_rank)
        return (f"SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id, to_tsquery('english', ?) f, "
                f"to_tsquery('english', ?) q WHERE c.fts @@ f AND {where} ORDER BY ts_rank(c.fts, q) DESC LIMIT ?")
    return (f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid JOIN documents d ON d.id=c.doc_id "
            f"WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts, 3.0, 1.0) LIMIT ?")


def keyword_ids(q: str, where: str, args: list, k: int) -> list[int]:
    match = fts_query(q)
    if not match:
        return []
    params = [pg_filter(q), match, *args, k] if db.backend() == "postgres" else [match, *args, k]
    try:
        return [r[0] for r in db.conn().execute(_fts_sql(where), params)]
    except Exception as exc:
        log.warning("Keyword query failed (%s): %s", match, exc)
        return []


def pg_keyword_ids(q: str, k: int, collections: set[str], case_ids: list[str], doc_ids: set[str] | None = None,
                   jurisdiction: str | None = None) -> list[int]:
    """Postgres keyword search through private.fts_chunk_ids (migration 09): under row-level security Postgres cannot
    use the full-text index directly, so the function runs the match with the index and applies the same access rule
    itself (library for everyone; case documents only for cases the caller can access)."""
    match = fts_query(q)
    if not match or not collections:
        return []
    try:
        rows = db.conn().execute("SELECT id FROM private.fts_chunk_ids(?, ?, ?, ?, ?, ?, ?)",
                                 (pg_filter(q), match, sorted(collections), list(case_ids),
                                  sorted(doc_ids) if doc_ids else None, jurisdiction or None, k)).fetchall()
        return [r["id"] for r in rows]
    except Exception as exc:
        log.warning("Keyword query failed (%s): %s", match, exc)
        return []


def lexical(q: str, k: int, scope: str, case_id: str | None, doc_ids: set[str] | None,
            exclude: frozenset[str] = frozenset(), jurisdiction: str | None = None) -> list[int]:
    if db.backend() == "postgres":
        cases = [case_id] if case_id and scope in ("all", "case") else []
        return pg_keyword_ids(q, k, _collections(scope, exclude), cases, doc_ids, jurisdiction)
    where, args = _scope_sql(scope, case_id, doc_ids, exclude, jurisdiction)
    return keyword_ids(q, where, args, k)


def exact(refs: list[citations.Ref], scope: str, case_id: str | None, doc_ids: set[str] | None,
          exclude: frozenset[str] = frozenset({"draft"}), jurisdiction: str | None = None) -> list[int]:
    """Chunks whose provision key matches a cited section/article/rule (in the named act if given)."""
    if not refs:
        return []
    where, args = _scope_sql(scope, case_id, doc_ids, exclude, jurisdiction)
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


def other_state_doc_ids(jurisdiction: str | None) -> frozenset[str]:
    """Statutes of states / UTs other than `jurisdiction` (nothing is excluded when no state is chosen)."""
    if not jurisdiction:
        return frozenset()
    return frozenset(r["id"] for r in db.conn().execute(
        "SELECT id FROM documents WHERE collection='bare_act' AND jurisdiction NOT IN ('', 'Central', ?)",
        (jurisdiction,)))


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
        f"SELECT c.id, c.doc_id, c.header, c.text, c.label, c.section, c.page_start, c.page_end, c.title AS chunk_title, "
        f"d.title, d.collection, d.doc_type, d.jurisdiction FROM chunks c JOIN documents d ON d.id=c.doc_id "
        f"WHERE c.id IN ({','.join('?' * len(ids))}) AND d.status='ready'", ids).fetchall()   # never superseded text
    by_id = {r["id"]: r for r in rows}
    # A Supreme Court catalogue entry is titled by its own case, not by the catalogue that holds it.
    return [Hit(chunk_id=r["id"], doc_id=r["doc_id"],
                title=(r["chunk_title"] or r["title"]) if r["doc_type"] == "catalogue" else r["title"],
                collection=r["collection"],
                doc_type=r["doc_type"], header=r["header"], text=r["text"], label=r["label"], section=r["section"],
                page_start=r["page_start"], page_end=r["page_end"], jurisdiction=r["jurisdiction"] or "")
            for i in ids if (r := by_id.get(i))]


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


CATALOGUE_PER_ANSWER = 4   # judgments from the Supreme Court catalogue (one entry each) per answer
COMPLETE_TOP = 3           # statutory hits whose split provision is completed from its other parts
COMPLETE_MAX_CHARS = 8000
COUNTERPART_MIN_COS = 0.80   # an IPC section and its BNS replacement read almost alike; unrelated ones do not
# Old and new criminal codes: a section of one is paired with the most similar section of the other.
CODE_PAIRS = (("Bharatiya Nyaya Sanhita", "Indian Penal Code"),)


def complete_sections(hits: list[Hit], top: int = COMPLETE_TOP, max_chars: int = COMPLETE_MAX_CHARS) -> list[Hit]:
    """A long provision is split into several passages; give the top statutory hits their whole text (all parts of
    the same section, in order), so a list such as the grounds of divorce is not cut off half-way."""
    done = 0
    for h in hits:
        if done >= top:
            break
        if h.collection != "bare_act" or not h.section:
            continue
        done += 1
        parts = db.conn().execute("SELECT text FROM chunks WHERE doc_id=? AND section=? ORDER BY ord",
                                  (h.doc_id, h.section)).fetchall()
        if len(parts) < 2:
            continue
        full = "\n".join(p["text"] for p in parts)
        if len(full) > len(h.text):
            h.text = full[:max_chars] + ("\n…" if len(full) > max_chars else "")
    return hits


def add_counterparts(hits: list[Hit], limit: int = 2) -> list[Hit]:
    """For criminal-code sections among the hits, add the corresponding section of the other code (IPC <-> BNS),
    found by text similarity, so an answer about the old and new law has both provisions in its sources."""
    from .ingest import EMBED_CHARS
    out, added = list(hits), 0
    have = {(h.doc_id, h.section or h.chunk_id) for h in hits}   # a section already present (any of its parts)
    for h in hits:
        if added >= limit or h.collection != "bare_act":
            continue
        other = next((b if a in h.title else a for a, b in CODE_PAIRS if a in h.title or b in h.title), None)
        if not other:
            continue
        target = act_doc_ids([other])
        if not target:
            continue
        vec = embeddings.embed_passages([f"{h.header}\n{h.text}"[:EMBED_CHARS]])[0]
        best = index.search(vec, 1, doc_ids=target, include_case_docs=False)
        if best and best[0][1] >= COUNTERPART_MIN_COS:
            for c in load_hits([best[0][0]]):
                if (c.doc_id, c.section or c.chunk_id) in have:
                    continue
                c.score = h.score - 0.01
                out.append(c)
                have.add((c.doc_id, c.section or c.chunk_id))
                added += 1
    return out

# Everyday words → the wording statutes actually use. Bare acts never say "FIR" or "cheque bounce", so without this a
# plain question misses the provision that answers it. Used only to widen the search and the re-ranking query.
LAY_TERMS: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), t) for p, t in [
    (r"\bf\.?i\.?r\b|first\s+information\s+report|एफआईआर",
     "information relating to the commission of a cognizable offence given to an officer in charge of a police station"),
    (r"police\s+(?:refus\w*|won'?t|will\s+not|did\s+not|didn'?t|not)\s+(?:register|record|file|lodge)|"
     r"police\s+mana|refus\w*\s+to\s+(?:register|record)",
     "officer in charge of a police station refuses to record information; substance sent in writing to the "
     "Superintendent of Police; complaint to Magistrate"),
    (r"anticipatory\s+bail|pre-?arrest\s+bail", "direction for grant of bail to person apprehending arrest"),
    (r"\bbail\b|जमानत", "when bail may be taken in case of non-bailable offence; release on bail"),
    # "check" too: the planner sometimes writes the American spelling ("bounced check"); either word order.
    (r"(?:cheque|check)s?\s+(?:bounc\w*|dishono\w*|return\w*)|bounc\w*\s+(?:cheque|check)|"
     r"dishono\w*\s+(?:of\s+)?(?:a\s+)?(?:cheque|check)|चेक\s*(?:का\s+)?(?:बाउंस|बाउन्स|अनादर)",
     "dishonour of cheque for insufficiency of funds in the account"),
    (r"charge-?\s?sheet", "report of police officer on completion of investigation"),
    (r"\bdivorce\b", "dissolution of marriage by a decree of divorce"),
    (r"\bmaintenance\b|\balimony\b", "order for maintenance of wives, children and parents"),
    (r"cyber\s*(?:attack|crime|fraud)|\bhack\w*|ransomware|malware|data\s+breach",
     "computer related offences; unauthorised access to computer resource; damage to computer system; "
     "cyber terrorism"),
    (r"\bterror\w*|\b26/11\b", "terrorist act; waging war against the Government of India; unlawful activities"),
    (r"\bdowry\b", "dowry death; cruelty by husband or relatives of husband"),
    (r"\bdefam\w*", "defamation; imputation harming reputation"),
    (r"consumer\s+(?:complaint|forum|court|commission|case)|defective\s+(?:goods|product)|deficiency\s+in\s+service",
     "jurisdiction of the District Commission: value of goods or services paid as consideration; complaint "
     "relating to defect in goods or deficiency in service"),
]]


def expand_lay_terms(text: str) -> list[str]:
    return list(dict.fromkeys(t for pat, t in LAY_TERMS if pat.search(text or "")))[:3]


def search(query: str, *, scope: str = "all", case_id: str | None = None, doc_ids: set[str] | None = None,
           k: int = config.CONTEXT_PASSAGES, extra_queries: list[str] | None = None,
           exclude: frozenset[str] = frozenset({"draft"}), hint: str = "",
           context: str = "", jurisdiction: str | None = None) -> tuple[list[Hit], dict]:
    """Return (hits, diagnostics). diagnostics['confidence'] is the top reranker logit (or None).

    Draft templates are excluded by default (they are model text, not authority);
    the drafting flow searches them explicitly with exclude=frozenset().
    `jurisdiction` (a state / UT) limits statutes to central law plus that state's acts."""
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
    lay = expand_lay_terms(f"{query} {hint} {' '.join(extra_queries or [])}")
    # Re-rank on what the user actually asked (when it is English) plus the statutory wording: a planner rewrite such
    # as "procedure to file an FIR under the BNSS" otherwise pulls in every generic "procedure" section.
    base = hint if hint and hint.isascii() and len(hint) < 400 else query
    rq = f"{base} ({'; '.join(lay)})" if lay else query
    colls = _collections(scope, exclude)
    inc_case = scope in ("all", "case")
    jur = jurisdiction or None
    skip = other_state_doc_ids(jur)

    dense = [cid for cid, _ in index.search(qv, config.CANDIDATES, case_id=case_id, collections=colls,
                                             doc_ids=doc_ids, include_case_docs=inc_case, skip_doc_ids=skip)]
    lists: list[tuple[list[int], float]] = [
        (dense, 1.0), (lexical(query, config.CANDIDATES, scope, case_id, doc_ids, exclude, jur), 1.0)]
    for phrase in lay:
        lists.append(([cid for cid, _ in index.search(embeddings.embed_query(phrase), 20, case_id=case_id,
                                                      collections=colls, doc_ids=doc_ids, include_case_docs=inc_case,
                                                      skip_doc_ids=skip)], 2.0))   # as strong as a named act
        lists.append((lexical(phrase, 20, scope, case_id, doc_ids, exclude, jur), 1.0))
    names = proper_names(f"{query} {hint}")
    if names:   # "Kasab", "Puttaswamy": rare, decisive words that common ones ("death", "sentence") would drown out
        lists.append((lexical(" ".join(names), 20, scope, case_id, doc_ids, exclude, jur), 2.0))
    exact_ids = exact(refs, scope, case_id, doc_ids, jurisdiction=jur)
    if exact_ids:
        lists.append((exact_ids, 3.0))
    ctx_ids = exact(ctx_refs, scope, case_id, doc_ids, jurisdiction=jur) if ctx_refs else []
    if ctx_ids:
        lists.append((ctx_ids, 1.5))
    focus: set[str] = set()
    if acts and scope != "case":
        focus = act_doc_ids(acts) - skip
        if doc_ids:
            focus &= doc_ids
        if focus:
            lists.append(([cid for cid, _ in index.search(qv, 20, doc_ids=focus, include_case_docs=False)], 2.0))
            lists.append((lexical(query, 20, "library", None, focus), 1.0))
    for q2 in extra_queries or []:
        lists.append(([cid for cid, _ in index.search(embeddings.embed_query(q2), 20, case_id=case_id,
                                                      collections=colls, doc_ids=doc_ids,
                                                      include_case_docs=inc_case, skip_doc_ids=skip)], 0.7))

    fused = rrf(lists)[: config.RERANK_POOL]
    hits = load_hits(fused)
    exact_set, ctx_set = set(exact_ids), set(ctx_ids)
    for h in hits:
        h.exact = h.chunk_id in exact_set

    q_terms = _terms(rq + " " + hint)
    scores = embeddings.rerank(rq, [h.header + "\n" + _best_window(h.text, q_terms) for h in hits])
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
        # The Supreme Court catalogue is one document holding a separate entry per judgment: allow a few more.
        cap = CATALOGUE_PER_ANSWER if h.doc_type == "catalogue" else config.MAX_PER_DOC
        if not single_doc and per_doc.get(h.doc_id, 0) >= cap:
            continue
        if used + len(h.text) > config.CONTEXT_CHAR_BUDGET and picked:
            continue
        picked.append(h)
        used += len(h.text)
        per_doc[h.doc_id] = per_doc.get(h.doc_id, 0) + 1
    diag = {"refs": [r.key for r in refs], "acts": acts, "candidates": len(fused), "confidence": confidence,
            "exact_hits": len(exact_ids), "indexed": len(index), "jurisdiction": jur or "all"}
    return picked, diag
