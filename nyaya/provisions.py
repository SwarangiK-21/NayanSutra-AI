"""Identification of potentially applicable acts, sections and articles for a case.

The model may only choose among passages retrieved from the official bare-act library, so every
suggestion is a real, verifiable provision with a page link. Temporal applicability is checked with
the commencement / repeal data recorded for each act (India Code metadata): for criminal matters,
conduct before 1 July 2024 is flagged before any BNS/BNSS/BSA provision is applied, and the old
codes are flagged for conduct after their repeal. Results are suggestions for the lawyer to accept,
reject or edit — never a determination of guilt or liability.
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import cases, config, db, docintel, library, llm, prompts, retrieval

NEW_CRIMINAL = ("Bharatiya Nyaya Sanhita", "Bharatiya Nagarik Suraksha Sanhita", "Bharatiya Sakshya Adhiniyam")
OLD_EQUIVALENT = {"Bharatiya Nyaya Sanhita": "Indian Penal Code, 1860",
                  "Bharatiya Nagarik Suraksha Sanhita": "Code of Criminal Procedure, 1973",
                  "Bharatiya Sakshya Adhiniyam": "Indian Evidence Act, 1872"}


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    for rx, order in ((r"^(\d{4})-(\d{1,2})-(\d{1,2})", "ymd"), (r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", "dmy")):
        m = re.match(rx, s)
        if m:
            a, b, c = map(int, m.groups())
            try:
                return date(a, b, c) if order == "ymd" else date(c, b, a)
            except ValueError:
                return None
    return None


def temporal_flags(act_title: str, category: str, conduct: date | None, meta: dict) -> list[str]:
    flags = []
    start = _parse_date(meta.get("enforcement_date"))
    if meta.get("repealed"):
        flags.append(f"{act_title} is recorded as repealed ({meta.get('note') or 'see India Code'}). "
                     "It may still govern conduct before the repeal — check the repeal-and-savings provision of the new law.")
    if start and conduct and conduct < start:
        old = next((o for n, o in OLD_EQUIVALENT.items() if n in act_title), None)
        flags.append(f"The conduct date ({conduct.isoformat()}) is before this Act came into force ({start.isoformat()})"
                     + (f"; the {old} may apply instead" if old else "") + ". Check the transitional provisions before applying it.")
    if category == "criminal" and not conduct and any(n in act_title for n in NEW_CRIMINAL + ("Indian Penal Code",)):
        flags.append("The date of the alleged conduct is not established — confirm whether the old codes (IPC/CrPC) "
                     "or the new codes (BNS/BNSS, in force from 1 July 2024) apply.")
    return flags


def _material(case: dict, doc_ids: list[str] | None, extra: str) -> str:
    parts = [cases.dossier(case)]
    if extra:
        parts.append(f"ADDITIONAL TEXT:\n{extra[:6000]}")
    rows = db.conn().execute("SELECT id, title, doc_type FROM documents WHERE case_id=? AND status='ready' "
                             "AND json_extract(meta,'$.derived_from') IS NULL ORDER BY created_at", (case["id"],)).fetchall()
    for r in rows:
        if doc_ids is not None and r["id"] not in doc_ids:
            continue
        tr = docintel.derivative(r["id"], "translation_en")
        pages = json.loads(tr["content"]) if tr else docintel.extracted_pages(r["id"])
        parts.append(f"<<<DOCUMENT {r['title']} ({r['doc_type']})\n" + "\n".join(pages)[:8000] + "\nDOCUMENT>>>")
    return "\n\n".join(parts)[:30000]


def suggest(case_id: str, *, doc_ids: list[str] | None = None, extra: str = "", progress=lambda *a: None) -> dict:
    case = cases.get_case(case_id)
    if not case:
        raise ValueError("Case not found")
    progress(0, 3, "Reading the case facts")
    info = llm.chat_json([{"role": "user", "content": prompts.PROV_FACTS.format(material=_material(case, doc_ids, extra))}],
                         default={})
    queries = [q for q in info.get("search_queries") or [] if isinstance(q, str) and q.strip()][:5]
    if not queries:
        queries = [case.get("description") or case["title"]]
    category = (info.get("category") or case.get("case_type") or "").lower()
    conduct = _parse_date(info.get("conduct_date"))

    progress(1, 3, "Searching the bare acts")
    acts_ids = {r[0] for r in db.conn().execute("SELECT id FROM documents WHERE collection='bare_act' AND status='ready'")}
    cand: dict[int, retrieval.Hit] = {}
    for q in queries:
        hits, _ = retrieval.search(q, scope="library", doc_ids=acts_ids, k=8, hint=extra[:500])
        for h in hits:
            cand.setdefault(h.chunk_id, h)
    cands = list(cand.values())[:30]
    if not cands:
        return {"facts": info, "provisions": [], "precedents": [], "conflicts": []}
    progress(2, 3, "Matching facts to provisions")
    cand_txt = "\n\n".join(f"C{i}: {h.header}\n{h.text[:900]}" for i, h in enumerate(cands, 1))
    facts_txt = json.dumps({k: info.get(k) for k in ("category", "conduct_date_text", "state", "subject", "key_facts")},
                           ensure_ascii=False)
    sel = llm.chat_json([{"role": "user", "content": prompts.PROV_SELECT.format(facts=facts_txt, candidates=cand_txt)}],
                        default={"provisions": []})
    now = db.now()
    out = []
    for p in sel.get("provisions") or []:
        m = re.match(r"C(\d+)$", str(p.get("cid", "")).strip())
        if not m or not 1 <= int(m.group(1)) <= len(cands):
            continue  # the model may only pick from the retrieved candidates
        h = cands[int(m.group(1)) - 1]
        meta = library.act_meta(h.title)
        flags = temporal_flags(h.title, category, conduct, meta)
        if p.get("uncertainty"):
            flags.append(f"To establish: {p['uncertainty']}")
        out.append({"act": h.title, "ref": h.label or h.header, "title": h.header.split(" — ", 1)[-1][:200],
                    "chunk_id": h.chunk_id, "doc_id": h.doc_id, "page": h.page_start,
                    "reason": f"[{p.get('relevance', 'medium')}] {str(p.get('why') or '')[:600]}", "flags": flags,
                    "verification": "verified_in_library"})
    with db.tx() as c:
        for p in out:
            c.execute("INSERT INTO case_provisions(id, case_id, act, ref, title, chunk_id, doc_id, page, reason, flags, "
                      "verification, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'suggested', ?, ?) "
                      "ON CONFLICT(case_id, act, ref) DO UPDATE SET reason=excluded.reason, flags=excluded.flags, "
                      "chunk_id=excluded.chunk_id, page=excluded.page, updated_at=excluded.updated_at "
                      "WHERE case_provisions.status='suggested'",
                      (db.new_id("prov_"), case_id, p["act"], p["ref"], p["title"], p["chunk_id"], p["doc_id"], p["page"],
                       p["reason"], json.dumps(p["flags"], ensure_ascii=False), p["verification"], now, now))
    # Precedents: only judgments actually in the library, retrieved by similarity.
    jids = {r[0] for r in db.conn().execute("SELECT id FROM documents WHERE collection='judgment' AND status='ready'")}
    precedents = []
    if jids:
        hits, diag = retrieval.search(" ".join(queries[:2]), scope="library", doc_ids=jids, k=3)
        # Only judgments the reranker considers relevant — a nearest neighbour is not a precedent.
        if diag.get("confidence") is None or diag["confidence"] >= config.RELEVANCE_FLOOR:
            precedents = [{"title": h.title, "label": h.label, "page": h.page_start, "doc_id": h.doc_id, "chunk_id": h.chunk_id,
                           "snippet": h.text[:300]} for h in hits]
    db.audit("provisions.suggest", "case", case_id, suggested=len(out), sent_to_llm=True)
    progress(3, 3, "Done")

    def readable(c) -> str | None:
        """Conflicts must name provisions, not internal candidate ids."""
        if not isinstance(c, str):
            c = " / ".join(map(str, c)) if isinstance(c, (list, tuple)) else str(c)
        c = re.sub(r"\bC(\d{1,2})\b", lambda m: (cands[int(m.group(1)) - 1].label or f"C{m.group(1)}")
                   if 1 <= int(m.group(1)) <= len(cands) else m.group(0), c)
        return c[:300] if re.search(r"[A-Za-z]{4,}", re.sub(r"Section|Article|Order|Rule", "", c)) else None

    conflicts = [x for x in (readable(c) for c in sel.get("conflicts") or []) if x][:5]
    return {"facts": info, "provisions": list_for(case_id), "precedents": precedents, "conflicts": conflicts}


def list_for(case_id: str) -> list[dict]:
    rows = db.conn().execute("SELECT * FROM case_provisions WHERE case_id=? ORDER BY status='rejected', created_at",
                             (case_id,)).fetchall()
    return [db.row_to_dict(r, ("flags",)) for r in rows]


def update(prov_id: str, *, status: str | None, note: str | None, ref: str | None) -> dict | None:
    row = db.row_to_dict(db.conn().execute("SELECT * FROM case_provisions WHERE id=?", (prov_id,)).fetchone(), ("flags",))
    if not row:
        return None
    fields: dict = {}
    if ref and ref.strip() and ref.strip() != row["ref"]:
        # An edited reference is re-verified against the library.
        found = library.lookup_provision(row["act"], ref.strip())
        fields.update(ref=ref.strip()[:120], verification="verified_in_library" if found["found"] else "not_found",
                      chunk_id=found.get("chunk_id"), page=found.get("page"), status="edited")
    if status in ("accepted", "rejected", "suggested", "edited"):
        fields["status"] = status
    if note is not None:
        fields["note"] = note[:1000]
    if not fields:
        return row
    with db.tx() as c:
        c.execute(f"UPDATE case_provisions SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? WHERE id=?",
                  [*fields.values(), db.now(), prov_id])
    new = db.row_to_dict(db.conn().execute("SELECT * FROM case_provisions WHERE id=?", (prov_id,)).fetchone(), ("flags",))
    if new["status"] == "accepted":
        case = cases.get_case(new["case_id"])
        short = library.act_meta(new["act"]).get("short") or new["act"]
        entry = f"{new['ref']} {short}"
        if case and entry not in (case.get("acts_sections") or ""):
            joined = "; ".join(x for x in [(case.get("acts_sections") or "").strip(), entry] if x)
            try:
                cases.update_case(case["id"], {"acts_sections": joined[:600]})
            except cases.FieldError:
                pass  # incomplete case record: the provision stays accepted here; the user can add it on the form
    db.audit("provisions.review", "provision", prov_id, status=new["status"])
    return new
