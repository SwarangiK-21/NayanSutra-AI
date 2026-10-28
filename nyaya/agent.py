"""The agentic RAG loop.

    plan ─┬─ research ─────► retrieve → rerank ─┬─ strong ─► generate (stream) → verify
          │                                     └─ weak ──► rewrite → retrieve ─┬─ strong ─► …
          │                                                                     └─ weak ──► abstain
          ├─ drafting ─────► template lookup + law retrieval → generate draft (stream)
          ├─ analyze_document ► whole document (or map-reduce if huge) → stream analysis
          ├─ calculation ──► deterministic limitation tool (no LLM arithmetic)
          ├─ time ─────────► clock (server clock cross-checked against network time; no LLM)
          ├─ news ─────────► LiveLaw feed (dated) + live web search → answer with publication dates
          ├─ out_of_scope ─► fixed redirect to legal help (coding, maths, …); a legal angle overrides it
          ├─ clarify ──────► one short question back
          └─ chitchat ─────► short reply

Research falls back to a live web search (official and legal sources ranked first) when the local library has no
reliable passage, and adds web sources when the planner says the question is about a specific real event, case or
person the statute library cannot contain (e.g. the 26/11 attacks and the Kasab trial). Only the redacted search query
is sent to the web — never case documents — and "case documents only" scope never searches the web.

Each step yields events that the server streams to the browser (Server-Sent Events),
so the advocate sees progress immediately and tokens as they are generated.

Guardrails (guardrails.py) wrap every step: the request is normalised and screened before the model sees it, the
planner may only choose an allow-listed tool with validated arguments, sources are delimited and injection-flagged,
and the answer's citations and output are checked deterministically. Which case, documents and chat may be used
was decided by the server before run() is called; nothing the model writes can change that.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

from . import (authz, cases, clock, config, db, guardrails, ingest, integrations, intents, judgments, llm, prompts,
               retrieval, tools, websearch)

log = logging.getLogger(__name__)

KIND = {"case": "CASE FILE", "bare_act": "BARE ACT", "judgment": "JUDGMENT", "draft": "TEMPLATE", "web": "WEB",
        "news": "NEWS"}


# ── Chat persistence ─────────────────────────────────────────────────────────
def ensure_chat(chat_id: str | None, case_id: str | None, first_message: str) -> str:
    """The caller's own chat (a chat id belonging to someone else starts a new chat instead)."""
    ident = db.current()
    if chat_id:
        row = db.conn().execute("SELECT id FROM chats WHERE id=? AND COALESCE(user_id, '') = ? AND COALESCE(case_id, '') = ?",
                                (chat_id, ident.user_id or "", case_id or "")).fetchone()
        if row:
            return chat_id
    cid = db.new_id("chat_")
    title = re.sub(r"\s+", " ", first_message).strip()[:70] or "New research"
    with db.tx() as c:
        c.execute("INSERT INTO chats(id, case_id, title, created_at, updated_at, user_id, org_id) VALUES(?,?,?,?,?,?,?)",
                  (cid, case_id, title, db.now(), db.now(), ident.user_id, ident.org_id))
    return cid


def save_message(chat_id: str, role: str, content: str, sources: list | None = None, meta: dict | None = None) -> int:
    with db.tx() as c:
        mid = c.execute("INSERT INTO messages(chat_id, role, content, sources, meta, created_at) VALUES(?,?,?,?,?,?) "
                        "RETURNING id", (chat_id, role, content, json.dumps(sources or []), json.dumps(meta or {}),
                                         db.now())).fetchone()[0]
        c.execute("UPDATE chats SET updated_at=? WHERE id=?", (db.now(), chat_id))
        return mid


def history(chat_id: str, limit: int = 8) -> list[dict]:
    rows = db.conn().execute("SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                             (chat_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ── Helpers ──────────────────────────────────────────────────────────────────
def _case_docs(case_id: str | None) -> list[dict]:
    if not case_id:
        return []
    return [dict(r) for r in db.conn().execute(
        "SELECT id, title, doc_type, filename, pages, n_chunks FROM documents "
        "WHERE case_id=? AND status='ready' ORDER BY created_at", (case_id,))]


def _kind(h: retrieval.Hit) -> str:
    """Source type as the model sees it; statutes say whose law they are (central, or one state's)."""
    if h.collection == "bare_act" and h.jurisdiction:
        return "BARE ACT — CENTRAL LAW" if h.jurisdiction == "Central" else f"BARE ACT — STATE LAW: {h.jurisdiction.upper()}"
    if h.collection in ("web", "news"):
        return f"{KIND[h.collection]} — {h.extra.get('domain', '')} ({h.extra.get('reliability', '')})"
    if h.collection == "judgment" and h.doc_type == judgments.CATALOGUE_TYPE:
        return "JUDGMENT SUMMARY — SUPREME COURT OF INDIA (official headnote, abridged; full text not loaded)"
    return KIND.get(h.collection, h.collection)


def _source_items(hits: list[retrieval.Hit]) -> list[dict]:
    return [{"n": i, "chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "collection": h.collection,
             "kind": KIND.get(h.collection, h.collection), "jurisdiction": h.jurisdiction if h.collection == "bare_act" else "",
             "label": h.label, "page": h.page_start,
             "citation": h.citation(), "snippet": h.text[:320], "exact": h.exact,
             "injection_suspected": guardrails.is_suspicious(h.text),
             **({"url": h.extra.get("url"), "published": h.extra.get("published"), "domain": h.extra.get("domain"),
                 "reliability": h.extra.get("reliability")} if h.collection in ("web", "news") else {}),
             **(_catalogue_item(h) if h.doc_type == judgments.CATALOGUE_TYPE else {})}
            for i, h in enumerate(hits, 1)]


def _catalogue_item(h: retrieval.Hit) -> dict:
    """A Supreme Court catalogue entry: the official PDF in the open dataset, and whether its full text is loaded."""
    path = h.section or ""
    full = judgments.full_text_doc(path)
    return {"catalogue": True, "dataset_path": path, "kind": "JUDGMENT SUMMARY",
            "url": judgments.pdf_url(path, path[:4]) if path[:4].isdigit() else None,
            "full_text": (full or {}).get("status")}


def _context(hits: list[retrieval.Hit]) -> str:
    """Numbered sources, each inside delimiters the passage text cannot close (see guardrails.wrap_source)."""
    return "\n\n".join(guardrails.wrap_source(i, _kind(h),
                                              f"[{i}] {h.header}{f' | p. {h.page_start}' if h.page_start else ''}", h.text)
                       for i, h in enumerate(hits, 1))


def _hist_text(hist: list[dict], n: int = 6) -> str:
    return "\n".join(f"{m['role']}: {m['content'][:600]}" for m in hist[-n:]) or "(none)"


def _prior_turns(hist: list[dict], n: int = 4) -> list[dict]:
    return [{"role": m["role"], "content": m["content"][:1500]} for m in hist[-n:]]


def plan(message: str, hist: list[dict], docs: list[dict], info: dict | None = None) -> dict:
    doc_list = "; ".join(f"[D{i}] {guardrails.neutralise(d['title'])} ({d['doc_type']})"
                         for i, d in enumerate(docs, 1)) or "(none uploaded)"
    raw = llm.chat_json(
        [{"role": "user", "content": prompts.PLANNER.format(
            docs=doc_list, keys=", ".join(tools.LIMITATION), today=clock.stamp(),
            history=_hist_text(hist), message=message)}],
        model=config.FAST_LLM_MODEL,
        default={"intent": "research", "query": message},
        info=info,
    )
    p = guardrails.validate_plan(raw, message)   # allow-listed tool + schema-checked arguments only
    if p["doc_ref"] is not None and p["doc_ref"] > len(docs):
        p["doc_ref"] = None
    return p


def _step(meta: dict, name: str, status: str = "ok", **info) -> None:
    """Workflow trace: which step ran, how it ended, which model answered — never any document text."""
    meta.setdefault("trace", []).append({"step": name, "status": status, **{k: v for k, v in info.items() if v is not None}})


def _model_info(info: dict) -> dict:
    return {"model": info.get("model"), "fallback": bool(info.get("fallback")) or None}


_ALT_CITE = re.compile(r"【\s*(\d{1,2})(?:†[^】]*)?】")


def normalise_citations(text: str) -> str:
    """Some models emit 【5†L1-L4】 instead of [5]."""
    return _ALT_CITE.sub(lambda m: f"[{m.group(1)}]", text)


def _stream_llm(messages: list[dict], parts: list[str], model: str | None = None, meta: dict | None = None,
                step: str = "generate") -> Iterator[dict]:
    info: dict = {}
    try:
        for tok in llm.stream(messages, model=model, info=info):
            parts.append(tok)
            yield {"type": "token", "text": tok}
    except llm.LLMError:
        if meta is not None:
            _step(meta, step, "partial" if parts else "failed", **_model_info(info))
            meta["partial"] = bool(parts)
        raise
    if meta is not None:
        _step(meta, step, "fallback" if info.get("fallback") else "ok", **_model_info(info))


# ── Main entry ───────────────────────────────────────────────────────────────
def run(*, message: str, chat_id: str | None = None, case_id: str | None = None, scope: str = "all",
        doc_id: str | None = None, task: str | None = None, lang: str = "en",
        principal: authz.Principal | None = None, jurisdiction: str | None = None) -> Iterator[dict]:
    """`jurisdiction`: None = the case's state (if any); 'all' = no state filter; else a state / UT name."""
    t0 = time.time()
    rule = prompts.language_rule(lang)
    message = guardrails.normalise(message).strip()[:6000]
    case = cases.get_case(case_id) if case_id else None
    if not case:
        case_id = None
        if scope == "case":
            scope = "library"
    chat_id = ensure_chat(chat_id, case_id, message)
    hist = history(chat_id)
    save_message(chat_id, "user", message, meta={"scope": scope, "doc_id": doc_id, "task": task})
    yield {"type": "chat", "chat_id": chat_id}

    docs = _case_docs(case_id)
    answer: list[str] = []
    sources: list[dict] = []
    jur = "" if jurisdiction == "all" else (jurisdiction or (case or {}).get("state") or "")
    meta: dict = {"scope": scope, "lang": lang, "jurisdiction": jur or "all"}
    # The server authorised the case, document and chat before this generator was created.
    _step(meta, "authorize", case=bool(case_id), documents=len(docs))
    try:
        blocked = guardrails.screen_request(message)
        _step(meta, "screen", "blocked" if blocked else "ok", category=blocked[0] if blocked else None)
        if blocked:   # decided by code, before any model call
            meta.update(intent="blocked", blocked=blocked[0])
            db.audit("ai.request_blocked", "chat", chat_id, outcome="blocked", category=blocked[0])
            answer.append(blocked[1])
            yield {"type": "meta", "intent": "blocked", "query": None}
            yield {"type": "token", "text": blocked[1]}
            p = None
        elif task and doc_id:
            p = {"intent": "analyze_document", "analysis": task, "query": message, "calc": {}, "alt_queries": []}
        elif intents.clock_question(message):   # decided by code: the time is never left to a model
            p = {"intent": "time", "query": message, "clock": intents.clock_question(message)}
            _step(meta, "classify", "rule", intent="time")
        else:
            yield {"type": "status", "step": "plan", "text": "Understanding the question…"}
            info: dict = {}
            p = plan(message, hist, docs, info)
            _step(meta, "classify", "ok" if info.get("ok") else "default", intent=p["intent"], **_model_info(info))
            if p["intent"] == "out_of_scope" and intents.has_legal_angle(message):
                # Never turn away a question with a legal angle (cybercrime, IT Act, a famous trial…).
                _step(meta, "scope_check", "overridden", requested="out_of_scope", used="research")
                p["intent"] = "research"
            elif p["intent"] == "news" and not intents.asks_for_recent(message):
                _step(meta, "scope_check", "overridden", requested="news", used="research")
                p["intent"], p["external"] = "research", True   # a past event: research it, with web sources
            elif not info.get("ok") and intents.looks_like_code_request(message):
                p["intent"] = "out_of_scope"   # planner unavailable: an obvious coding request still gets the redirect
                _step(meta, "scope_check", "rule", requested="research", used="out_of_scope")
        if p is not None:
            if principal is not None:
                allowed = guardrails.allowed_intent(principal, p["intent"], case_id)
                if allowed != p["intent"]:
                    _step(meta, "tool_policy", "downgraded", requested=p["intent"], used=allowed)
                p["intent"] = allowed
            meta["intent"] = p["intent"]
            yield {"type": "meta", "intent": p["intent"], "query": p.get("query")}

        if p is None:
            pass
        elif p["intent"] == "chitchat":
            msgs = [{"role": "system", "content": prompts.CHAT_SYSTEM + rule}, *_prior_turns(hist, 2),
                    {"role": "user", "content": message}]
            yield from _stream_llm(msgs, answer, model=config.FAST_LLM_MODEL, meta=meta)

        elif p["intent"] == "time":
            text, now = clock.answer(p.get("clock") or "time", lang)
            meta["clock"] = {"verified": now.verified, "source": now.source, "tz": now.tz_name, "at": now.dt.isoformat()}
            _step(meta, "clock", "ok" if now.verified else "unverified")
            answer.append(text)
            yield {"type": "token", "text": text}

        elif p["intent"] == "out_of_scope":
            text = intents.OFF_TOPIC.get(lang, intents.OFF_TOPIC["en"])
            _step(meta, "redirect")
            answer.append(text)
            yield {"type": "token", "text": text}

        elif p["intent"] == "clarify":
            text = (p.get("clarify_question") or "").strip() or intents.CLARIFY_DEFAULT
            answer.append(text)
            yield {"type": "token", "text": text}

        elif p["intent"] == "news":
            yield from _news(message, p, answer, sources, meta, rule)

        elif p["intent"] == "calculation":
            text = _calculation(p.get("calc") or {})
            _step(meta, "calculate", tool="limitation")
            answer.append(text)
            yield {"type": "token", "text": text}

        elif p["intent"] == "analyze_document":
            yield from _analyze(message, p, docs, doc_id, case_id, answer, sources, meta, rule)

        else:
            yield from _research(message, p, case, docs, scope, hist, answer, sources, meta, rule, jur)

    except llm.LLMError as exc:
        err = str(exc)
        meta["error"] = err
        yield {"type": "error", "text": err}
        if not answer:
            answer.append(f"⚠️ {err}")
        elif meta.get("partial"):   # never present a cut-off answer as complete
            note = "\n\n*⚠️ This answer is incomplete: the language model stopped before finishing. It was not verified.*"
            answer.append(note)
            yield {"type": "token", "text": note}
    except Exception:  # never leak internals to the browser
        log.exception("Agent failure")
        meta["error"] = "internal"
        yield {"type": "error", "text": "Something went wrong while answering. Please try again."}
        if not answer:
            answer.append("⚠️ Something went wrong while answering. Please try again.")

    meta["elapsed"] = round(time.time() - t0, 2)
    raw_text = normalise_citations("".join(answer))
    final, findings = guardrails.scan_output(raw_text)   # never store or show secrets / internal instructions
    _step(meta, "output_check", "filtered" if findings else "ok")
    meta["fallback_used"] = any(s.get("fallback") for s in meta.get("trace", []))
    if findings:
        meta["output_filtered"] = findings
        db.audit("ai.output_filtered", "chat", chat_id, outcome="filtered", findings=findings)
        yield {"type": "final", "text": final}
    mid = save_message(chat_id, "assistant", final, sources, meta)
    yield {"type": "done", "message_id": mid, "elapsed": meta["elapsed"], "meta": meta}


# ── Branches ─────────────────────────────────────────────────────────────────
def _calculation(calc: dict) -> str:
    cause, start = calc.get("cause"), calc.get("start_date")
    if not cause or cause not in tools.LIMITATION or not start:
        rows = "\n".join(f"- `{c['key']}` — {c['label']} ({c['period']}; {c['basis']})" for c in tools.categories())
        return ("To compute a limitation deadline I need **the type of proceeding** and **the date the period starts** "
                "(e.g. *“limitation for breach of contract, broken on 2024-03-15”*).\n\nSupported categories:\n" + rows)
    try:
        r = tools.limitation(cause, start)
    except ValueError:
        return "Please give the start date as YYYY-MM-DD."
    return (f"**{r['label']}**\n\n"
            f"| | |\n|---|---|\n| Period | {r['period']} |\n| Basis | {r['basis']} |\n"
            f"| Period starts | {r['starts']} |\n| Start date used | {r['start_date']} |\n"
            f"| **Last date** | **{r['deadline']}** ({r['status']}) |\n\n"
            f"> {r['caveat']}\n\n*Computed by a deterministic Python tool — not by the language model.*")


def _pick_doc(message: str, p: dict, docs: list[dict], doc_id: str | None, case_id: str | None) -> dict | None:
    if doc_id:
        row = db.conn().execute("SELECT id, title, doc_type, filename, pages, n_chunks FROM documents "
                                "WHERE id=? AND status='ready'", (doc_id,)).fetchone()
        return dict(row) if row else None
    ref = p.get("doc_ref")
    try:
        if ref is not None and 1 <= int(str(ref).lstrip("Dd")) <= len(docs):
            return docs[int(str(ref).lstrip("Dd")) - 1]
    except ValueError:
        pass
    if len(docs) == 1:
        return docs[0]
    if docs:  # let retrieval decide which document the question is about
        hits, _ = retrieval.search(p.get("query") or message, scope="case",
                                   case_id=case_id, doc_ids={d["id"] for d in docs}, k=3)
        if hits:
            return next((d for d in docs if d["id"] == hits[0].doc_id), docs[0])
        return docs[-1]
    return None


def _analyze(message: str, p: dict, docs: list[dict], doc_id: str | None, case_id: str | None,
             answer: list[str], sources: list[dict], meta: dict, rule: str = "") -> Iterator[dict]:
    doc = _pick_doc(message, p, docs, doc_id, case_id)
    if not doc:
        text = ("There is no uploaded document to analyse yet. Open a case, upload the judgment, petition or "
                "agreement in **Case documents**, and ask again.")
        answer.append(text)
        yield {"type": "token", "text": text}
        return
    task = p.get("analysis") or "summary"
    chunks, _ = ingest.document_text(doc["id"])
    body = "\n".join(f"[p. {ps}] {t}" for ps, _, t in chunks)
    meta.update({"doc_id": doc["id"], "task": task})
    item = {"n": 1, "chunk_id": None, "doc_id": doc["id"], "title": doc["title"], "collection": "case",
            "kind": "CASE FILE", "label": f"{doc.get('pages') or '?'} pages", "page": 1,
            "citation": doc["title"], "snippet": chunks[0][2][:320] if chunks else "", "exact": False}
    sources.append(item)
    yield {"type": "sources", "items": sources}

    if len(body) > config.DOC_ANALYSIS_MAX_CHARS:
        parts = [body[i:i + 40000] for i in range(0, len(body), 40000)][:8]
        yield {"type": "status", "step": "read", "text": f"Reading “{doc['title']}” in {len(parts)} parts…"}

        def note(i_part):
            i, part = i_part
            return llm.chat([{"role": "user", "content": prompts.MAP_NOTES.format(
                task=task, request=message[:300], i=i, n=len(parts), text=part)}], model=config.FAST_LLM_MODEL)

        with ThreadPoolExecutor(max_workers=3) as ex:
            notes = list(ex.map(note, enumerate(parts, 1)))
        _step(meta, "read_parts", parts=len(parts))
        body = "\n\n".join(f"NOTES ON PART {i}:\n{n}" for i, n in enumerate(notes, 1))
    yield {"type": "status", "step": "generate", "text": f"Analysing “{doc['title']}”…"}
    if guardrails.is_suspicious(body):
        item["injection_suspected"] = True
        yield {"type": "sources", "items": sources}
    msgs = [{"role": "system", "content": prompts.ANALYZE_SYSTEM + rule},
            {"role": "user", "content": f"TASK: {task}\nUSER REQUEST: {message}\n\n"
                                        f"{guardrails.wrap_document(doc['title'], body)}"}]
    yield from _stream_llm(msgs, answer, meta=meta)


def _research(message: str, p: dict, case: dict | None, docs: list[dict], scope: str, hist: list[dict],
              answer: list[str], sources: list[dict], meta: dict, rule: str = "", jur: str = "") -> Iterator[dict]:
    case_id = case["id"] if case else None
    drafting = p["intent"] == "drafting"
    where = {"case": "this case's documents", "library": "the law library"}.get(scope, "case documents and the law library")
    yield {"type": "status", "step": "retrieve", "text": f"Searching {where}…"}

    query = p["query"]
    ctx = (case or {}).get("acts_sections") or ""
    hits, diag = retrieval.search(query, scope=scope, case_id=case_id, extra_queries=p["alt_queries"], hint=message,
                                  context=ctx, jurisdiction=jur)
    weak = diag.get("confidence") is not None and diag["confidence"] < config.RELEVANCE_FLOOR and not diag["exact_hits"]
    _step(meta, "retrieve", "weak" if (not hits or weak) else "ok", passages=len(hits), scope=scope,
          jurisdiction=jur or "all")
    if (not hits or weak) and not drafting:
        yield {"type": "status", "step": "rewrite", "text": "Weak matches — refining the search…"}
        info: dict = {}
        try:
            q2 = llm.chat([{"role": "user", "content": prompts.REWRITE.format(q=query)}],
                          model=config.FAST_LLM_MODEL, max_tokens=120, info=info).strip().strip('"')[:300]
        except llm.LLMError:   # optional step: continue with the original search instead of failing the answer
            q2 = ""
        _step(meta, "rewrite_query", "ok" if q2 else "skipped", **_model_info(info))
        if q2:
            hits2, diag2 = retrieval.search(q2, scope=scope, case_id=case_id, extra_queries=[query], hint=message,
                                            context=ctx, jurisdiction=jur)
            if hits2 and (diag2.get("confidence") or -99) > (diag.get("confidence") or -99):
                hits, diag, query = hits2, diag2, q2
        weak = diag.get("confidence") is not None and diag["confidence"] < config.RELEVANCE_FLOOR and not diag["exact_hits"]

    # Live web search when the library has nothing reliable, or when the question is about a specific real event,
    # case or person that a statute library cannot contain. Never for drafting or a "case documents only" search.
    if not drafting and scope != "case" and (not hits or weak or p.get("external")):
        yield {"type": "status", "step": "web", "text": "Searching reliable web sources…"}
        web, why = websearch.search(intents.strip_parties(p["query"], case))
        _step(meta, "web_search", "ok" if web else "empty" if why == websearch.NO_RESULT else "unavailable",
              results=len(web))
        meta["web"] = {"results": len(web), "domains": [r.domain for r in web], "reason": why or None}
        if web:
            library = [] if (not hits or weak) else hits[: max(config.CONTEXT_PASSAGES - len(web), 3)]
            hits, weak = library + _web_hits(web), False
    meta["retrieval"] = {k: diag.get(k) for k in ("confidence", "refs", "acts", "candidates", "exact_hits")}
    meta["query"] = query

    template = None
    if drafting:
        yield {"type": "status", "step": "template", "text": "Finding the closest draft template…"}
        drafts = {r["id"]: json.loads(r["meta"] or "{}").get("area", "") for r in db.conn().execute(
            "SELECT id, meta FROM documents WHERE collection='draft' AND status='ready'")}
        if drafts:
            # Match on what is being drafted *and* what the matter is about, then prefer
            # templates from the case's practice area (e.g. a Civil notice, not a s.80 Govt notice).
            facts = (case or {}).get("description", "")[:300]
            t_query = f"{p.get('draft_kind') or ''} {message} {facts}".strip()
            t_hits, _ = retrieval.search(t_query, scope="library", doc_ids=set(drafts), k=6, exclude=frozenset())
            if t_hits:
                area = (case or {}).get("case_type")
                best = max(t_hits, key=lambda h: h.score + (1.5 if drafts.get(h.doc_id) == area else 0.0))
                chunks, tdoc = ingest.document_text(best.doc_id)
                template = retrieval.Hit(chunk_id=best.chunk_id, doc_id=tdoc["id"], title=tdoc["title"],
                                         collection="draft", doc_type="template",
                                         header=f"Draft template: {tdoc['title']}",
                                         text="\n".join(t for *_, t in chunks)[:9000], label="template",
                                         section=None, page_start=None, page_end=None)
        hits = [h for h in hits if h.collection != "draft"][: config.CONTEXT_PASSAGES - 1]
        if template:
            hits = [template] + hits

    if not hits or (weak and not drafting):
        text = _no_answer(query, where, scope, meta.get("web"))
        answer.append(text)
        meta["abstained"] = True
        yield {"type": "token", "text": text}
        return

    sources.extend(_source_items(hits))
    yield {"type": "sources", "items": sources}
    yield {"type": "status", "step": "generate", "text": "Drafting…" if drafting else "Writing a cited answer…"}

    dossier = cases.dossier(case, docs) if case else "(no case selected — general research)"
    user = (f"TODAY: {clock.stamp()}\n\nCASE DOSSIER:\n{guardrails.neutralise(dossier)}\n\n"
            f"SOURCES:\n{_context(hits)}\n\n{'DRAFTING REQUEST' if drafting else 'QUESTION'}: {message}")
    msgs = [{"role": "system", "content": (prompts.DRAFT_SYSTEM if drafting else prompts.ANSWER_SYSTEM) + rule},
            *_prior_turns(hist), {"role": "user", "content": user}]
    yield from _stream_llm(msgs, answer, meta=meta)

    if drafting:
        return
    yield from _audit(message, hits, answer, meta, f"{message}\n{dossier}")
    _follow_up_judgments(hits, answer, meta)


def _follow_up_judgments(hits: list[retrieval.Hit], answer: list[str], meta: dict) -> None:
    """Supreme Court judgments the answer cited from the catalogue get their full text fetched in the background, so
    the next question about them is answered from the judgment itself; full texts that are used are kept longest."""
    try:
        cited = {int(n) for n in re.findall(r"\[(\d{1,2})\]", normalise_citations("".join(answer)))}
        used = [h for i, h in enumerate(hits, 1) if i in cited and h.collection == "judgment"]
        for h in used:
            if h.doc_type != judgments.CATALOGUE_TYPE:
                judgments.touch(h.doc_id)
        if config.SC_AUTO_FETCH:
            queued = judgments.queue_fetch([h.section for h in used if h.doc_type == judgments.CATALOGUE_TYPE][:2])
            if queued:
                meta["judgments_queued"] = queued
                _step(meta, "judgment_fetch", "queued", count=len(queued))
    except Exception:   # housekeeping must never break an answer
        log.exception("Judgment follow-up failed")


def _audit(message: str, hits: list[retrieval.Hit], answer: list[str], meta: dict, allowed_text: str) -> Iterator[dict]:
    """Citation audit: deterministic check for out-of-range numbers + LLM check of support."""
    text = normalise_citations("".join(answer))
    cited = {int(n) for n in re.findall(r"\[(\d{1,2})\]", text)}
    invalid = sorted(n for n in cited if n < 1 or n > len(hits))
    yield {"type": "status", "step": "verify", "text": "Checking citations against sources…"}
    info = {}
    v = llm.chat_json([{"role": "user", "content": prompts.VERIFY.format(sources=_context(hits)[:24000], answer=text)}],
                      model=config.FAST_LLM_MODEL, default={"grounded": None, "unsupported": []}, info=info)
    unsupported = [str(u)[:300] for u in (v.get("unsupported") or [])][:6]
    if invalid:
        unsupported.append(f"Cites source numbers that do not exist: {invalid}")
    # Deterministic check, independent of the model: every section/article and case citation must be in a source.
    fabricated = guardrails.verify_citations(
        text, [f"{h.label or ''} {h.title}\n{h.header}\n{h.text}" for h in hits],
        allowed_text=allowed_text, case_lookup=judgments.describe_case)   # cases checked against the SC register
    unsupported += fabricated
    grounded = (v.get("grounded") is not False) and not invalid and not unsupported
    if not cited:
        grounded, unsupported = False, unsupported or ["The answer contains no citations."]
    meta["verify"] = {"grounded": grounded, "unsupported": unsupported, "checked": v.get("grounded") is not None,
                      "citation_check": "deterministic", "unverified_citations": len(fabricated)}
    # The model-based audit can fail (provider down); the deterministic check always runs. Never report "verified"
    # when the model audit did not happen.
    _step(meta, "verify_citations", "ok" if grounded else "issues" if meta["verify"]["checked"] else "partial",
          issues=len(unsupported), model_check=meta["verify"]["checked"], **_model_info(info))
    yield {"type": "verify", **meta["verify"]}


# ── Live sources ─────────────────────────────────────────────────────────────
def _web_hits(results: list[websearch.WebResult]) -> list[retrieval.Hit]:
    return [retrieval.Hit(chunk_id=None, doc_id="", title=r.title, collection="web", doc_type="web",
                          header=f"{r.title} — {r.domain}", text=r.text, label=r.domain, section=None,
                          page_start=None, page_end=None,
                          extra={"url": r.url, "domain": r.domain, "published": r.published,
                                 "reliability": websearch.TIER_LABEL.get(r.tier, "other website")})
            for r in results]


_NEWS_STOP = {"latest", "recent", "news", "legal", "law", "india", "indian", "today", "todays", "current", "update",
              "updates", "development", "developments", "what", "are", "the", "in", "of", "and", "on", "this", "week",
              "new", "any", "tell", "me", "about", "headlines", "happening", "s"}


def _feed_hits(query: str, limit: int = 6) -> list[retrieval.Hit]:
    """Dated items from the LiveLaw feed (synced hourly by integrations); topic words narrow them when given."""
    try:
        rows = integrations.news(limit=80)
    except Exception:   # the feed is optional for answering; the web search still runs
        log.exception("Reading the news feed failed")
        return []
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _NEWS_STOP and len(w) > 2}
    if words:
        rows = [r for r in rows if words & set(re.findall(r"[a-z0-9]+", (r.get("title") or "").lower()))]
    out = []
    for r in rows[:limit]:
        pub = (r.get("published_at") or "")[:10] or None
        cats = ", ".join((r.get("data") or {}).get("categories") or [])
        out.append(retrieval.Hit(chunk_id=None, doc_id="", title=r["title"], collection="news", doc_type="news",
                                 header=f"{r['title']} — livelaw.in", label="livelaw.in", section=None,
                                 page_start=None, page_end=None,
                                 text=f"Headline: {r['title']}" + (f"\nTopics: {cats}" if cats else ""),
                                 extra={"url": r.get("url"), "domain": "livelaw.in", "published": pub,
                                        "reliability": "legal publisher"}))
    return out


def _dated_context(hits: list[retrieval.Hit]) -> str:
    return "\n\n".join(guardrails.wrap_source(
        i, _kind(h), f"[{i}] {h.header} | " + (f"published {h.extra['published']}" if h.extra.get("published")
                                               else "publication date not stated by the source"), h.text)
        for i, h in enumerate(hits, 1))


def _news(message: str, p: dict, answer: list[str], sources: list[dict], meta: dict, rule: str = "") -> Iterator[dict]:
    general = p.get("news_scope") == "general"
    query = p.get("query") or message
    if general:   # outside the app's purpose: offer legal news instead of general headlines
        note = ("I focus on legal news, so I don't report general headlines. Here are the latest legal developments "
                "I could find:\n\n")
        answer.append(note)
        yield {"type": "token", "text": note}
        query = "latest legal news India Supreme Court High Court"
    yield {"type": "status", "step": "news", "text": "Checking current legal news…"}
    feed = _feed_hits("" if general else query)
    web, why = websearch.search(query if re.search(r"\bnews\b", query, re.I) else f"{query} India legal news")
    hits = (feed[:5] + _web_hits(web))[: config.CONTEXT_PASSAGES]
    _step(meta, "news_search", "ok" if hits else "empty", feed=len(feed), web=len(web))
    meta["web"] = {"results": len(web), "domains": [r.domain for r in web], "reason": why or None}
    if not hits:
        text = ("I couldn't retrieve current news right now" + (f" ({why})" if why else "")
                + ", so I won't describe recent events from memory — that could be out of date or wrong.\n\n"
                "You can check the latest directly on the Supreme Court website (sci.gov.in), India Code "
                "(indiacode.nic.in) or LiveLaw, or ask me again in a few minutes. I can still answer questions about "
                "the law itself.")
        answer.append(text)
        meta["abstained"] = True
        yield {"type": "token", "text": text}
        return
    sources.extend(_source_items(hits))
    yield {"type": "sources", "items": sources}
    yield {"type": "status", "step": "generate", "text": "Summarising the news…"}
    msgs = [{"role": "system", "content": prompts.NEWS_SYSTEM.format(today=clock.stamp()) + rule},
            {"role": "user", "content": f"SOURCES:\n{_dated_context(hits)}\n\nREQUEST: {message}"}]
    yield from _stream_llm(msgs, answer, meta=meta)
    if why and why != websearch.NO_RESULT:   # say what was not checked
        note = f"\n\n*The web search was unavailable ({why}); these items come only from the LiveLaw feed.*"
        answer.append(note)
        yield {"type": "token", "text": note}
    yield from _audit(message, hits, answer, meta, message)


def _no_answer(query: str, where: str, scope: str, web: dict | None) -> str:
    """Transparent "not found": what was searched, what was not, and what to do next — never a guess."""
    text = f"I searched {where} for **“{query}”** but found no passage that answers this reliably, so I won't guess."
    if scope == "case":
        text += (" The search scope is set to *case documents only*, so the law library and the web were not "
                 "searched.")
    elif web is not None and web.get("reason") == websearch.NO_RESULT:
        text += " I also searched the web but found no reliable source for it."
    elif web is not None and web.get("reason"):
        text += f" I could not check web sources either ({web['reason']})."
    return (text + "\n\n**What helps:**\n"
            "- Upload the relevant judgment, petition or agreement to the case (Case documents).\n"
            "- Name the provision, case or event precisely, e.g. *Section 138 NI Act* or *State v. Kasab (2012)*.\n"
            "- Switch the search scope to **Case + Library** if it is set narrower.\n\n"
            "Would you like me to try a different wording or another source?")
