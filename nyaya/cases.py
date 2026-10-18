"""Matter (case) management backed by SQLite."""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import config, db

CASE_TYPES = ["Civil", "Criminal", "Family", "Revenue"]
CASE_STATUSES = ["Draft / Pre-filing", "Active / In Court", "Evidence Stage", "Final Arguments",
                 "Reserved for Orders", "Disposed / Closed"]
FIELDS = ["title", "case_type", "case_number", "court", "client_name", "client_role", "opposite_party",
          "filing_date", "next_hearing_date", "status", "acts_sections", "description"]
LIMITS = {"title": 200, "description": 6000, "acts_sections": 600}


def _clean(data: dict) -> dict:
    out = {}
    for k in FIELDS:
        if k in data and data[k] is not None:
            v = str(data[k]).strip()[: LIMITS.get(k, 200)]
            out[k] = v or None if k in ("filing_date", "next_hearing_date") else v
    if "case_type" in out and out["case_type"] not in CASE_TYPES:
        out["case_type"] = "Civil"
    if "status" in out and out["status"] not in CASE_STATUSES:
        out["status"] = "Active / In Court"
    return out


def list_cases() -> list[dict]:
    rows = db.conn().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM documents d WHERE d.case_id=c.id) AS n_docs "
        "FROM cases c ORDER BY c.updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_case(case_id: str) -> dict | None:
    row = db.conn().execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return None
    case = dict(row)
    case["notes"] = [dict(n) for n in db.conn().execute(
        "SELECT * FROM case_notes WHERE case_id=? ORDER BY created_at DESC", (case_id,))]
    return case


def create_case(data: dict) -> dict:
    d = _clean(data)
    if not d.get("title"):
        raise ValueError("Case title is required")
    d.setdefault("case_type", "Civil")
    prefix = d["case_type"][:3].upper()
    cid = db.new_id(f"CASE-{prefix}-")[:20]
    now = db.now()
    cols = ["id", *d.keys(), "created_at", "updated_at"]
    with db.tx() as c:
        c.execute(f"INSERT INTO cases({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [cid, *d.values(), now, now])
    return get_case(cid)


def update_case(case_id: str, data: dict) -> dict | None:
    d = _clean(data)
    if not d:
        return get_case(case_id)
    with db.tx() as c:
        cur = c.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in d)}, updated_at=? WHERE id=?",
                        [*d.values(), db.now(), case_id])
    return get_case(case_id) if cur.rowcount else None


def delete_case(case_id: str) -> bool:
    from .ingest import delete_document

    for r in db.conn().execute("SELECT id FROM documents WHERE case_id=?", (case_id,)).fetchall():
        delete_document(r["id"])
    with db.tx() as c:
        return c.execute("DELETE FROM cases WHERE id=?", (case_id,)).rowcount > 0


def add_note(case_id: str, content: str, author: str = "Advocate") -> dict | None:
    content = content.strip()[:4000]
    if not content or not get_case(case_id):
        return None
    with db.tx() as c:
        c.execute("INSERT INTO case_notes(id, case_id, author, content, created_at) VALUES(?,?,?,?,?)",
                  (db.new_id("note_"), case_id, (author or "Advocate").strip()[:80], content, db.now()))
        c.execute("UPDATE cases SET updated_at=? WHERE id=?", (db.now(), case_id))
    return get_case(case_id)


def dossier(case: dict, documents: list[dict] | None = None) -> str:
    """Compact case facts for the LLM."""
    lines = [
        f"Title: {case['title']}  |  Type: {case['case_type']}  |  No.: {case.get('case_number') or '—'}",
        f"Court/Forum: {case.get('court') or '—'}  |  Status: {case.get('status') or '—'}",
        f"Client: {case.get('client_name') or '—'} ({case.get('client_role') or '—'})  |  "
        f"Opposite party: {case.get('opposite_party') or '—'}",
        f"Filing date: {case.get('filing_date') or '—'}  |  Next hearing: {case.get('next_hearing_date') or '—'}",
        f"Provisions noted: {case.get('acts_sections') or '—'}",
        f"Facts / background: {case.get('description') or '—'}",
    ]
    notes = case.get("notes") or []
    if notes:
        lines.append("Recent notes: " + " | ".join(f"{n['created_at'][:10]}: {n['content'][:200]}" for n in notes[:3]))
    if documents:
        lines.append("Documents on file: " + "; ".join(f"[D{i}] {d['title']} ({d['doc_type']})"
                                                       for i, d in enumerate(documents, 1)))
    return "\n".join(lines)


def seed_if_empty() -> None:
    """First run: import the existing cases.json (or demo matters) so the app is not empty."""
    if db.conn().execute("SELECT COUNT(*) FROM cases").fetchone()[0]:
        return
    src = config.DATA_DIR / "cases.json"
    items = []
    if src.exists():
        try:
            items = json.loads(src.read_text("utf-8"))
        except ValueError:
            items = []
    today = date.today()
    for i, it in enumerate(items[:20]):
        it = dict(it)
        # Demo matters carried 2024 hearing dates; move them into the near future.
        it["next_hearing_date"] = (today + timedelta(days=7 + 6 * i)).isoformat()
        case = create_case(it)
        for n in it.get("notes", [])[:5]:
            add_note(case["id"], n.get("content", ""), n.get("author", "Advocate"))
