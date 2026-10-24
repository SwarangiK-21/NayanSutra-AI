"""Matter (case) management backed by SQLite."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from . import config, db, reference

CASE_TYPES = reference.CATEGORIES
CASE_STATUSES = ["Draft / Pre-filing", "Active / In Court", "Evidence Stage", "Final Arguments",
                 "Reserved for Orders", "Disposed / Closed"]
FIELDS = ["title", "case_type", "case_subtype", "case_number", "filing_number", "fir_number", "cnr_number", "court",
          "state", "district", "police_station", "filing_year", "client_name", "client_role", "opposite_party",
          "opposite_role", "filing_date", "next_hearing_date", "status", "acts_sections", "description", "is_draft"]
LIMITS = {"title": 200, "description": 6000, "acts_sections": 600}
# Names: letters in any script, spaces and the punctuation found in Indian party names (M/s., S/o, &, etc.).
_NAME = re.compile(r"^[\w\s.,&'()/\-]+$", re.UNICODE)
_IDENT = re.compile(r"^[\w\s./\-()]+$", re.UNICODE)


class FieldError(ValueError):
    """Validation failure tied to one form field; `code` lets the UI show a translated message."""

    def __init__(self, field: str, code: str, message: str):
        super().__init__(message)
        self.field, self.code = field, code


def _clean(data: dict) -> dict:
    out = {}
    for k in FIELDS:
        if k in data and data[k] is not None:
            v = str(data[k]).strip()[: LIMITS.get(k, 200)]
            out[k] = v or None if k in ("filing_date", "next_hearing_date", "filing_year") else v
    if "case_type" in out and out["case_type"] not in CASE_TYPES:
        out["case_type"] = "Civil"
    if "status" in out and out["status"] not in CASE_STATUSES:
        out["status"] = "Active / In Court"
    if "is_draft" in out:
        out["is_draft"] = 1 if str(out["is_draft"]).lower() in ("1", "true", "yes", "on") else 0
    return out


def auto_title(d: dict) -> str:
    """Title derived from the parties ("Client v. Opposite party") when the user did not type one."""
    parts = [d.get("client_name"), d.get("opposite_party")]
    if all(parts):
        return f"{parts[0]} v. {parts[1]}"[:200]
    return (next((p for p in parts if p), None) or "Untitled matter")[:200]


def validate(d: dict, *, final: bool) -> dict:
    """Field rules. Drafts may be incomplete; anything entered must still be well-formed."""
    for f in ("client_name", "opposite_party"):
        v = d.get(f)
        if v and (len(v) < 2 or not _NAME.match(v)):
            raise FieldError(f, "invalid_name", "Use letters, spaces and . , & ' ( ) / - only (at least 2 characters).")
    for f in ("case_number", "filing_number", "fir_number", "police_station"):
        v = d.get(f)
        if v and not _IDENT.match(v):
            raise FieldError(f, "invalid_identifier", "Use letters, digits, spaces and . / - ( ) only.")
    if d.get("cnr_number"):
        cnr = reference.parse_cnr(d["cnr_number"])
        if not cnr["valid"]:
            raise FieldError("cnr_number", "invalid_cnr", cnr["error"])
        d["cnr_number"] = cnr["cnr"]
    if d.get("filing_year") is not None:
        try:
            y = int(d["filing_year"])
        except ValueError:
            raise FieldError("filing_year", "invalid_year", "Filing year must be a 4-digit year.") from None
        if not 1950 <= y <= date.today().year:
            raise FieldError("filing_year", "invalid_year", f"Filing year must be between 1950 and {date.today().year}.")
        d["filing_year"] = y
    if d.get("state") and d["state"] not in reference.STATES:
        raise FieldError("state", "invalid_state", "Choose a state or union territory from the list.")
    if d.get("district") and d.get("state") in reference.DISTRICTS and d["district"] not in reference.DISTRICTS[d["state"]]:
        raise FieldError("district", "invalid_district", "Choose a district of the selected state.")
    if not d.get("title"):
        d["title"] = auto_title(d) if (d.get("client_name") or d.get("opposite_party") or not final) else None
    if final:
        if not d.get("title"):
            raise FieldError("title", "required", "Case title is required (or enter both party names).")
        if not d.get("client_name"):
            raise FieldError("client_name", "required", "Client / party name is required.")
    return d


META_FIELDS = ("id", "title", "case_type", "case_subtype", "case_number", "cnr_number", "court", "state", "district",
               "status", "filing_year", "next_hearing_date", "is_draft", "created_at", "updated_at", "org_id")


def list_cases(principal) -> list[dict]:
    """Cases the caller is on (with their access level). Administrators also see the rest of their organisation's
    case list — metadata only — so they can assign people."""
    if not principal.signed_in:
        return []
    rows = db.conn().execute(
        "SELECT c.*, cm.access, (SELECT COUNT(*) FROM documents d WHERE d.case_id=c.id) AS n_docs "
        "FROM cases c JOIN case_members cm ON cm.case_id = c.id AND cm.user_id = ? "
        "WHERE c.org_id = ? ORDER BY c.updated_at DESC", (principal.user_id, principal.org_id)).fetchall()
    out = [dict(r) for r in rows]
    if principal.role == "client":
        for c in out:
            c["access"] = "client"
    if principal.role == "admin":
        mine = {c["id"] for c in out}
        for r in db.conn().execute(f"SELECT {', '.join(META_FIELDS)} FROM cases WHERE org_id=? ORDER BY updated_at DESC",
                                   (principal.org_id,)):
            if r["id"] not in mine:
                out.append({**dict(r), "access": None, "n_docs": None})
    return out


def case_metadata(case_id: str) -> dict | None:
    row = db.conn().execute(f"SELECT {', '.join(META_FIELDS)} FROM cases WHERE id=?", (case_id,)).fetchone()
    return dict(row) if row else None


def get_case(case_id: str, with_notes: bool = True) -> dict | None:
    row = db.conn().execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return None
    case = dict(row)
    case["notes"] = [dict(n) for n in db.conn().execute(
        "SELECT * FROM case_notes WHERE case_id=? ORDER BY created_at DESC", (case_id,))] if with_notes else []
    return case


def create_case(data: dict) -> dict:
    d = _clean(data)
    d = validate(d, final=not d.get("is_draft"))
    d.setdefault("case_type", "Civil")
    if d.get("is_draft") and "status" not in d:
        d["status"] = "Draft / Pre-filing"
    prefix = d["case_type"][:3].upper()
    cid = db.new_id(f"CASE-{prefix}-")[:20]
    now = db.now()
    ident = db.current()
    # The creator becomes the case owner (database trigger trg_case_owner); the case belongs to their organisation.
    owner = {"org_id": ident.org_id or db.DEFAULT_ORG, "created_by": ident.user_id}
    cols = ["id", *d.keys(), *owner.keys(), "created_at", "updated_at"]
    with db.tx() as c:
        c.execute(f"INSERT INTO cases({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [cid, *d.values(), *owner.values(), now, now])
    db.audit("case.create", "case", cid, draft=bool(d.get("is_draft")))
    return get_case(cid)


def update_case(case_id: str, data: dict) -> dict | None:
    current = get_case(case_id)
    if not current:
        return None
    d = _clean(data)
    if not d:
        return current
    merged = {**{k: current.get(k) for k in FIELDS}, **d}
    # A title that was generated from the parties follows them when they change; a typed title is kept.
    title_was_auto = current.get("title") == auto_title(current)
    if "title" not in d and title_was_auto:
        merged["title"] = None
    final = not merged.get("is_draft")
    d = {k: v for k, v in validate(dict(merged), final=final).items()
         if k in d or (k == "title" and (title_was_auto or not current.get("title")))}
    with db.tx() as c:
        cur = c.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in d)}, updated_at=? WHERE id=?",
                        [*d.values(), db.now(), case_id])
    db.audit("case.update", "case", case_id, fields=sorted(d))
    return get_case(case_id) if cur.rowcount else None


def delete_case(case_id: str) -> bool:
    """Delete a case with all its files and records. The caller's right to do so is checked by the API
    (owner or organisation administrator); the clean-up itself runs as internal work so no file is left behind."""
    from .ingest import delete_document

    with db.system():
        for r in db.conn().execute("SELECT id FROM documents WHERE case_id=?", (case_id,)).fetchall():
            delete_document(r["id"])
        with db.tx() as c:
            ok = c.execute("DELETE FROM cases WHERE id=?", (case_id,)).rowcount > 0
    if ok:
        db.audit("case.delete", "case", case_id)
    return ok


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
    idents = ", ".join(f"{lbl}: {case[k]}" for k, lbl in (
        ("case_number", "Case no."), ("filing_number", "Filing no."), ("cnr_number", "CNR"),
        ("fir_number", "FIR no."), ("police_station", "Police station")) if case.get(k))
    where = ", ".join(x for x in (case.get("district"), case.get("state")) if x)
    lines = [
        f"Title: {case['title']}  |  Type: {case['case_type']}{' — ' + case['case_subtype'] if case.get('case_subtype') else ''}",
        f"Identifiers: {idents or '—'}",
        f"Court/Forum: {case.get('court') or '—'}{' (' + where + ')' if where else ''}  |  Status: {case.get('status') or '—'}",
        f"Client: {case.get('client_name') or '—'} ({case.get('client_role') or '—'})  |  "
        f"Opposite party: {case.get('opposite_party') or '—'} ({case.get('opposite_role') or '—'})",
        f"Filing date: {case.get('filing_date') or '—'}  |  Filing year: {case.get('filing_year') or '—'}  |  "
        f"Next hearing: {case.get('next_hearing_date') or '—'}",
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
    if db.backend() == "postgres":
        # Never seed demo data into Supabase: an empty Supabase database usually means the data migration
        # (scripts/migrate_to_supabase.py) has not been run yet, and seeding would create duplicates of real cases.
        if config.DB_PATH.exists():
            import logging
            logging.getLogger(__name__).warning(
                "Supabase has no cases but a local database exists at %s — run scripts/migrate_to_supabase.py.",
                config.DB_PATH)
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
        try:
            case = create_case(it)
        except FieldError:
            continue
        for n in it.get("notes", [])[:5]:
            add_note(case["id"], n.get("content", ""), n.get("author", "Advocate"))
