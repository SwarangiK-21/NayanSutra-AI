"""Server-side sessions (the browser only holds an opaque, HttpOnly cookie).

* The cookie carries 256 random bits; the database stores only their SHA-256, so a database leak gives no session.
* Absolute and idle expiry; the token is rotated on every privilege change (sign-in, MFA, password reset), which
  defeats session fixation.
* Sessions can be listed and revoked by their owner; revocation is immediate on this instance and at most
  CACHE_SECONDS later on others.
* Supabase access/refresh tokens for a session are kept in Supabase Vault (Postgres) or in memory (SQLite / tests);
  they never reach the browser.
* CSRF: state-changing requests must carry a header derived from the session token with a server key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timedelta

from . import config, db, security

COOKIE = "nyaya_session"
CSRF_HEADER = "x-nyaya-csrf"
CACHE_SECONDS = 15
SHORT_STATES = {"mfa_pending": 10, "mfa_enroll": 30, "recovery": 15}   # minutes a half-finished sign-in may last
MAX_MFA_ATTEMPTS = 5

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    return hmac.new(security.secret_key(), f"csrf:{token}".encode(), hashlib.sha256).hexdigest()[:32]


def csrf_ok(token: str | None, header: str | None) -> bool:
    return bool(token and header) and hmac.compare_digest(csrf_for(token), header)


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def invalidate(session_hash: str | None = None, user_id: str | None = None) -> None:
    with _cache_lock:
        if session_hash:
            _cache.pop(session_hash, None)
        if user_id:
            for k in [k for k, (_, v) in _cache.items() if v.get("user_id") == user_id]:
                _cache.pop(k, None)


# ── Token store for Supabase session tokens ─────────────────────────────────
class _MemoryTokens:
    def __init__(self):
        self._d: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, name: str, secret: str) -> str:
        ref = secrets.token_hex(16)
        with self._lock:
            self._d[ref] = secret
        return ref

    def get(self, ref: str) -> str | None:
        with self._lock:
            return self._d.get(ref)

    def update(self, ref: str, secret: str) -> None:
        with self._lock:
            self._d[ref] = secret

    def delete(self, ref: str) -> None:
        with self._lock:
            self._d.pop(ref, None)


class _VaultTokens:
    """Supabase Vault (encrypted at rest by Supabase), reached through SECURITY DEFINER wrappers that only work in
    the system context. A small in-process cache avoids a decrypt on every request."""

    def __init__(self):
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, name: str, secret: str) -> str:
        with db.system():
            ref = str(db.conn().execute("SELECT private.token_put(?, ?) AS id", (name, secret)).fetchone()["id"])
        with self._lock:
            self._cache[ref] = secret
        return ref

    def get(self, ref: str) -> str | None:
        with self._lock:
            if ref in self._cache:
                return self._cache[ref]
        with db.system():
            row = db.conn().execute("SELECT private.token_get(CAST(? AS uuid)) AS v", (ref,)).fetchone()
        v = row["v"] if row else None
        if v:
            with self._lock:
                self._cache[ref] = v
        return v

    def update(self, ref: str, secret: str) -> None:
        with db.system():
            db.conn().execute("SELECT private.token_update(CAST(? AS uuid), ?)", (ref, secret))
        with self._lock:
            self._cache[ref] = secret

    def delete(self, ref: str) -> None:
        with self._lock:
            self._cache.pop(ref, None)
        with db.system():
            db.conn().execute("SELECT private.token_delete(CAST(? AS uuid))", (ref,))


_memory_tokens = _MemoryTokens()
_vault_tokens = _VaultTokens()


def token_store():
    return _vault_tokens if db.backend() == "postgres" else _memory_tokens


def save_tokens(row: dict, tokens: dict) -> str:
    blob = json.dumps({k: tokens.get(k) for k in ("access_token", "refresh_token", "expires_at")})
    ref = row.get("token_ref")
    store = token_store()
    if ref:
        store.update(ref, blob)
        return ref
    return store.put(f"nyaya_session:{row['id'][:24]}:{secrets.token_hex(4)}", blob)


def load_tokens(row: dict) -> dict | None:
    ref = row.get("token_ref")
    if not ref:
        return None
    blob = token_store().get(ref)
    return json.loads(blob) if blob else None


# ── Sessions ─────────────────────────────────────────────────────────────────
def create(user_id: str, *, org_id: str | None, state: str = "active", aal: str = "aal1", ip: str = "",
           user_agent: str = "", tokens: dict | None = None, sb_session_id: str | None = None,
           meta: dict | None = None) -> tuple[str, dict]:
    """New session; returns (cookie token, row). The token itself is never stored."""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    minutes = SHORT_STATES.get(state)
    expires = now + (timedelta(minutes=minutes) if minutes else timedelta(hours=config.SESSION_TTL_HOURS))
    row = {"id": _hash(token), "public_id": secrets.token_urlsafe(9), "user_id": user_id, "org_id": org_id,
           "state": state, "aal": aal, "created_at": _ts(now), "last_seen_at": _ts(now), "expires_at": _ts(expires),
           "ip": (ip or "")[:64], "user_agent": (user_agent or "")[:300], "sb_session_id": sb_session_id,
           "token_ref": None, "access_expires_at": None, "meta": json.dumps(meta or {})}
    if tokens:
        row["token_ref"] = save_tokens(row, tokens)
        row["access_expires_at"] = _ts(datetime.fromtimestamp(int(tokens.get("expires_at") or time.time() + 3600)))
    with db.system():
        with db.tx() as c:
            cols = list(row)
            c.execute(f"INSERT INTO app_sessions({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                      [row[k] for k in cols])
    if state == "active":   # signing in counts as using the account (data retention)
        from . import retention
        retention.mark_active(user_id, now)
    return token, row


def lookup(token: str | None) -> dict | None:
    """Valid session row for a cookie token (expired, idle or revoked sessions are deleted and return None)."""
    if not token or len(token) > 200:
        return None
    h = _hash(token)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(h)
        if hit and now - hit[0] < CACHE_SECONDS:
            row = hit[1]
            if (_parse(row["expires_at"]) or datetime.min) > datetime.now():
                return row
    with db.system():
        r = db.conn().execute(
            "SELECT s.*, p.email, p.display_name, p.status AS profile_status, p.mfa_enabled, m.role "
            "FROM app_sessions s JOIN profiles p ON p.user_id = s.user_id "
            "LEFT JOIN memberships m ON m.user_id = s.user_id AND m.org_id = s.org_id WHERE s.id = ?", (h,)).fetchone()
    if not r:
        return None
    row = dict(r)
    dt_now = datetime.now()
    idle_limit = timedelta(minutes=config.SESSION_IDLE_MINUTES)
    last = _parse(row["last_seen_at"]) or datetime.min
    if row["revoked_at"] or row["profile_status"] != "active" or (_parse(row["expires_at"]) or datetime.min) <= dt_now \
            or (row["state"] == "active" and dt_now - last > idle_limit):
        revoke(row, reason="expired" if not row["revoked_at"] else "revoked")
        return None
    if dt_now - last > timedelta(seconds=60):   # sliding idle window, written at most once a minute
        row["last_seen_at"] = _ts(dt_now)
        with db.system():
            with db.tx() as c:
                c.execute("UPDATE app_sessions SET last_seen_at=? WHERE id=?", (row["last_seen_at"], h))
        if last.date() != dt_now.date() and row.get("user_id"):   # account activity for data retention, once a day
            from . import retention
            retention.mark_active(row["user_id"], dt_now)
    with _cache_lock:
        _cache[h] = (now, row)
    return row


def update(row: dict, **fields) -> dict:
    if not fields:
        return row
    with db.system():
        with db.tx() as c:
            c.execute(f"UPDATE app_sessions SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                      [*fields.values(), row["id"]])
    invalidate(row["id"])
    return {**row, **fields}


def rotate(row: dict, **fields) -> tuple[str, dict]:
    """Issue a new token for the same session (after any privilege change) and drop the old one."""
    token = secrets.token_urlsafe(32)
    new_id = _hash(token)
    if "state" in fields:
        minutes = SHORT_STATES.get(fields["state"])
        expires = datetime.now() + (timedelta(minutes=minutes) if minutes else timedelta(hours=config.SESSION_TTL_HOURS))
        fields["expires_at"] = _ts(expires)
    with db.system():
        with db.tx() as c:
            sets = {"id": new_id, "last_seen_at": _ts(datetime.now()), **fields}
            c.execute(f"UPDATE app_sessions SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                      [*sets.values(), row["id"]])
    invalidate(row["id"])
    return token, {**row, **sets}


def revoke(row: dict, reason: str = "signed_out") -> None:
    with db.system():
        with db.tx() as c:
            c.execute("DELETE FROM app_sessions WHERE id=?", (row["id"],))
    if row.get("token_ref"):
        try:
            token_store().delete(row["token_ref"])
        except Exception:  # the session is gone either way; a leftover vault entry is unusable without it
            pass
    invalidate(row["id"])


def revoke_all(user_id: str, except_id: str | None = None) -> int:
    with db.system():
        rows = [dict(r) for r in db.conn().execute("SELECT id, token_ref FROM app_sessions WHERE user_id=?", (user_id,))]
    n = 0
    for r in rows:
        if r["id"] != except_id:
            revoke(r, "revoked")
            n += 1
    invalidate(user_id=user_id)
    return n


def list_for(user_id: str) -> list[dict]:
    with db.system():
        rows = db.conn().execute("SELECT public_id, state, created_at, last_seen_at, expires_at, ip, user_agent "
                                 "FROM app_sessions WHERE user_id=? ORDER BY last_seen_at DESC", (user_id,))
        return [dict(r) for r in rows]


def by_public_id(user_id: str, public_id: str) -> dict | None:
    with db.system():
        r = db.conn().execute("SELECT * FROM app_sessions WHERE user_id=? AND public_id=?", (user_id, public_id)).fetchone()
    return dict(r) if r else None


def purge_expired() -> int:
    """Housekeeping: drop sessions past their absolute expiry."""
    with db.system():
        rows = [dict(r) for r in db.conn().execute("SELECT id, token_ref FROM app_sessions WHERE expires_at < ?",
                                                    (_ts(datetime.now()),))]
    for r in rows:
        revoke(r, "expired")
    return len(rows)
