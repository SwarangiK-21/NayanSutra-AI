"""Account flows: sign-in, MFA, recovery codes, sign-up, password reset/change, sessions, invitations.

Supabase Auth does the identity work (see supabase_auth.py). This module adds what the backend must own in a
backend-for-frontend design: the app session, the role (from an administrator's invitation — never from the user),
account lockout across IPs, a stricter password policy, MFA recovery codes, and in-app security notifications.
Every outcome is written to the audit log without passwords, codes or tokens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from . import authz, config, db, security, sessions
from . import supabase_auth as sb

log = logging.getLogger(__name__)

LOCAL_USER_ID = "00000000-0000-4000-8000-000000000001"
LOCAL_EMAIL = "operator@localhost"
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # recovery codes: no 0/O, 1/I/L confusion
N_RECOVERY_CODES = 10
GENERIC_SENT = "If the address can be used here, an e-mail with a link is on its way. Check your inbox (and spam)."

# Frequently used and leaked passwords; Supabase's own leaked-password check needs a paid plan.
_COMMON = {
    "password", "password1", "password123", "passw0rd", "p@ssw0rd", "p@ssword", "qwerty", "qwerty123", "qwertyuiop",
    "123456", "1234567", "12345678", "123456789", "1234567890", "12345678910", "111111", "000000", "abc123",
    "iloveyou", "admin", "admin123", "administrator", "welcome", "welcome1", "welcome123", "letmein", "monkey",
    "dragon", "football", "cricket", "sachin", "india123", "india@123", "bharat", "nyaysutra", "nyaysutra123",
    "advocate", "advocate123", "lawyer", "lawyer123", "justice", "court123", "changeme", "secret", "default",
    "sunshine", "princess", "master", "superman", "trustno1", "zaq12wsx", "1q2w3e4r", "asdfghjkl", "computer",
}


class AccountError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


def mode() -> str:
    return config.auth_mode()


def norm_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL.match(e) or len(e) > 254:
        raise AccountError("Enter a valid e-mail address.", 400, "invalid_email")
    return e


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def password_problems(pw: str, email: str = "") -> list[str]:
    """Server-side password rules (Supabase's default minimum is only 6 characters)."""
    out = []
    if len(pw) < 12:
        out.append("Use at least 12 characters.")
    if len(pw.encode("utf-8")) > 72:   # bcrypt (used by Supabase) ignores everything after 72 bytes
        out.append("Use at most 72 bytes (about 72 letters).")
    low = pw.lower()
    if low in _COMMON or re.sub(r"[\d!@#$%^&*._-]+$", "", low) in _COMMON:
        out.append("This password is too common.")
    if len(set(pw)) < 5:
        out.append("Use more varied characters.")
    local = email.split("@")[0].lower() if email else ""
    if len(local) >= 4 and local in low:
        out.append("Do not use your e-mail name in the password.")
    return out


def _check_password_policy(pw: str, email: str = "") -> None:
    problems = password_problems(pw, email)
    if problems:
        raise AccountError(" ".join(problems), 400, "weak_password")


# ── Brute-force protection (per e-mail, shared by every IP and app instance) ─
def _tkey(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


def _locked(key: str) -> bool:
    with db.system():
        r = db.conn().execute("SELECT locked_until FROM auth_throttle WHERE key=?", (key,)).fetchone()
    return bool(r and r["locked_until"] and r["locked_until"] > db.now())


def _fail(key: str) -> None:
    now = datetime.now()
    window = timedelta(minutes=config.LOGIN_LOCK_MINUTES)
    with db.system():
        with db.tx() as c:
            r = c.execute("SELECT failures, window_start FROM auth_throttle WHERE key=?", (key,)).fetchone()
            if not r or datetime.fromisoformat(r["window_start"]) < now - window:
                failures, start = 1, now
            else:
                failures, start = r["failures"] + 1, datetime.fromisoformat(r["window_start"])
            locked = (now + window).isoformat(timespec="seconds") if failures >= config.LOGIN_MAX_FAILURES else None
            c.execute("INSERT INTO auth_throttle(key, failures, window_start, locked_until) VALUES(?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET failures=excluded.failures, window_start=excluded.window_start, "
                      "locked_until=excluded.locked_until",
                      (key, failures, start.isoformat(timespec="seconds"), locked))


def _clear(key: str) -> None:
    with db.system():
        with db.tx() as c:
            c.execute("DELETE FROM auth_throttle WHERE key=?", (key,))


def _locked_error() -> AccountError:
    return AccountError(f"Too many failed attempts. Try again in {config.LOGIN_LOCK_MINUTES} minutes, "
                        "or reset your password.", 429, "locked")


# ── Profiles, memberships, invitations ──────────────────────────────────────
def membership(user_id: str) -> tuple[str | None, str | None]:
    with db.system():
        r = db.conn().execute("SELECT org_id, role FROM memberships WHERE user_id=? ORDER BY created_at LIMIT 1",
                              (user_id,)).fetchone()
    return (r["org_id"], r["role"]) if r else (None, None)


def claim_unowned(user_id: str, org_id: str) -> int:
    """Bootstrap / local mode: records created before accounts existed become this person's."""
    with db.system():
        with db.tx() as c:
            n = c.execute("INSERT INTO case_members(case_id, user_id, access, added_by, created_at) "
                          "SELECT c.id, ?, 'owner', ?, ? FROM cases c WHERE c.org_id = ? AND NOT EXISTS "
                          "(SELECT 1 FROM case_members cm WHERE cm.case_id = c.id)",
                          (user_id, user_id, db.now(), org_id)).rowcount
            c.execute("UPDATE chats SET user_id=? WHERE user_id IS NULL AND COALESCE(org_id, ?) = ?", (user_id, org_id, org_id))
            c.execute("UPDATE drafts SET created_by=? WHERE created_by IS NULL AND COALESCE(org_id, ?) = ?",
                      (user_id, org_id, org_id))
            c.execute("UPDATE source_records SET created_by=? WHERE created_by IS NULL AND kind='case_record'", (user_id,))
            c.execute("UPDATE cases SET created_by=? WHERE created_by IS NULL AND org_id=?", (user_id, org_id))
    return n


def ensure_profile(user: dict) -> dict:
    """Create/refresh the profile for a verified Supabase user and apply a pending invitation (the only way a role
    is given). Returns the profile with org_id and role (None when the person has no access yet)."""
    uid = str(user["id"])
    email = (user.get("email") or "").lower()
    name = ((user.get("user_metadata") or {}).get("display_name") or "").strip()[:80] or email.split("@")[0]
    now = db.now()
    with db.system():
        prof = db.conn().execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()
        if not prof:
            other = db.conn().execute("SELECT user_id FROM profiles WHERE lower(email)=?", (email,)).fetchone()
            if other:
                log.warning("Profile e-mail conflict for a new account (existing profile %s)", other["user_id"])
                raise AccountError("This e-mail address is linked to another account record. Ask an administrator.",
                                   409, "email_conflict")
            with db.tx() as c:
                c.execute("INSERT INTO profiles(user_id, email, display_name, status, created_at, updated_at) "
                          "VALUES(?,?,?,?,?,?)", (uid, email, name, "active", now, now))
            prof = db.conn().execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()
        elif prof["email"] != email:
            with db.tx() as c:
                c.execute("UPDATE profiles SET email=?, updated_at=? WHERE user_id=?", (email, now, uid))
        org, role = membership(uid)
        if not org:
            inv = db.conn().execute(
                "SELECT * FROM invitations WHERE lower(email)=? AND accepted_at IS NULL AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1", (email, now)).fetchone()
            if inv:
                with db.tx() as c:
                    c.execute("INSERT INTO memberships(org_id, user_id, role, created_at, created_by) VALUES(?,?,?,?,?)",
                              (inv["org_id"], uid, inv["role"], now, inv["invited_by"]))
                    c.execute("UPDATE invitations SET accepted_at=?, accepted_by=? WHERE id=?", (now, uid, inv["id"]))
                org, role = inv["org_id"], inv["role"]
                if json.loads(inv["meta"] or "{}").get("claim_unowned"):
                    claim_unowned(uid, org)
                db.audit("invitation.accept", "invitation", inv["id"], role=role)
            elif config.SIGNUP_MODE == "open":
                # Public registration: always a Lawyer, never an administrator (only an invitation can do that).
                with db.tx() as c:
                    c.execute("INSERT INTO memberships(org_id, user_id, role, created_at, created_by) VALUES(?,?,?,?,?)",
                              (db.DEFAULT_ORG, uid, "advocate", now, None))
                org, role = db.DEFAULT_ORG, "advocate"
                db.audit("membership.self_registered", "user", uid, role="advocate")
    out = dict(prof)
    out.update(org_id=org, role=role)
    return out


def ensure_local_operator() -> None:
    """Local single-user mode: the person at this computer is the organisation's administrator and owns the matters."""
    now = db.now()
    with db.system():
        with db.tx() as c:
            c.execute("INSERT INTO profiles(user_id, email, display_name, status, created_at, updated_at) "
                      "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO NOTHING",
                      (LOCAL_USER_ID, LOCAL_EMAIL, "Local operator", "active", now, now))
            c.execute("INSERT INTO memberships(org_id, user_id, role, created_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(org_id, user_id) DO NOTHING", (db.DEFAULT_ORG, LOCAL_USER_ID, "admin", now))
    claim_unowned(LOCAL_USER_ID, db.DEFAULT_ORG)


def create_invitation(org_id: str, email: str, role: str, invited_by: str, claim: bool = False) -> dict:
    email = norm_email(email)
    if role not in authz.ASSIGNABLE_ROLES:
        raise AccountError("Choose a valid role.")
    now = datetime.now()
    inv = {"id": db.new_id("inv_"), "org_id": org_id, "email": email, "role": role, "invited_by": invited_by,
           "created_at": now.isoformat(timespec="seconds"),
           "expires_at": (now + timedelta(days=config.INVITE_DAYS)).isoformat(timespec="seconds"),
           "meta": json.dumps({"claim_unowned": True} if claim else {})}
    with db.tx() as c:
        c.execute("DELETE FROM invitations WHERE org_id=? AND lower(email)=? AND accepted_at IS NULL", (org_id, email))
        c.execute("INSERT INTO invitations(id, org_id, email, role, invited_by, created_at, expires_at, meta) "
                  "VALUES(?,?,?,?,?,?,?,?)", tuple(inv.values()))
    db.audit("invitation.create", "invitation", inv["id"], role=role, email=mask_email(email))
    return {k: v for k, v in inv.items() if k != "meta"}


def principal_for_user(user_id: str | None) -> authz.Principal | None:
    """The user's current principal (as a background job sees it: active profile, current role), or None."""
    if not user_id:
        return None
    with db.system():
        r = db.conn().execute(
            "SELECT p.user_id, p.email, p.display_name, p.status, p.mfa_enabled, m.org_id, m.role FROM profiles p "
            "LEFT JOIN memberships m ON m.user_id = p.user_id WHERE p.user_id=? ORDER BY m.created_at LIMIT 1",
            (user_id,)).fetchone()
    if not r or r["status"] != "active" or not r["role"]:
        return None
    return authz.Principal(user_id=r["user_id"], email=r["email"], display_name=r["display_name"] or "",
                           org_id=r["org_id"], role=r["role"], session_state="active",
                           mfa_enabled=bool(r["mfa_enabled"]), local=r["user_id"] == LOCAL_USER_ID)


# ── Sessions → principal ─────────────────────────────────────────────────────
def principal_from_row(row: dict | None, ip: str | None = None) -> authz.Principal:
    if not row:
        return authz.Principal(ip=ip)
    return authz.Principal(
        user_id=row["user_id"], email=row.get("email"), display_name=row.get("display_name") or "",
        org_id=row.get("org_id"), role=row.get("role") or "guest", session_id=row.get("public_id"),
        session_state=row["state"], aal=row.get("aal") or "aal1", mfa_enabled=bool(row.get("mfa_enabled")),
        local=row["user_id"] == LOCAL_USER_ID, ip=ip, meta={"row": row})


def _mfa_required(role: str | None) -> bool:
    return mode() == "supabase" and (role or "") in config.MFA_REQUIRED_ROLES


def access_token(row: dict) -> str:
    """A valid Supabase access token for this session, refreshed when it is about to expire. A refresh that Supabase
    refuses (signed out elsewhere, account disabled or deleted) ends the app session too."""
    tokens = sessions.load_tokens(row)
    if not tokens or not tokens.get("access_token"):
        raise AccountError("Please sign in again to do this.", 401, "reauth")
    exp = int(tokens.get("expires_at") or 0)
    if exp - 60 > datetime.now().timestamp():
        return tokens["access_token"]
    try:
        fresh = sb.refresh(tokens["refresh_token"])
    except sb.AuthError as exc:
        if exc.status == 503:
            raise AccountError(str(exc), 503, exc.code) from exc
        sessions.revoke(row, "supabase_refresh_failed")
        db.audit("auth.session_ended", "session", row.get("public_id"), reason=exc.code or "refresh_failed")
        raise AccountError("Your session has ended. Please sign in again.", 401, "session_ended") from exc
    sessions.save_tokens(row, fresh)
    sessions.update(row, access_expires_at=datetime.fromtimestamp(int(fresh.get("expires_at") or 0)).isoformat(
        timespec="seconds"), aal=sb.claims(fresh["access_token"]).get("aal", row.get("aal") or "aal1"))
    return fresh["access_token"]


def revalidate(row: dict) -> None:
    """Called on requests once the Supabase access token has expired: proves the Supabase session is still alive."""
    if mode() != "supabase" or not row.get("token_ref"):
        return
    exp = row.get("access_expires_at")
    if exp and exp > db.now():
        return
    access_token(row)


# ── Local single-user mode ───────────────────────────────────────────────────
def local_session(ip: str, ua: str) -> tuple[str, dict]:
    ensure_local_operator()
    token, row = sessions.create(LOCAL_USER_ID, org_id=db.DEFAULT_ORG, ip=ip, user_agent=ua)
    return token, sessions.lookup(token) or row


def local_login(password: str, ip: str, ua: str) -> tuple[str, dict]:
    key = _tkey("local", "operator")
    if _locked(key):
        raise _locked_error()
    if not security.check_password(password):
        _fail(key)
        db.audit("auth.login", "user", LOCAL_USER_ID, outcome="failed", method="app_password")
        raise AccountError("Incorrect password", 401, "invalid_credentials")
    _clear(key)
    token, row = local_session(ip, ua)
    db.audit("auth.login", "user", LOCAL_USER_ID, method="app_password")
    return token, row


# ── Supabase sign-in ─────────────────────────────────────────────────────────
def _device(ip: str, ua: str) -> str:
    return hashlib.sha256(f"{ua}|{'.'.join(ip.split('.')[:3])}".encode()).hexdigest()[:16]


def _new_device_notice(user_id: str, ip: str, ua: str) -> None:
    dev = _device(ip, ua)
    with db.system():
        known = json.loads(db.get_user_pref("known_devices", "[]", user_id=user_id) or "[]")
        if dev in known:
            return
        db.set_user_pref("known_devices", json.dumps((known + [dev])[-20:]), user_id=user_id)
    if known:   # not the very first sign-in
        browser = re.sub(r"\s+", " ", ua)[:80] or "unknown browser"
        db.notify("security", "New sign-in to your account",
                  f"A sign-in from a new device or network ({browser}, {ip}). If this was not you, change your password "
                  "and sign out all other sessions in Account settings.", "#/account", recipients=[user_id],
                  dedupe_key=f"newdev:{dev}")


def _activate(row: dict, ip: str, ua: str) -> None:
    _new_device_notice(row["user_id"], ip, ua)


def login(email: str, password: str, ip: str, ua: str) -> tuple[str, dict]:
    email = norm_email(email)
    if len(password) > 200:
        raise AccountError("Incorrect e-mail or password.", 401, "invalid_credentials")
    key = _tkey("login", email)
    if _locked(key):
        db.audit("auth.login", "user", None, outcome="locked", email=mask_email(email))
        raise _locked_error()
    try:
        res = sb.sign_in(email, password)
    except sb.AuthError as exc:
        if exc.code in ("invalid_credentials", "invalid_grant", ""):
            _fail(key)
        db.audit("auth.login", "user", None, outcome="failed", email=mask_email(email), reason=exc.code or "error")
        status = 429 if exc.status == 429 else 503 if exc.status == 503 else \
            403 if exc.code in ("email_not_confirmed", "user_banned") else 401
        raise AccountError(str(exc), status, exc.code) from exc
    _clear(key)
    return _start_session(res, ip, ua, method="password")


def _start_session(res: dict, ip: str, ua: str, *, method: str, state: str | None = None) -> tuple[str, dict]:
    user = res.get("user") or {}
    if not user.get("email_confirmed_at") and not user.get("confirmed_at"):
        # Enforced here as well, in case e-mail confirmation is switched off in the Supabase dashboard.
        _sign_out_quietly(res.get("access_token"))
        raise AccountError(sb._MESSAGES["email_not_confirmed"], 403, "email_not_confirmed")
    prof = ensure_profile(user)
    if prof["status"] != "active":
        _sign_out_quietly(res.get("access_token"))
        db.audit("auth.login", "user", prof["user_id"], outcome="disabled")
        raise AccountError(sb._MESSAGES["user_banned"], 403, "user_banned")
    c = sb.claims(res["access_token"])
    has_factor = bool(sb.verified_factors(user))
    if state is None:
        state = "mfa_pending" if has_factor and c.get("aal") != "aal2" else \
            "mfa_enroll" if not has_factor and _mfa_required(prof["role"]) else "active"
    with db.system():
        with db.tx() as cx:
            cx.execute("UPDATE profiles SET mfa_enabled=? WHERE user_id=?", (1 if has_factor else 0, prof["user_id"]))
    token, row = sessions.create(prof["user_id"], org_id=prof["org_id"], state=state, aal=c.get("aal", "aal1"),
                                 ip=ip, user_agent=ua, tokens=res, sb_session_id=c.get("session_id"))
    with db.as_identity(db.Identity(user_id=prof["user_id"], org_id=prof["org_id"], role=prof["role"] or "guest", ip=ip)):
        db.audit("auth.login", "user", prof["user_id"], method=method, state=state)
    if state == "active":
        _activate(row, ip, ua)
    return token, sessions.lookup(token) or row


def _sign_out_quietly(access: str | None) -> None:
    if access:
        try:
            sb.sign_out(access, "local")
        except sb.AuthError:
            pass


def logout(row: dict) -> None:
    tokens = sessions.load_tokens(row)
    if tokens and tokens.get("access_token"):
        _sign_out_quietly(tokens["access_token"])
    sessions.revoke(row, "signed_out")
    db.audit("auth.logout", "session", row.get("public_id"))


# ── MFA ──────────────────────────────────────────────────────────────────────
def _user(row: dict) -> dict:
    try:
        return sb.get_user(access_token(row))
    except sb.AuthError as exc:
        raise AccountError(str(exc), exc.status, exc.code) from exc


def mfa_verify(row: dict, code: str, ip: str, ua: str) -> tuple[str, dict]:
    if row["state"] != "mfa_pending":
        raise AccountError("No second-factor check is pending.", 400)
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        raise AccountError("Enter the 6-digit code from your authenticator app.", 400, "invalid_code")
    if row.get("mfa_attempts", 0) >= sessions.MAX_MFA_ATTEMPTS:
        sessions.revoke(row, "mfa_attempts")
        db.audit("auth.mfa_verify", "user", row["user_id"], outcome="locked")
        raise AccountError("Too many wrong codes. Please sign in again.", 401, "mfa_locked")
    factors = sb.verified_factors(_user(row))
    if not factors:
        raise AccountError(sb._MESSAGES["mfa_factor_not_found"], 400, "mfa_factor_not_found")
    access = access_token(row)
    try:
        ch = sb.mfa_challenge(access, factors[0]["id"])
        res = sb.mfa_verify(access, factors[0]["id"], ch["id"], code)
    except sb.AuthError as exc:
        sessions.update(row, mfa_attempts=row.get("mfa_attempts", 0) + 1)
        db.audit("auth.mfa_verify", "user", row["user_id"], outcome="failed", reason=exc.code)
        raise AccountError(str(exc), 401 if exc.status != 429 else 429, exc.code) from exc
    sessions.save_tokens(row, res)
    token, row = sessions.rotate(row, state="active", aal=sb.claims(res["access_token"]).get("aal", "aal2"),
                                 mfa_attempts=0)
    db.audit("auth.mfa_verify", "user", row["user_id"])
    _activate(row, ip, ua)
    return token, sessions.lookup(token) or row


def mfa_enroll_start(row: dict) -> dict:
    if row["state"] not in ("active", "mfa_enroll"):
        raise AccountError("Sign in first.", 401)
    user = _user(row)
    if sb.verified_factors(user):
        raise AccountError("An authenticator is already set up. Remove it first to replace it.", 400, "mfa_exists")
    access = access_token(row)
    for f in user.get("factors") or []:   # an abandoned earlier attempt
        if f.get("status") == "unverified":
            try:
                sb.mfa_unenroll(access, f["id"])
            except sb.AuthError:
                pass
    try:
        res = sb.mfa_enroll(access, f"NyaySutra {secrets.token_hex(2)}")
    except sb.AuthError as exc:
        raise AccountError(str(exc), exc.status, exc.code) from exc
    totp = res.get("totp") or {}
    qr = totp.get("qr_code") or ""
    if qr.lstrip().startswith("<"):   # Supabase returns SVG markup; an <img> data URI cannot run scripts
        qr = "data:image/svg+xml;base64," + base64.b64encode(qr.encode("utf-8")).decode()
    return {"factor_id": res["id"], "qr_code": qr, "secret": totp.get("secret"), "uri": totp.get("uri")}


def _hash_code(user_id: str, code: str) -> str:
    norm = re.sub(r"[^A-Z0-9]", "", code.upper())
    return hmac.new(security.secret_key(), f"rc:{user_id}:{norm}".encode(), hashlib.sha256).hexdigest()


def new_recovery_codes(user_id: str) -> list[str]:
    codes = ["".join(secrets.choice(_ALPHABET) for _ in range(10)) for _ in range(N_RECOVERY_CODES)]
    codes = [f"{c[:5]}-{c[5:]}" for c in codes]
    now = db.now()
    with db.system():
        with db.tx() as c:
            c.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
            for code in codes:
                c.execute("INSERT INTO recovery_codes(user_id, code_hash, created_at) VALUES(?,?,?)",
                          (user_id, _hash_code(user_id, code), now))
    return codes


def recovery_codes_left(user_id: str) -> int:
    with db.system():
        return db.conn().execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id=? AND used_at IS NULL",
                                 (user_id,)).fetchone()[0]


def mfa_enroll_finish(row: dict, factor_id: str, code: str, ip: str, ua: str) -> tuple[str, dict, list[str]]:
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"[\w-]{8,64}", factor_id or ""):
        raise AccountError("Enter the 6-digit code from your authenticator app.", 400, "invalid_code")
    access = access_token(row)
    try:
        ch = sb.mfa_challenge(access, factor_id)
        res = sb.mfa_verify(access, factor_id, ch["id"], code)
    except sb.AuthError as exc:
        db.audit("auth.mfa_enroll", "user", row["user_id"], outcome="failed", reason=exc.code)
        raise AccountError(str(exc), exc.status, exc.code) from exc
    sessions.save_tokens(row, res)
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE profiles SET mfa_enabled=1, updated_at=? WHERE user_id=?", (db.now(), row["user_id"]))
    codes = new_recovery_codes(row["user_id"])
    was_enrolling = row["state"] == "mfa_enroll"
    token, row = sessions.rotate(row, state="active", aal=sb.claims(res["access_token"]).get("aal", "aal2"))
    # Every other session was established without the new factor.
    sessions.revoke_all(row["user_id"], except_id=row["id"])
    db.audit("auth.mfa_enroll", "user", row["user_id"])
    db.notify("security", "Two-step verification turned on",
              "An authenticator app now protects your account. Keep your recovery codes somewhere safe.",
              "#/account", recipients=[row["user_id"]])
    if was_enrolling:
        _activate(row, ip, ua)
    return token, sessions.lookup(token) or row, codes


def use_recovery_code(row: dict, code: str, ip: str, ua: str) -> tuple[str, dict]:
    """Lost authenticator: a one-time recovery code removes the factor; the person must set up a new one."""
    if row["state"] != "mfa_pending":
        raise AccountError("No second-factor check is pending.", 400)
    if row.get("mfa_attempts", 0) >= sessions.MAX_MFA_ATTEMPTS:
        sessions.revoke(row, "mfa_attempts")
        raise AccountError("Too many wrong codes. Please sign in again.", 401, "mfa_locked")
    h = _hash_code(row["user_id"], code or "")
    with db.system():
        with db.tx() as c:
            used = c.execute("UPDATE recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                             (db.now(), row["user_id"], h)).rowcount
    if not used:
        sessions.update(row, mfa_attempts=row.get("mfa_attempts", 0) + 1)
        db.audit("auth.recovery_code", "user", row["user_id"], outcome="failed")
        raise AccountError("That recovery code is not valid or was already used.", 401, "invalid_code")
    for f in sb.verified_factors(_user(row)):
        try:
            sb.admin_delete_factor(row["user_id"], f["id"])
        except sb.AuthError as exc:
            raise AccountError(str(exc), exc.status, exc.code) from exc
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE profiles SET mfa_enabled=0, updated_at=? WHERE user_id=?", (db.now(), row["user_id"]))
    role = membership(row["user_id"])[1]
    token, row = sessions.rotate(row, state="mfa_enroll" if _mfa_required(role) else "active", mfa_attempts=0)
    sessions.revoke_all(row["user_id"], except_id=row["id"])
    left = recovery_codes_left(row["user_id"])
    db.audit("auth.recovery_code", "user", row["user_id"], remaining=left)
    db.notify("security", "A recovery code was used",
              f"Your authenticator was removed after a recovery code was used ({left} codes left). Set up a new "
              "authenticator now. If this was not you, change your password immediately.", "#/account",
              recipients=[row["user_id"]])
    if row["state"] == "active":
        _activate(row, ip, ua)
    return token, sessions.lookup(token) or row


def _check_totp(row: dict, code: str) -> None:
    """Fresh proof of the second factor for sensitive changes."""
    code = re.sub(r"\s", "", code or "")
    factors = sb.verified_factors(_user(row))
    if not factors:
        raise AccountError(sb._MESSAGES["mfa_factor_not_found"], 400, "mfa_factor_not_found")
    if not re.fullmatch(r"\d{6}", code):
        raise AccountError("Enter the 6-digit code from your authenticator app.", 400, "invalid_code")
    access = access_token(row)
    try:
        ch = sb.mfa_challenge(access, factors[0]["id"])
        res = sb.mfa_verify(access, factors[0]["id"], ch["id"], code)
    except sb.AuthError as exc:
        raise AccountError(str(exc), 401 if exc.status != 429 else 429, exc.code) from exc
    sessions.save_tokens(row, res)


def regenerate_recovery_codes(row: dict, code: str) -> list[str]:
    _check_totp(row, code)
    codes = new_recovery_codes(row["user_id"])
    db.audit("auth.recovery_codes_regenerated", "user", row["user_id"])
    return codes


def mfa_disable(row: dict, code: str) -> None:
    role = membership(row["user_id"])[1]
    if _mfa_required(role):
        raise AccountError("Your role requires two-step verification, so it cannot be turned off.", 403, "mfa_required")
    _check_totp(row, code)
    access = access_token(row)
    for f in sb.verified_factors(_user(row)):
        sb.mfa_unenroll(access, f["id"])
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE profiles SET mfa_enabled=0, updated_at=? WHERE user_id=?", (db.now(), row["user_id"]))
            c.execute("DELETE FROM recovery_codes WHERE user_id=?", (row["user_id"],))
    db.audit("auth.mfa_disable", "user", row["user_id"])
    db.notify("security", "Two-step verification turned off", "Your account is now protected by the password only.",
              "#/account", recipients=[row["user_id"]])


# ── Sign-up, e-mail links, password reset ───────────────────────────────────
def _callback_url(flow: str) -> str:
    base = config.PUBLIC_URL or f"http://127.0.0.1:{config.APP_PORT}"
    return f"{base}/auth/callback?flow={flow}"


def signup(email: str, password: str, display_name: str, ip: str) -> str:
    """Returns the PKCE verifier to keep in an HttpOnly cookie until the e-mail link comes back."""
    email = norm_email(email)
    _check_password_policy(password, email)
    display_name = re.sub(r"[\x00-\x1f<>]", "", display_name or "").strip()[:80]
    verifier, challenge = sb.pkce_pair()
    try:
        sb.sign_up(email, password, display_name, challenge, _callback_url("signup"))
    except sb.AuthError as exc:
        if exc.code in ("weak_password", "over_email_send_rate_limit", "over_request_rate_limit", "signup_disabled"):
            raise AccountError(str(exc), exc.status, exc.code) from exc
        # Anything else (including "already registered") gets the same answer, so sign-up cannot be used to
        # discover which addresses have accounts.
        log.info("sign-up not completed: %s", exc.code)
    db.audit("auth.signup", "user", None, email=mask_email(email))
    return verifier


def resend(email: str) -> str:
    email = norm_email(email)
    try:
        sb.resend_signup(email, _callback_url("signup"))
    except sb.AuthError as exc:
        if exc.status == 429:
            raise AccountError(str(exc), 429, exc.code) from exc
    return GENERIC_SENT


def forgot(email: str, ip: str) -> str:
    email = norm_email(email)
    key = _tkey("reset", email)
    if _locked(key):
        return ""   # same answer as success
    _fail(key)      # at most LOGIN_MAX_FAILURES reset e-mails per address per window
    verifier, challenge = sb.pkce_pair()
    try:
        sb.recover(email, challenge, _callback_url("recovery"))
    except sb.AuthError as exc:
        if exc.status == 503:
            raise AccountError(str(exc), 503, exc.code) from exc
        log.info("password reset not sent: %s", exc.code)
    db.audit("auth.password_reset_requested", "user", None, email=mask_email(email))
    return verifier


def callback(auth_code: str, flow: str, verifier: str | None, ip: str, ua: str) -> tuple[str, dict, str]:
    """E-mail link returned with a one-time code. Returns (session token, row, where to send the browser)."""
    if not verifier or not re.fullmatch(r"[\w-]{20,200}", auth_code or ""):
        raise AccountError(sb._MESSAGES["bad_code_verifier"], 400, "bad_code_verifier")
    try:
        res = sb.exchange_code(auth_code, verifier)
    except sb.AuthError as exc:
        raise AccountError(str(exc), 400, exc.code) from exc
    if flow == "recovery":
        token, row = _start_session(res, ip, ua, method="recovery_link", state="recovery")
        return token, row, "/#/reset-password"
    token, row = _start_session(res, ip, ua, method="email_link")
    return token, row, "/#/dashboard"


def reset_password(row: dict, new_password: str) -> None:
    if row["state"] != "recovery":
        raise AccountError("Open the reset link from your e-mail again.", 400)
    _check_password_policy(new_password, row.get("email") or "")
    access = access_token(row)
    try:
        sb.update_password(access, new_password)
    except sb.AuthError as exc:
        raise AccountError(str(exc), exc.status, exc.code) from exc
    try:   # every Supabase session of this user, not just this one
        sb.sign_out(access, "global")
    except sb.AuthError:
        pass
    n = sessions.revoke_all(row["user_id"])
    db.audit("auth.password_reset", "user", row["user_id"], sessions_revoked=n)
    db.notify("security", "Your password was reset", "All sessions were signed out. If you did not do this, contact "
              "your administrator immediately.", "#/account", recipients=[row["user_id"]])


def change_password(row: dict, current: str, new_password: str) -> tuple[str, dict]:
    email = row.get("email") or ""
    _check_password_policy(new_password, email)
    key = _tkey("login", email)
    if _locked(key):
        raise _locked_error()
    try:   # proof of the current password; the extra Supabase session is ended straight away
        fresh = sb.sign_in(email, current)
    except sb.AuthError as exc:
        if exc.status in (429, 503):
            raise AccountError(str(exc), exc.status, exc.code) from exc
        _fail(key)
        db.audit("auth.password_change", "user", row["user_id"], outcome="failed")
        raise AccountError("The current password is incorrect.", 401, "invalid_credentials") from exc
    _sign_out_quietly(fresh.get("access_token"))
    _clear(key)
    access = access_token(row)   # this session's own token (keeps its MFA assurance level)
    try:
        sb.update_password(access, new_password)
    except sb.AuthError as exc:
        raise AccountError(str(exc), exc.status, exc.code) from exc
    try:
        sb.sign_out(access, "others")
    except sb.AuthError:
        pass
    sessions.revoke_all(row["user_id"], except_id=row["id"])
    token, row = sessions.rotate(row)
    db.audit("auth.password_change", "user", row["user_id"])
    db.notify("security", "Your password was changed", "Other sessions were signed out. If you did not do this, reset "
              "your password and contact your administrator.", "#/account", recipients=[row["user_id"]])
    return token, sessions.lookup(token) or row
