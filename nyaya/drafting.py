"""Document-aware drafting engine (drafts, legal notices) with court-/forum-wise customisation.

    intake (facts with provenance, missing information, inconsistencies)
      → law retrieval (local library) + forum requirements verified against the bare acts
      → draft with fact tags ⟦F#⟧, law citations [n] and [●: …] placeholders
      → post-checks (unknown tags/citations, placeholders left) → saved with versions

Nothing is invented: unknown particulars stay as placeholders, statutory periods are stated only
when the retrieved provision text states them, and every draft is labelled for professional review.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from . import cases, config, db, docintel, guardrails, library, llm, prompts, retrieval

DATA = config.ROOT / "data" / "drafting"   # configuration shipped with the app (not user data)
FILE_CHARS = 14000          # per supporting file
MATERIAL_CHARS = 60000
PLACEHOLDER = re.compile(r"\[●[^\]]*\]")
FACT_TAG = re.compile(r"⟦(F\d{1,3})⟧")
CITE = re.compile(r"\[(\d{1,2})\]")
REVIEW_LINE = re.compile(r"^[\s*_#>]*DRAFT\b", re.I)   # the label, even if the model formats it in Markdown


@lru_cache(maxsize=4)
def _load(name: str, mtime: float) -> dict:
    return json.loads((DATA / name).read_text("utf-8"))


def _cfg(name: str) -> dict:
    p = DATA / name
    return _load(name, p.stat().st_mtime)


def doc_types() -> list[dict]:
    return _cfg("doc_types.json")["types"]


def forums() -> list[dict]:
    return _cfg("forums.json")["forums"]


def styles() -> list[dict]:
    return _cfg("forums.json")["styles"]


def doc_type(type_id: str) -> dict:
    t = next((t for t in doc_types() if t["id"] == type_id), None)
    if not t:
        raise ValueError("Unknown document type")
    return t


def forum(forum_id: str) -> dict:
    f = next((f for f in forums() if f["id"] == forum_id), None)
    if not f:
        raise ValueError("Unknown court / forum")
    return f


def user_requirements(forum_id: str) -> list[dict]:
    raw = db.get_setting(f"forum_reqs:{forum_id}")   # verified requirements added by the organisation
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


def forum_profile(forum_id: str) -> dict:
    """Forum with each requirement verified against the library (or marked 'confirm')."""
    f = dict(forum(forum_id))
    reqs = []
    for r in f.get("requirements", []):
        item = {"text": r["text"], "source": "library"}
        if r.get("library_ref"):
            item["provision"] = library.lookup_provision(*r["library_ref"])
            item["status"] = "verified_in_library" if item["provision"]["found"] else "confirm"
        reqs.append(item)
    for r in user_requirements(forum_id):
        reqs.append({"text": r.get("text", ""), "source": "user", "url": r.get("url", ""),
                     "verified_on": r.get("verified_on", ""), "status": "user_supplied"})
    f["requirements"] = reqs
    return f


def type_profile(type_id: str) -> dict:
    t = dict(doc_type(type_id))
    t["provisions"] = [dict(library.lookup_provision(act, ref), why=why) for act, ref, why in t.get("library_refs", [])]
    return t


# ── Material ─────────────────────────────────────────────────────────────────
def _file_text(doc_id: str, case_id: str | None) -> tuple[dict, str]:
    row = db.row_to_dict(db.conn().execute("SELECT * FROM documents WHERE id=? AND status='ready'", (doc_id,)).fetchone())
    if not row or row["collection"] != "case" or (case_id and row["case_id"] != case_id):
        raise ValueError("A supporting document was not found in this case")
    tr = docintel.derivative(doc_id, "translation_en")
    pages = json.loads(tr["content"]) if tr else docintel.extracted_pages(doc_id)
    return row, "\n".join(f"[p. {i}] {p}" for i, p in enumerate(pages, 1))[:FILE_CHARS]


def gather(case: dict | None, doc_ids: list[str], known: dict, instructions: str) -> tuple[str, list[dict]]:
    parts, files = [], []
    if case:
        docs = [dict(r) for r in db.conn().execute(
            "SELECT id, title, doc_type FROM documents WHERE case_id=? AND status='ready'", (case["id"],))]
        parts.append("CASE DOSSIER:\n" + guardrails.neutralise(cases.dossier(case, docs)))
    if known:
        parts.append("USER-SUPPLIED DETAILS:\n" + "\n".join(f"- {k}: {v}" for k, v in known.items() if str(v).strip()))
    if instructions:
        parts.append(f"USER INSTRUCTIONS:\n{instructions}")
    for i, d in enumerate(doc_ids[:6], 1):
        row, text = _file_text(d, case["id"] if case else None)
        files.append({"n": i, "doc_id": d, "title": row["title"]})
        parts.append(guardrails.wrap_document(f"D{i}: {row['title']}", text))
    return "\n\n".join(parts)[:MATERIAL_CHARS], files


def _template_text(template_doc_id: str | None, base_doc_id: str | None, base_text: str | None, case_id: str | None,
                   type_cfg: dict) -> tuple[str, dict | None]:
    if base_text:
        return guardrails.neutralise(base_text[:12000]), {"kind": "pasted", "title": "Draft supplied by the user"}
    if base_doc_id:
        row, text = _file_text(base_doc_id, case_id)
        return guardrails.neutralise(text[:12000]), {"kind": "uploaded", "title": row["title"], "doc_id": base_doc_id}
    tid = template_doc_id
    if not tid:
        drafts = {r["id"] for r in db.conn().execute("SELECT id FROM documents WHERE collection='draft' AND status='ready'")}
        if drafts:
            hits, _ = retrieval.search(type_cfg.get("template_match") or type_cfg["label"], scope="library",
                                       doc_ids=drafts, k=1, exclude=frozenset())
            tid = hits[0].doc_id if hits else None
    if not tid:
        return "(none — use the STRUCTURE)", None
    row = db.conn().execute("SELECT title, path FROM documents WHERE id=? AND collection='draft'", (tid,)).fetchone()
    if not row:
        return "(none — use the STRUCTURE)", None
    return config.from_stored(row["path"]).read_text("utf-8")[:12000], {"kind": "library", "title": row["title"], "doc_id": tid}


# ── Generation ───────────────────────────────────────────────────────────────
def generate(*, doc_type_id: str, forum_id: str, style: str = "formal", lang: str = "en", case_id: str | None = None,
             doc_ids: list[str] | None = None, known: dict | None = None, instructions: str = "",
             template_doc_id: str | None = None, base_doc_id: str | None = None, base_text: str | None = None,
             title: str | None = None, draft_id: str | None = None, progress=lambda *a: None) -> dict:
    type_cfg = doc_type(doc_type_id)
    fprof = forum_profile(forum_id)
    style_cfg = next((s for s in styles() if s["id"] == style), styles()[0])
    case = cases.get_case(case_id) if case_id else None
    if case_id and not case:
        raise ValueError("Case not found")
    known = {k: str(v)[:2000] for k, v in (known or {}).items() if str(v).strip()}

    progress(0, 4, "Reading the supplied material")
    material, files = gather(case, doc_ids or [], known, instructions[:4000])
    required = "; ".join(lbl for _, lbl in type_cfg.get("required", []))
    intake = llm.chat_json([{"role": "user", "content": prompts.DRAFT_INTAKE.format(
        doc_type=type_cfg["label"], kind=type_cfg["kind"], required=required or "none", material=material)}],
        default={"facts": [], "missing": []})
    facts = [f for f in intake.get("facts") or [] if isinstance(f, dict) and f.get("text")]
    # Details the user typed are facts by definition — never left for the model to drop.
    labels = dict(type_cfg.get("required", []))
    for k, v in known.items():
        if not any(v.lower() in str(f["text"]).lower() for f in facts):
            facts.append({"text": f"{labels.get(k, k.replace('_', ' '))}: {v}", "origin": "user", "ref": "user input"})
    for i, f in enumerate(facts, 1):
        f["id"] = f"F{i}"
        f["origin"] = f.get("origin") if f.get("origin") in ("file", "user", "case") else "file"
    missing = [m for m in intake.get("missing") or [] if isinstance(m, dict) and m.get("question")]

    progress(1, 4, "Finding the applicable law in the library")
    fact_summary = " ".join(f["text"] for f in facts[:12])[:1200]
    query = f"{type_cfg['label']} {instructions[:300]} {fact_summary}"
    hits, _ = retrieval.search(query, scope="library", k=6, hint=f"{instructions} {known.get('provisions', '')}",
                               context=(case or {}).get("acts_sections", ""))
    law = [h for h in hits if h.collection != "draft"]
    # Provisions the document type depends on (e.g. NI Act s.138 for a cheque notice) are added verbatim.
    for p in type_profile(doc_type_id)["provisions"]:
        if p["found"] and not any(h.chunk_id == p["chunk_id"] for h in law):
            law.insert(0, retrieval.Hit(chunk_id=p["chunk_id"], doc_id=p["doc_id"], title=p["act_title"], collection="bare_act",
                                        doc_type="statute", header=p["heading"], text=p["text"], label=p["label"],
                                        section=None, page_start=p["page"], page_end=p["page"], exact=True))
    law = law[:8]
    template, template_info = _template_text(template_doc_id, base_doc_id, base_text, case_id, type_cfg)

    progress(2, 4, "Writing the draft")
    reqs = [f"- {r['text']}: " + (f"{r['provision']['label']} of {r['provision']['act_title']} (verified in library)"
                                  if r.get("status") == "verified_in_library" else
                                  "user-supplied requirement" if r["source"] == "user" else "CONFIRM — not verified")
            for r in fprof["requirements"]] + [f"- CONFIRM: {c}" for c in fprof.get("confirm", [])]
    facts_txt = "\n".join(f"{f['id']} [{f['origin']}: {f.get('ref', '')}] {f['text']}" for f in facts) or "(no facts supplied)"
    law_txt = "\n\n".join(guardrails.wrap_source(i, h.collection, f"[{i}] {h.header}"
                                                 f"{f' | p. {h.page_start}' if h.page_start else ''}", h.text[:2500])
                          for i, h in enumerate(law, 1)) or "(no provisions retrieved)"
    rule = prompts.language_rule(lang, headings=False)
    content = llm.chat([{"role": "user", "content": prompts.DRAFT_FILL.format(
        doc_label=type_cfg["label"], forum=fprof["name"], structure="; ".join(type_cfg["structure"]),
        heading=fprof.get("heading", ""), style=style_cfg["instruction"], lang_rule=rule.strip(),
        forum_reqs="\n".join(reqs) or "(none configured — confirm with the registry)", template=template,
        facts=facts_txt, law=law_txt, instructions=instructions[:4000] or "(none)")}], temperature=0.1)
    if not content.strip():
        raise llm.LLMError("The model returned an empty draft. Please try again.")
    if not REVIEW_LINE.match(content):
        content = "DRAFT — FOR REVIEW BY A QUALIFIED LEGAL PROFESSIONAL\n\n" + content

    progress(3, 4, "Checking the draft")
    checks = check_draft(content, facts, len(law))
    sources = [{"n": i, "chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "collection": h.collection,
                "label": h.label, "page": h.page_start, "citation": h.citation(), "snippet": h.text[:320]}
               for i, h in enumerate(law, 1)]
    record = {
        "case_id": case_id, "kind": type_cfg["kind"], "doc_type": doc_type_id, "forum": forum_id, "style": style,
        "lang": lang, "title": (title or f"{type_cfg['label']}" + (f" — {case['title']}" if case else ""))[:200],
        "content": content, "facts": facts, "missing": missing, "sources": sources,
        "inputs": {"known": known, "instructions": instructions, "doc_ids": doc_ids or [], "files": files,
                   "template": template_info, "inconsistencies": intake.get("inconsistencies") or [],
                   "unsupported": intake.get("unsupported") or [], "detected_doc_type": intake.get("detected_doc_type"),
                   "checks": checks, "forum_requirements": fprof["requirements"], "forum_confirm": fprof.get("confirm", []),
                   "forum_official": fprof.get("official", []), "formalities": type_cfg.get("formalities", []),
                   "template_doc_id": template_doc_id, "base_doc_id": base_doc_id},
    }
    saved = save(record, draft_id, note="generated")
    progress(4, 4, "Done")
    return saved


def check_draft(content: str, facts: list[dict], n_law: int) -> dict:
    ids = {f["id"] for f in facts}
    tags = set(FACT_TAG.findall(content))
    cites = {int(n) for n in CITE.findall(content)}
    return {"placeholders": PLACEHOLDER.findall(content)[:80], "unknown_fact_tags": sorted(tags - ids),
            "invalid_citations": sorted(n for n in cites if n < 1 or n > n_law), "facts_used": sorted(tags & ids),
            "labelled_for_review": bool(REVIEW_LINE.match(content))}


# ── Persistence ──────────────────────────────────────────────────────────────
_JSON = ("facts", "missing", "sources", "inputs")


def save(rec: dict, draft_id: str | None = None, note: str = "") -> dict:
    now = db.now()
    ident = db.current()
    with db.tx() as c:
        if draft_id and c.execute("SELECT 1 FROM drafts WHERE id=?", (draft_id,)).fetchone():
            v = c.execute("SELECT version FROM drafts WHERE id=?", (draft_id,)).fetchone()[0] + 1
            # Any new version needs its own review: an approval applies to one version only.
            c.execute("UPDATE drafts SET kind=?, doc_type=?, forum=?, style=?, lang=?, title=?, content=?, facts=?, missing=?, "
                      "sources=?, inputs=?, version=?, updated_at=?, review_status='draft', approved_version=NULL WHERE id=?",
                      (rec["kind"], rec["doc_type"], rec["forum"], rec["style"], rec["lang"], rec["title"], rec["content"],
                       *(json.dumps(rec[k], ensure_ascii=False) for k in _JSON), v, now, draft_id))
        else:
            draft_id, v = db.new_id("draft_"), 1
            c.execute("INSERT INTO drafts(id, case_id, kind, doc_type, forum, style, lang, title, content, facts, missing, "
                      "sources, inputs, version, created_at, updated_at, created_by, org_id) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (draft_id, rec.get("case_id"), rec["kind"], rec["doc_type"], rec["forum"], rec["style"], rec["lang"],
                       rec["title"], rec["content"], *(json.dumps(rec[k], ensure_ascii=False) for k in _JSON), v, now, now,
                       ident.user_id, ident.org_id or db.DEFAULT_ORG))
        c.execute("INSERT INTO draft_versions(draft_id, version, content, note, created_at) VALUES(?,?,?,?,?)",
                  (draft_id, v, rec["content"], note, now))
    db.audit(f"{rec['kind']}.{note or 'save'}", "draft", draft_id, version=v, doc_type=rec["doc_type"])
    return get(draft_id)


def get(draft_id: str) -> dict | None:
    d = db.row_to_dict(db.conn().execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone(), _JSON)
    if d:
        d["versions"] = [dict(r) for r in db.conn().execute(
            "SELECT version, note, created_at, length(content) AS chars FROM draft_versions WHERE draft_id=? ORDER BY version DESC",
            (draft_id,))]
    return d


def update_content(draft_id: str, content: str, title: str | None = None) -> dict | None:
    d = get(draft_id)
    if not d:
        return None
    d["content"] = content[:200000]
    if title:
        d["title"] = title[:200]
    d["inputs"]["checks"] = check_draft(d["content"], d["facts"], len(d["sources"]))
    return save(d, draft_id, note="edited")


def version(draft_id: str, v: int) -> dict | None:
    r = db.conn().execute("SELECT * FROM draft_versions WHERE draft_id=? AND version=?", (draft_id, v)).fetchone()
    return dict(r) if r else None


def list_drafts(case_id: str | None = None, kind: str | None = None, *, user_id: str | None = None,
                team_cases: list[str] | None = None) -> list[dict]:
    """The caller's own drafts plus the drafts of cases where they are on the case team."""
    sql, args = ("SELECT id, case_id, kind, doc_type, forum, style, lang, title, version, updated_at, review_status, "
                 "approved_version FROM drafts WHERE 1=1"), []
    team = list(team_cases or [])
    sql += " AND ((case_id IS NULL AND created_by = ?)" + (f" OR case_id IN ({','.join('?' * len(team))})" if team else "") + ")"
    args += [user_id or "", *team]
    if case_id:
        sql += " AND case_id=?"
        args.append(case_id)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in db.conn().execute(sql + " ORDER BY updated_at DESC LIMIT 200", args)]


REVIEW_ACTIONS = ("approve", "request_changes", "reopen")


def review_blockers(d: dict) -> list[str]:
    """What must be fixed before a draft can be approved (re-checked on the current text)."""
    checks = check_draft(d["content"], d.get("facts") or [], len(d.get("sources") or []))
    out = []
    if checks["placeholders"]:
        out.append(f"{len(checks['placeholders'])} placeholder(s) [●] still to fill")
    if checks["invalid_citations"]:
        out.append(f"citations to sources that do not exist: {checks['invalid_citations']}")
    if checks["unknown_fact_tags"]:
        out.append(f"fact tags without a fact: {checks['unknown_fact_tags']}")
    return out


def review(draft_id: str, action: str, note: str = "", reviewer: str | None = None) -> dict:
    """Human review: a generated document stays a draft until an advocate approves a specific version."""
    d = get(draft_id)
    if not d:
        raise ValueError("Draft not found")
    if action not in REVIEW_ACTIONS:
        raise ValueError("Unknown review action")
    if action == "approve":
        blockers = review_blockers(d)
        if blockers:
            raise ValueError("This draft cannot be approved yet: " + "; ".join(blockers) + ".")
        status, version = "approved", d["version"]
    else:
        status, version = ("changes_requested" if action == "request_changes" else "draft"), None
    with db.tx() as c:
        c.execute("UPDATE drafts SET review_status=?, approved_version=?, reviewed_by=?, reviewed_at=?, review_note=? "
                  "WHERE id=?", (status, version, reviewer, db.now(), (note or "")[:1000], draft_id))
    db.audit(f"draft.{action}", "draft", draft_id, version=d["version"])
    return get(draft_id)


def approval(d: dict) -> dict | None:
    """Approval details for export, only when the current version is the approved one."""
    if d.get("review_status") != "approved" or d.get("approved_version") != d.get("version"):
        return None
    with db.system():
        r = db.conn().execute("SELECT display_name FROM profiles WHERE user_id=?", (d.get("reviewed_by"),)).fetchone()
    return {"by": (r["display_name"] if r else "") or "reviewer", "at": (d.get("reviewed_at") or "")[:10],
            "version": d["version"]}


def delete(draft_id: str) -> bool:
    with db.tx() as c:
        ok = c.execute("DELETE FROM drafts WHERE id=?", (draft_id,)).rowcount > 0
    if ok:
        db.audit("draft.delete", "draft", draft_id)
    return ok


def clean_for_export(content: str) -> str:
    """Remove fact tags (internal provenance markers) but keep placeholders visible."""
    return FACT_TAG.sub("", content).replace(" .", ".")
