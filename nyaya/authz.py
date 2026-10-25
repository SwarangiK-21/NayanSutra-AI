"""Central authorization: roles, the permission matrix, and case-level access.

Every API endpoint declares the permission it needs; object endpoints also check the caller's access to the
case the object belongs to. Deny by default: a permission or case action that is not listed here is refused.
The database enforces the same rules again with row-level security (supabase/migrations/…05).

Two personas (organisation roles). Public sign-up gives "advocate" (shown as Lawyer); only an administrator can
make someone an administrator — a user never chooses their own role:
    guest     signed out, or signed in without access yet: public legal information only
    advocate  Lawyer: creates matters; works on the matters they are a member of; research, drafting, calendar
    admin     Administrator: everything a lawyer can do, plus users, roles, case assignments, settings and the
              audit log; sees the organisation's case list, but not case contents unless they are on the case
              (adding themselves is audited)

Case access (per matter, from case_members): owner > advocate. Guests never have case access.
The former "client" and "auditor" roles were retired (supabase/migrations/…07).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import db

ROLES = ("guest", "advocate", "admin")
ASSIGNABLE_ROLES = ("advocate", "admin")
CASE_ACCESS = ("owner", "advocate")

_SIGNED_IN = {"advocate", "admin"}
_WORKERS = {"advocate", "admin"}

PERMISSIONS: dict[str, set[str]] = {
    "library.read": {"guest", *_WORKERS},          # bare acts, awareness, limitation tool, news
    "account.self": _SIGNED_IN,                    # own profile, password, MFA, sessions, preferences
    "notifications.read": _SIGNED_IN,
    "research.use": _WORKERS,                      # AI legal research on the library (+ cases they can access)
    "cases.list": _WORKERS,
    "case.create": _WORKERS,
    "drafting.use": _WORKERS,
    "casesearch.use": _WORKERS,
    "forum.edit": _WORKERS,
    "directory.read": _WORKERS,                    # look up colleagues to add to a case
    "calendar.use": _WORKERS,                      # own hearings, deadlines and tasks with reminders
    "library.import": _WORKERS,                    # add an official India Code act to the shared library
    "users.manage": {"admin"},
    "settings.manage": {"admin"},
    "integrations.manage": {"admin"},
    "integrations.read": _WORKERS,
    "audit.read": {"admin"},                       # security logging stays; only administrators read it
}

CASE_ACTIONS: dict[str, set[str]] = {
    "case.read": {"owner", "advocate"},
    "case.update": {"owner", "advocate"},
    "case.delete": {"owner"},
    "case.members": {"owner"},
    "notes": {"owner", "advocate"},
    "documents.read": {"owner", "advocate"},
    "documents.upload": {"owner", "advocate"},
    "documents.delete": {"owner", "advocate"},
    "documents.ai": {"owner", "advocate"},
    "chat": {"owner", "advocate"},
    "drafts": {"owner", "advocate"},
    "provisions": {"owner", "advocate"},
    "records": {"owner", "advocate"},
}
# Organisation administrators may do these on any case of their organisation without being on it.
ADMIN_CASE_ACTIONS = {"case.delete", "case.members", "case.meta"}

# Roles that must use a second factor before reaching any data (Supabase Auth mode).
MFA_REQUIRED_DEFAULT = ("admin", "advocate")


class Denied(Exception):
    """403 — the caller is known but not allowed (message is safe to show)."""


class NotFound(Exception):
    """404 — used instead of 403 when revealing that a record exists would itself leak information."""


@dataclass
class Principal:
    user_id: str | None = None
    email: str | None = None
    display_name: str = ""
    org_id: str | None = None
    role: str = "guest"
    session_id: str | None = None      # public id of the app session
    session_state: str = "none"        # none | active | mfa_pending | mfa_enroll | recovery
    aal: str = "aal1"
    mfa_enabled: bool = False
    local: bool = False                # local single-user operator (no Supabase Auth configured)
    ip: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def signed_in(self) -> bool:
        return bool(self.user_id) and self.session_state == "active"

    def can(self, permission: str) -> bool:
        role = self.role if self.session_state == "active" else "guest"
        return role in PERMISSIONS.get(permission, set())

    def identity(self) -> db.Identity:
        active = self.session_state == "active"
        return db.Identity(user_id=self.user_id if active else None, org_id=self.org_id,
                           role=self.role if active else "guest", ip=self.ip)


GUEST = Principal()


def require(p: Principal, permission: str) -> None:
    if not p.can(permission):
        raise Denied("You do not have permission to do this." if p.signed_in else "Please sign in to continue.")


# ── Case access ──────────────────────────────────────────────────────────────
def _cap(role: str, access: str | None) -> str | None:
    if access is None or role not in _WORKERS or access not in CASE_ACCESS:
        return None
    return access


def case_access(p: Principal, case_id: str | None) -> str | None:
    """Effective access level of the caller on a case (None = no access)."""
    if not case_id or not p.signed_in:
        return None
    row = db.conn().execute(
        "SELECT cm.access, c.org_id FROM case_members cm JOIN cases c ON c.id = cm.case_id "
        "WHERE cm.case_id = ? AND cm.user_id = ?", (case_id, p.user_id)).fetchone()
    if not row or row["org_id"] != p.org_id:
        return None
    return _cap(p.role, row["access"])


def case_org(case_id: str) -> str | None:
    with db.system():
        row = db.conn().execute("SELECT org_id FROM cases WHERE id=?", (case_id,)).fetchone()
    return row["org_id"] if row else None


def can_case(p: Principal, case_id: str | None, action: str) -> bool:
    if not p.signed_in or not case_id:
        return False
    if action in ADMIN_CASE_ACTIONS and p.role == "admin" and case_org(case_id) == p.org_id:
        return True
    return case_access(p, case_id) in CASE_ACTIONS.get(action, set())


def require_case(p: Principal, case_id: str | None, action: str) -> str:
    """Raise NotFound when the caller has no access at all (do not reveal the case exists), Denied when the
    caller is on the case but the action needs a higher level. Returns the effective access."""
    if not p.signed_in:
        raise Denied("Please sign in to continue.")
    access = case_access(p, case_id)
    if action in ADMIN_CASE_ACTIONS and p.role == "admin" and case_id and case_org(case_id) == p.org_id:
        return access or "admin"
    if access is None:
        raise NotFound("Case not found")
    if access not in CASE_ACTIONS.get(action, set()):
        raise Denied("Your access to this case does not allow this.")
    return access


def accessible_case_ids(p: Principal) -> list[str]:
    if not p.signed_in or p.role not in _WORKERS:
        return []
    rows = db.conn().execute(
        "SELECT cm.case_id FROM case_members cm JOIN cases c ON c.id = cm.case_id "
        "WHERE cm.user_id = ? AND c.org_id = ?", (p.user_id, p.org_id))
    return [r["case_id"] for r in rows]


def team_case_ids(p: Principal) -> list[str]:
    """Cases where the caller is on the case team (owner/advocate), e.g. for drafts and records."""
    if not p.signed_in or p.role not in ("advocate", "admin"):
        return []
    rows = db.conn().execute(
        "SELECT cm.case_id FROM case_members cm JOIN cases c ON c.id = cm.case_id "
        "WHERE cm.user_id = ? AND c.org_id = ? AND cm.access IN ('owner','advocate')", (p.user_id, p.org_id))
    return [r["case_id"] for r in rows]


def compatible_access(role: str, access: str) -> bool:
    """Which case access levels a person with this organisation role may be given."""
    return role in _WORKERS and access in CASE_ACCESS


# ── Documents and other objects reached by id ───────────────────────────────
def document_case(doc_id: str) -> tuple[str | None, str | None] | None:
    """(collection, case_id) of a document, or None if it does not exist."""
    with db.system():
        row = db.conn().execute("SELECT collection, case_id FROM documents WHERE id=?", (doc_id,)).fetchone()
    return (row["collection"], row["case_id"]) if row else None


def require_document(p: Principal, doc_id: str, action: str = "documents.read") -> tuple[str, str | None]:
    """Library documents are public reading; case documents need the given case action."""
    info = document_case(doc_id)
    if not info:
        raise NotFound("Document not found")
    collection, case_id = info
    if collection != "case":
        if action != "documents.read":
            raise Denied("Library documents are managed by the administrator.")
        require(p, "library.read")
        return collection, None
    try:
        require_case(p, case_id, action)
    except NotFound:
        raise NotFound("Document not found") from None
    return collection, case_id
