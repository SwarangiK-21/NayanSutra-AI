"""FastAPI server: JSON API + Server-Sent Events chat stream + static frontend.

Every request is bound to a principal (the signed-in user, or a guest) before the endpoint runs; the database sees
the same identity (db.Identity → request.jwt.claims). Each endpoint declares its permission (authz.require) and
object endpoints check the caller's access to the case the object belongs to (authz.require_case/require_document).
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import threading
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import (accounts, agent, authz, awareness, cases, casesearch, citations, config, db, docintel, drafting,
               embeddings, events, export, filescan, indiacode, ingest, integrations, jobs, llm, mailer, ocr, privacy,
               provisions, reference, retrieval, search, security, sessions, storage, tasks, tools)
from . import supabase_auth as sb
from .authz import Principal
from .vectorindex import index

log = logging.getLogger("nyaya")

_state = {"models_ready": False, "llm": None, "llm_checked": 0.0}
_scheduler: integrations.Scheduler | None = None
PRE_COOKIE = "nyaya_pre"     # random value that anchors the CSRF token before anyone has signed in
PKCE_COOKIE = "nyaya_pkce"   # verifier for an e-mail link (sign-up confirmation / password reset)


def _warm():
    with db.system():
        index.load()      # vectors → RAM (background, so the UI is up immediately)
    embeddings.warm_up()  # load ONNX models
    _state["models_ready"] = True


def _submit_ingest(doc_id: str, p: Principal | None = None) -> dict:
    """Queue background processing (durable job; authorization happened when the file was uploaded)."""
    return ingest.queue(doc_id, owner=p.user_id if p else None, org_id=p.org_id if p else None)


def _sweep_temp_files(max_age_hours: int = 6) -> int:
    """Upload staging folders are deleted after each request; this removes any left by a crash."""
    import shutil
    root, n = Path(tempfile.gettempdir()), 0
    cutoff = time.time() - max_age_hours * 3600
    for d in root.glob("nyaya_up_*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def _apply_retention() -> None:
    """Data minimisation: delete conversations older than each organisation's retention period (if one is set)."""
    with db.system():
        rows = db.conn().execute("SELECT org_id, value FROM org_settings WHERE key='chat_retention_days'").fetchall()
        for r in rows:
            if not (r["value"] or "").isdigit():
                continue
            cutoff = (date.today() - timedelta(days=int(r["value"]))).isoformat()
            with db.tx() as c:
                n = c.execute("DELETE FROM chats WHERE COALESCE(org_id, ?) = ? AND updated_at < ?",
                              (db.DEFAULT_ORG, r["org_id"], cutoff)).rowcount
                n += c.execute("DELETE FROM search_history WHERE COALESCE(org_id, ?) = ? AND created_at < ?",
                               (db.DEFAULT_ORG, r["org_id"], cutoff)).rowcount
            if n:
                db.audit("retention.purge_chats", "chat", None, deleted=n, older_than_days=int(r["value"]))
        sessions.purge_expired()
        # Finished jobs keep their parameters (which may include typed facts) for 30 days, then go.
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        with db.tx() as c:
            c.execute("DELETE FROM jobs WHERE status NOT IN ('queued','processing') AND COALESCE(finished_at, updated_at) < ?",
                      (cutoff,))
            c.execute("DELETE FROM upload_batches WHERE updated_at < ?", (cutoff,))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    privacy.install_log_filter()
    _sweep_temp_files()
    with db.system():
        db.init()
        cases.seed_if_empty()
        if accounts.mode() == "local":
            accounts.ensure_local_operator()   # also takes ownership of matters created before accounts existed
        _apply_retention()
        stored = db.get_meta("embed_model")
        if stored and stored != embeddings.model_name():
            log.warning("Index was built with %s but EMBED_MODEL is %s — run: python -m nyaya.ingest reembed",
                        stored, embeddings.model_name())
        # Documents left 'processing' without a job (e.g. queued by an older version) get one; jobs themselves are
        # durable and resume on their own.
        for r in db.conn().execute(
                "SELECT d.id, d.uploaded_by FROM documents d WHERE d.status='processing' AND NOT EXISTS "
                "(SELECT 1 FROM jobs j WHERE j.dedupe_key = 'ingest:' || d.id AND j.status IN ('queued','processing'))"
        ).fetchall():
            ingest.queue(r["id"], owner=r["uploaded_by"])
    jobs.start_workers()
    if config.REMINDERS_ENABLED:
        events.start_dispatcher()   # calendar reminders are delivered whether or not anyone has the app open
    threading.Thread(target=_warm, daemon=True).start()
    if config.SYNC_ENABLED:
        _scheduler = integrations.Scheduler()
        _scheduler.start()
    yield
    if _scheduler:
        _scheduler.stop.set()
    events.stop_dispatcher()
    jobs.stop_workers()
    db.close_pool()


# ── Principal (who is calling) — bound once per request, before the endpoint runs ──
def client_ip(request: Request) -> str:
    return request.client.host if request.client else "local"


def _resolve(token: str | None, ip: str) -> dict | None:
    row = sessions.lookup(token)
    if row and row["state"] == "active":
        try:
            accounts.revalidate(row)   # Supabase session still alive? (at most once per access-token lifetime)
        except accounts.AccountError as exc:
            if exc.status == 401:
                return None
            log.warning("Could not revalidate a session: %s", exc.code)   # Supabase unreachable: keep the session
    return row


async def principal(request: Request) -> Principal:
    if hasattr(request.state, "principal"):
        return request.state.principal
    ip = client_ip(request)
    row = await run_in_threadpool(_resolve, request.cookies.get(sessions.COOKIE), ip)
    p = accounts.principal_from_row(row, ip)
    request.state.principal, request.state.session_row = p, row
    db.set_identity(p.identity())   # the database (RLS) sees the same caller in this request's context
    return p


app = FastAPI(title="NyaySutra AI", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
              dependencies=[Depends(principal)])
P = Depends(principal)


# ── Security middleware ──────────────────────────────────────────────────────
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.headers.get("host")
    if not security.host_allowed(host):
        return JSONResponse({"detail": "Host not allowed"}, status_code=400)
    path = request.url.path
    if path.startswith("/api/") and request.method not in SAFE_METHODS:
        if not security.origin_allowed(request.headers.get("origin"), host):
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
        # The header must be derived from one of this browser's own HttpOnly cookies (session, or the pre-sign-in
        # value); another site can neither read those cookies nor send a custom header here.
        header = request.headers.get(sessions.CSRF_HEADER)
        if not any(sessions.csrf_ok(request.cookies.get(name), header) for name in (sessions.COOKIE, PRE_COOKIE)):
            return JSONResponse({"detail": "Missing or invalid CSRF token"}, status_code=403)
    response: Response = await call_next(request)
    if "content-security-policy" not in response.headers:  # endpoints may set a stricter / specific policy
        response.headers["Content-Security-Policy"] = security.CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    # Microphone only for this origin (voice input in Legal Research); the browser still asks the user.
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
    if security.cookie_secure():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if path.startswith("/api/") or path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/"):
        # Revalidate (cheap 304 via ETag) so an updated app.js / app.css is picked up on reload.
        response.headers["Cache-Control"] = "no-cache"
    return response


# ── Errors: never leak internals ─────────────────────────────────────────────
_denied_seen: dict[tuple, float] = {}


@app.exception_handler(authz.Denied)
async def denied(request: Request, exc: authz.Denied):
    p = getattr(request.state, "principal", authz.GUEST)
    key = (p.user_id, request.url.path)
    if p.user_id and time.monotonic() - _denied_seen.get(key, 0) > 60:   # one audit entry per user+path a minute
        if len(_denied_seen) > 5000:
            _denied_seen.clear()
        _denied_seen[key] = time.monotonic()
        await run_in_threadpool(db.audit, "authz.denied", "endpoint", request.url.path[:200], outcome="denied",
                                method=request.method)
    status = 403 if p.signed_in else 401
    return JSONResponse({"detail": str(exc), "code": "forbidden" if p.signed_in else "sign_in"}, status_code=status)


@app.exception_handler(authz.NotFound)
async def not_found(request: Request, exc: authz.NotFound):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(accounts.AccountError)
async def account_error(request: Request, exc: accounts.AccountError):
    return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Internal error"}, status_code=500)


def rate(limit: int, bucket: str, window: float = 60.0):
    def dep(request: Request):
        if not security.limiter.allow(f"{bucket}:{client_ip(request)}", limit, window):
            raise HTTPException(429, "Too many requests — please slow down.")
    return dep


def need(perm: str):
    """Dependency: the caller must hold this permission."""
    def dep(p: Principal = P) -> Principal:
        authz.require(p, perm)
        return p
    return dep


def _row(request: Request) -> dict:
    row = getattr(request.state, "session_row", None)
    if not row:
        raise accounts.AccountError("Please sign in.", 401, "sign_in")
    return row


def _set_cookie(resp: Response, name: str, value: str, max_age: int, *, samesite: str = "strict", path: str = "/") -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite=samesite, secure=security.cookie_secure(),
                    path=path)


def _set_session(resp: Response, token: str, row: dict) -> None:
    minutes = sessions.SHORT_STATES.get(row.get("state", "active"))
    _set_cookie(resp, sessions.COOKIE, token, minutes * 60 if minutes else config.SESSION_TTL_HOURS * 3600)


def _session_body(p: Principal, token: str | None) -> dict:
    mfa_required = accounts.mode() == "supabase" and p.role in config.MFA_REQUIRED_ROLES
    body = {"authenticated": p.signed_in, "state": p.session_state, "auth_mode": accounts.mode(),
            "signup_mode": config.SIGNUP_MODE, "password_required": accounts.mode() == "local" and bool(config.APP_PASSWORD),
            "csrf": sessions.csrf_for(token) if token else None,
            "permissions": sorted(k for k in authz.PERMISSIONS if p.can(k))}
    if p.user_id:
        body.update(user={"id": p.user_id, "email": p.email if not p.local else None, "display_name": p.display_name},
                    role=p.role if p.signed_in else "guest", org_id=p.org_id,
                    mfa={"enabled": p.mfa_enabled, "required": mfa_required}, local=p.local)
    return body


# ── Pages / e-mail link callback ─────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index_page(request: Request, p: Principal = P):
    resp = FileResponse(config.WEB_DIR / "index.html", media_type="text/html")
    resp.headers["Cache-Control"] = "no-store"
    if accounts.mode() == "local" and not config.APP_PASSWORD and not p.user_id:
        token, row = accounts.local_session(client_ip(request), request.headers.get("user-agent", ""))
        _set_session(resp, token, row)
    return resp


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(request: Request, flow: Literal["signup", "recovery"] = "signup", code: str = "",
                  error_code: str = ""):
    if error_code or not code:
        return RedirectResponse(f"/#/login?e={quote(error_code or 'link')}", status_code=303)
    try:
        token, row, target = accounts.callback(code, flow, request.cookies.get(PKCE_COOKIE), client_ip(request),
                                               request.headers.get("user-agent", ""))
    except accounts.AccountError as exc:
        return RedirectResponse(f"/#/login?e={quote(exc.code or 'link')}", status_code=303)
    resp = RedirectResponse(target, status_code=303)
    _set_session(resp, token, row)
    resp.delete_cookie(PKCE_COOKIE, path="/auth/callback")
    return resp


# ── Session and sign-in ──────────────────────────────────────────────────────
@app.get("/api/session")
def session(request: Request, p: Principal = P):
    token = request.cookies.get(sessions.COOKIE) if p.user_id else None
    if not p.user_id and accounts.mode() == "local" and not config.APP_PASSWORD:
        token, row = accounts.local_session(client_ip(request), request.headers.get("user-agent", ""))
        p = accounts.principal_from_row(row, client_ip(request))
        resp = JSONResponse(_session_body(p, token))
        _set_session(resp, token, row)
        return resp
    if token:
        return _session_body(p, token)
    pre = request.cookies.get(PRE_COOKIE)
    if not pre or len(pre) > 100:
        import secrets as _s
        pre = _s.token_urlsafe(24)
    resp = JSONResponse(_session_body(p, pre))
    _set_cookie(resp, PRE_COOKIE, pre, 12 * 3600)
    if request.cookies.get(sessions.COOKIE):   # an ended or expired session: drop the stale cookie
        resp.delete_cookie(sessions.COOKIE, path="/")
    return resp


class LoginIn(BaseModel):
    email: str | None = Field(None, max_length=254)
    password: str = Field(max_length=200)


def _login(request: Request, body: LoginIn) -> JSONResponse:
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    if accounts.mode() == "local":
        token, row = accounts.local_login(body.password, ip, ua)
    else:
        if not body.email:
            raise accounts.AccountError("Enter your e-mail address.", 400, "invalid_email")
        token, row = accounts.login(body.email, body.password, ip, ua)
    p = accounts.principal_from_row(row, ip)
    resp = JSONResponse({"ok": True, **_session_body(p, token)})
    _set_session(resp, token, row)
    return resp


@app.post("/api/auth/login", dependencies=[Depends(rate(10, "login"))])
def auth_login(request: Request, body: LoginIn):
    return _login(request, body)


@app.post("/api/login", dependencies=[Depends(rate(10, "login"))])
def login(request: Request, body: LoginIn):   # kept for older clients
    return _login(request, body)


@app.post("/api/auth/logout")
@app.post("/api/logout")
def logout(request: Request):
    row = getattr(request.state, "session_row", None)
    if row:
        accounts.logout(row)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(sessions.COOKIE, path="/")
    return resp


class SignupIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    display_name: str = Field("", max_length=80)


def _supabase_only() -> None:
    if accounts.mode() != "supabase":
        raise accounts.AccountError("Accounts are not enabled on this installation (local single-user mode).", 400,
                                    "local_mode")


@app.post("/api/auth/signup", dependencies=[Depends(rate(5, "signup", 3600))])
def auth_signup(request: Request, body: SignupIn):
    _supabase_only()
    verifier = accounts.signup(body.email, body.password, body.display_name, client_ip(request))
    resp = JSONResponse({"ok": True, "message": accounts.GENERIC_SENT})
    _set_cookie(resp, PKCE_COOKIE, verifier, 24 * 3600, samesite="lax", path="/auth/callback")
    return resp


class EmailIn(BaseModel):
    email: str = Field(max_length=254)


@app.post("/api/auth/resend", dependencies=[Depends(rate(3, "resend", 3600))])
def auth_resend(body: EmailIn):
    _supabase_only()
    return {"ok": True, "message": accounts.resend(body.email)}


@app.post("/api/auth/password/forgot", dependencies=[Depends(rate(5, "forgot", 3600))])
def auth_forgot(request: Request, body: EmailIn):
    _supabase_only()
    verifier = accounts.forgot(body.email, client_ip(request))
    resp = JSONResponse({"ok": True, "message": accounts.GENERIC_SENT})
    if verifier:
        _set_cookie(resp, PKCE_COOKIE, verifier, 3600, samesite="lax", path="/auth/callback")
    return resp


class PasswordIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/auth/password/reset", dependencies=[Depends(rate(10, "reset"))])
def auth_reset(request: Request, body: PasswordIn):
    accounts.reset_password(_row(request), body.password)
    resp = JSONResponse({"ok": True, "message": "Password changed. Sign in with the new password."})
    resp.delete_cookie(sessions.COOKIE, path="/")
    return resp


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@app.post("/api/auth/password/change", dependencies=[Depends(rate(5, "pwchange"))])
def auth_change_password(request: Request, body: ChangePasswordIn, p: Principal = Depends(need("account.self"))):
    _supabase_only()
    token, row = accounts.change_password(_row(request), body.current_password, body.new_password)
    resp = JSONResponse({"ok": True})
    _set_session(resp, token, row)
    return resp


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=20)


def _session_response(request: Request, token: str, row: dict, extra: dict | None = None) -> JSONResponse:
    p = accounts.principal_from_row(row, client_ip(request))
    resp = JSONResponse({"ok": True, **(extra or {}), **_session_body(p, token)})
    _set_session(resp, token, row)
    return resp


@app.post("/api/auth/mfa/verify", dependencies=[Depends(rate(10, "mfa"))])
def mfa_verify(request: Request, body: CodeIn):
    token, row = accounts.mfa_verify(_row(request), body.code, client_ip(request), request.headers.get("user-agent", ""))
    return _session_response(request, token, row)


@app.post("/api/auth/mfa/recovery", dependencies=[Depends(rate(10, "mfa"))])
def mfa_recovery(request: Request, body: CodeIn):
    token, row = accounts.use_recovery_code(_row(request), body.code, client_ip(request),
                                            request.headers.get("user-agent", ""))
    return _session_response(request, token, row)


@app.post("/api/auth/mfa/enroll", dependencies=[Depends(rate(10, "mfa"))])
def mfa_enroll(request: Request):
    _supabase_only()
    return accounts.mfa_enroll_start(_row(request))


class EnrollVerifyIn(BaseModel):
    factor_id: str = Field(max_length=64)
    code: str = Field(min_length=6, max_length=10)


@app.post("/api/auth/mfa/enroll/verify", dependencies=[Depends(rate(10, "mfa"))])
def mfa_enroll_verify(request: Request, body: EnrollVerifyIn):
    token, row, codes = accounts.mfa_enroll_finish(_row(request), body.factor_id, body.code, client_ip(request),
                                                   request.headers.get("user-agent", ""))
    return _session_response(request, token, row, {"recovery_codes": codes})


@app.post("/api/auth/mfa/disable", dependencies=[Depends(rate(5, "mfa"))])
def mfa_disable(request: Request, body: CodeIn, p: Principal = Depends(need("account.self"))):
    accounts.mfa_disable(_row(request), body.code)
    return {"ok": True}


@app.post("/api/auth/mfa/recovery-codes", dependencies=[Depends(rate(5, "mfa"))])
def mfa_new_codes(request: Request, body: CodeIn, p: Principal = Depends(need("account.self"))):
    return {"recovery_codes": accounts.regenerate_recovery_codes(_row(request), body.code)}


# ── Account: profile, sessions ───────────────────────────────────────────────
@app.get("/api/account")
def account(request: Request, p: Principal = Depends(need("account.self"))):
    row = _row(request)
    return {"user": {"id": p.user_id, "email": p.email, "display_name": p.display_name}, "role": p.role,
            "org_id": p.org_id, "local": p.local, "auth_mode": accounts.mode(),
            "mfa": {"enabled": p.mfa_enabled, "required": accounts.mode() == "supabase" and p.role in config.MFA_REQUIRED_ROLES,
                    "recovery_codes_left": accounts.recovery_codes_left(p.user_id)},
            "sessions": [{**s, "current": s["public_id"] == row["public_id"]} for s in sessions.list_for(p.user_id)]}


class AccountIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


@app.patch("/api/account")
def account_update(body: AccountIn, p: Principal = Depends(need("account.self"))):
    name = re.sub(r"[\x00-\x1f<>]", "", body.display_name).strip()[:80]
    with db.tx() as c:
        c.execute("UPDATE profiles SET display_name=?, updated_at=? WHERE user_id=?", (name, db.now(), p.user_id))
    sessions.invalidate(user_id=p.user_id)
    return {"ok": True, "display_name": name}


@app.get("/api/auth/sessions")
def my_sessions(request: Request, p: Principal = Depends(need("account.self"))):
    row = _row(request)
    return {"sessions": [{**s, "current": s["public_id"] == row["public_id"]} for s in sessions.list_for(p.user_id)]}


@app.delete("/api/auth/sessions/{public_id}")
def revoke_session(public_id: str, p: Principal = Depends(need("account.self"))):
    target = sessions.by_public_id(p.user_id, public_id[:40])
    if not target:
        raise authz.NotFound("Session not found")
    sessions.revoke(target, "revoked_by_user")
    db.audit("auth.session_revoke", "session", public_id[:40])
    return {"ok": True}


@app.post("/api/auth/sessions/revoke-others")
def revoke_other_sessions(request: Request, p: Principal = Depends(need("account.self"))):
    row = _row(request)
    n = sessions.revoke_all(p.user_id, except_id=row["id"])
    if accounts.mode() == "supabase":
        try:
            sb.sign_out(accounts.access_token(row), "others")
        except (sb.AuthError, accounts.AccountError):
            pass
    db.audit("auth.sessions_revoke_others", "user", p.user_id, revoked=n)
    return {"ok": True, "revoked": n}


# ── Administration: users, roles, invitations ───────────────────────────────
def _admin_users(org_id: str) -> list[dict]:
    rows = db.conn().execute(
        "SELECT p.user_id, p.email, p.display_name, p.status, p.mfa_enabled, m.role, m.created_at, "
        "(SELECT MAX(s.last_seen_at) FROM app_sessions s WHERE s.user_id = p.user_id) AS last_seen "
        "FROM memberships m JOIN profiles p ON p.user_id = m.user_id WHERE m.org_id = ? ORDER BY p.email", (org_id,))
    return [dict(r) for r in rows]


@app.get("/api/admin/users")
def admin_users(p: Principal = Depends(need("users.manage"))):
    with db.system():   # last-seen comes from the sessions table, which only the backend reads
        users = _admin_users(p.org_id)
    return {"users": users, "roles": list(authz.ASSIGNABLE_ROLES)}


class UserUpdateIn(BaseModel):
    role: Literal["advocate", "admin"] | None = None
    status: Literal["active", "disabled"] | None = None


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, body: UserUpdateIn, p: Principal = Depends(need("users.manage"))):
    if user_id == p.user_id:
        raise authz.Denied("You cannot change your own role or status. Ask another administrator.")
    with db.system():
        target = db.conn().execute("SELECT m.role, p.status FROM memberships m JOIN profiles p ON p.user_id = m.user_id "
                                   "WHERE m.org_id=? AND m.user_id=?", (p.org_id, user_id)).fetchone()
        n_admins = db.conn().execute("SELECT COUNT(*) FROM memberships m JOIN profiles p ON p.user_id = m.user_id "
                                     "WHERE m.org_id=? AND m.role='admin' AND p.status='active'", (p.org_id,)).fetchone()[0]
    if not target:
        raise authz.NotFound("User not found")
    removing_admin = target["role"] == "admin" and target["status"] == "active" and (
        (body.role and body.role != "admin") or body.status == "disabled")
    if removing_admin and n_admins <= 1:
        raise authz.Denied("The organisation must keep at least one active administrator.")
    if body.role and body.role != target["role"]:
        with db.tx() as c:   # RLS: only an administrator of this organisation, never on their own row
            c.execute("UPDATE memberships SET role=? WHERE org_id=? AND user_id=?", (body.role, p.org_id, user_id))
        db.audit("user.role_change", "user", user_id, old=target["role"], new=body.role)
        db.notify("security", "Your role was changed", f"An administrator changed your role to {body.role}.",
                  "#/account", recipients=[user_id])
    if body.status and body.status != target["status"]:
        with db.tx() as c:
            c.execute("UPDATE profiles SET status=?, updated_at=? WHERE user_id=?", (body.status, db.now(), user_id))
        if body.status == "disabled":
            sessions.revoke_all(user_id)
        if accounts.mode() == "supabase" and config.SUPABASE_SECRET_KEY:
            try:   # also stops sign-in and token refresh at Supabase
                sb.admin_set_banned(user_id, body.status == "disabled")
            except sb.AuthError as exc:
                log.warning("Could not update the Supabase ban state: %s", exc.code)
        db.audit("user.status_change", "user", user_id, new=body.status)
    sessions.invalidate(user_id=user_id)
    with db.system():
        users = _admin_users(p.org_id)
    return {"users": users}


class InviteIn(BaseModel):
    email: str = Field(max_length=254)
    role: Literal["advocate", "admin"]


@app.get("/api/admin/invitations")
def admin_invitations(p: Principal = Depends(need("users.manage"))):
    rows = db.conn().execute("SELECT id, email, role, created_at, expires_at, accepted_at FROM invitations "
                             "WHERE org_id=? ORDER BY created_at DESC LIMIT 200", (p.org_id,))
    return {"invitations": [dict(r) for r in rows]}


@app.post("/api/admin/invitations")
def admin_invite(body: InviteIn, p: Principal = Depends(need("users.manage"))):
    inv = accounts.create_invitation(p.org_id, body.email, body.role, p.user_id)
    base = config.PUBLIC_URL or "http://127.0.0.1:%d" % config.APP_PORT
    return {**inv, "signup_link": f"{base}/#/signup?email={quote(inv['email'])}"}


@app.delete("/api/admin/invitations/{inv_id}")
def admin_invite_delete(inv_id: str, p: Principal = Depends(need("users.manage"))):
    with db.tx() as c:
        n = c.execute("DELETE FROM invitations WHERE id=? AND org_id=? AND accepted_at IS NULL", (inv_id, p.org_id)).rowcount
    if not n:
        raise authz.NotFound("Invitation not found")
    db.audit("invitation.revoke", "invitation", inv_id)
    return {"ok": True}


@app.get("/api/org/directory")
def directory(q: str = "", p: Principal = Depends(need("directory.read"))):
    like = f"%{q.strip()[:80]}%"
    rows = db.conn().execute(
        "SELECT p.user_id, p.display_name, p.email, m.role FROM memberships m JOIN profiles p ON p.user_id = m.user_id "
        "WHERE m.org_id=? AND p.status='active' AND m.role IN ('advocate','admin') "
        "AND (p.display_name LIKE ? OR p.email LIKE ?) ORDER BY p.display_name LIMIT 50", (p.org_id, like, like))
    return {"people": [dict(r) for r in rows]}


# ── Status / dashboard ───────────────────────────────────────────────────────
@app.get("/api/status")
def status(p: Principal = P):
    if time.time() - _state["llm_checked"] > 120:
        ok, _ = llm.ping()
        _state.update(llm=ok, llm_checked=time.time())
    lib = {r["collection"]: r["n"] for r in db.conn().execute(
        "SELECT collection, COUNT(*) n FROM documents WHERE status='ready' AND collection <> 'case' GROUP BY collection")}
    mine = authz.accessible_case_ids(p)
    if mine:
        ph = ",".join("?" * len(mine))
        lib["case"] = db.conn().execute(f"SELECT COUNT(*) FROM documents WHERE status='ready' AND collection='case' "
                                        f"AND case_id IN ({ph})", mine).fetchone()[0]
        processing = db.conn().execute(f"SELECT COUNT(*) FROM documents WHERE status='processing' AND case_id IN ({ph})",
                                       mine).fetchone()[0]
    else:
        processing = 0
    return {
        "llm_model": config.LLM_MODEL, "fast_model": config.FAST_LLM_MODEL, "embed_model": config.EMBED_MODEL,
        "reranker": config.RERANK_MODEL if config.RERANK_ENABLED else None, "llm_reachable": _state["llm"],
        "llm_host": "Ollama Cloud" if "ollama.com" in config.OLLAMA_HOST else "Local Ollama",
        "models_ready": _state["models_ready"], "indexed_chunks": len(index), "documents": lib,
        "processing": processing, "ocr": ocr.status(), "sync_enabled": config.SYNC_ENABLED,
        "database": "Supabase Postgres" if db.backend() == "postgres" else "Local SQLite",
        "file_storage": "Supabase Storage (private)" if storage.remote_enabled() else "Local folder",
        "accounts": "Supabase Auth" if accounts.mode() == "supabase" else "Local single user",
        "malware_scan": filescan.malware_scan_status(), "pii_redaction": config.AI_PII_REDACTION,
        "fallback_model": config.LLM_FALLBACK_MODEL or None, "llm_circuit": llm.breaker_state(),
        "jobs": jobs.stats() if p.signed_in else None,
    }


@app.get("/api/dashboard")
def dashboard(p: Principal = Depends(need("cases.list"))):
    today = date.today()
    mine = authz.accessible_case_ids(p) or ["-"]
    ph = ",".join("?" * len(mine))
    upcoming = [dict(r) for r in db.conn().execute(
        f"SELECT id, title, case_type, court, next_hearing_date, status FROM cases WHERE id IN ({ph}) "
        "AND next_hearing_date >= ? AND next_hearing_date <= ? ORDER BY next_hearing_date LIMIT 8",
        (*mine, today.isoformat(), (today + timedelta(days=45)).isoformat()))]
    by_type = {r["case_type"]: r["n"] for r in db.conn().execute(
        f"SELECT case_type, COUNT(*) n FROM cases WHERE id IN ({ph}) GROUP BY case_type", mine)}
    lib = {r["collection"]: r["n"] for r in db.conn().execute(
        "SELECT collection, COUNT(*) n FROM documents WHERE status='ready' AND collection <> 'case' GROUP BY collection")}
    # Research history lives on the Legal Research page (Recent History), not here.
    return {"upcoming": upcoming, "by_type": by_type, "library": lib,
            "total_cases": sum(by_type.values())}


# ── Preferences, notifications, activity ────────────────────────────────────
LANGS = ("en", "hi", "mr")
PREF_KEYS = {"lang": LANGS, "voice_lang": ("en-IN", "hi-IN", "mr-IN"), "tts": ("on", "off"),
             "voice_consent": ("granted", "denied", ""), "draft_style": None}
ORG_KEYS = {"chat_retention_days"}


class PrefsIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


@app.get("/api/prefs")
def get_prefs(p: Principal = P):
    out = {"lang": "en"}
    if p.signed_in:
        out.update({k: v for k, v in db.user_prefs(p.user_id).items() if k in PREF_KEYS})
        out["chat_retention_days"] = db.get_setting("chat_retention_days", "") or ""
    return out


@app.put("/api/prefs")
def put_prefs(body: PrefsIn, p: Principal = Depends(need("account.self"))):
    for k, v in body.values.items():
        v = str(v)[:200]
        if k in ORG_KEYS:
            authz.require(p, "settings.manage")
            if v and not (v.isdigit() and 1 <= int(v) <= 3650):
                raise HTTPException(400, "Retention must be 1–3650 days, or empty to keep forever.")
            db.set_setting(k, v)
            db.audit("settings.update", "setting", k, value=v or "forever")
            continue
        allowed = PREF_KEYS.get(k, ())
        if k not in PREF_KEYS or (allowed is not None and v not in allowed):
            raise HTTPException(400, f"Invalid preference: {k}")
        db.set_user_pref(k, v)
    return get_prefs(p)


@app.get("/api/notifications")
def notifications(unread: bool = False, limit: int = 50, p: Principal = Depends(need("notifications.read"))):
    sql = "SELECT * FROM notifications WHERE user_id=?" + (" AND read_at IS NULL" if unread else "") + \
          " ORDER BY created_at DESC, id DESC LIMIT ?"
    items = [dict(r) for r in db.conn().execute(sql, (p.user_id, max(1, min(limit, 200))))]
    n_unread = db.conn().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
                                 (p.user_id,)).fetchone()[0]
    return {"items": items, "unread": n_unread}


@app.post("/api/notifications/read")
def notifications_read(p: Principal = Depends(need("notifications.read"))):
    with db.tx() as c:
        c.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (db.now(), p.user_id))
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = 100, action: str = "", outcome: str = "", actor: str = "",
              p: Principal = Depends(need("audit.read"))):
    sql, args = "SELECT * FROM audit_log WHERE org_id=?", [p.org_id]
    if action:
        sql += " AND action LIKE ?"
        args.append(f"{action[:60]}%")
    if outcome:
        sql += " AND outcome=?"
        args.append(outcome[:20])
    if actor:
        sql += " AND actor_id=?"
        args.append(actor[:60])
    rows = db.conn().execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 500))))
    # Records only: who, what, which id, outcome. Details never contain document text (see db.audit).
    return {"items": [db.row_to_dict(r, ("detail",)) for r in rows]}


# ── Cases ────────────────────────────────────────────────────────────────────
class CaseIn(BaseModel):
    title: str | None = Field(None, max_length=200)
    case_type: str | None = Field(None, max_length=20)
    case_subtype: str | None = Field(None, max_length=120)
    case_number: str | None = Field(None, max_length=100)
    filing_number: str | None = Field(None, max_length=100)
    fir_number: str | None = Field(None, max_length=60)
    cnr_number: str | None = Field(None, max_length=30)
    court: str | None = Field(None, max_length=200)
    state: str | None = Field(None, max_length=80)
    district: str | None = Field(None, max_length=80)
    police_station: str | None = Field(None, max_length=120)
    filing_year: str | int | None = None
    client_name: str | None = Field(None, max_length=200)
    client_role: str | None = Field(None, max_length=100)
    opposite_party: str | None = Field(None, max_length=200)
    opposite_role: str | None = Field(None, max_length=100)
    filing_date: str | None = Field(None, max_length=10)
    next_hearing_date: str | None = Field(None, max_length=10)
    status: str | None = Field(None, max_length=40)
    acts_sections: str | None = Field(None, max_length=600)
    description: str | None = Field(None, max_length=6000)
    is_draft: bool | None = None


class NoteIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field("Advocate", max_length=80)


def _field_error(exc: cases.FieldError) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "field": exc.field, "code": exc.code}, status_code=400)


@app.get("/api/reference")
def reference_data(p: Principal = Depends(need("library.read"))):
    return reference.form_options()


@app.get("/api/cases")
def list_cases(p: Principal = Depends(need("cases.list"))):
    return {"cases": cases.list_cases(p), "types": cases.CASE_TYPES, "statuses": cases.CASE_STATUSES,
            "can_create": p.can("case.create")}


@app.post("/api/cases")
def create_case(body: CaseIn, p: Principal = Depends(need("case.create"))):
    try:
        return cases.create_case(body.model_dump(exclude_none=True))
    except cases.FieldError as exc:
        return _field_error(exc)


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, p: Principal = P):
    access = authz.case_access(p, case_id)
    if access is None:
        if p.signed_in and p.role == "admin" and authz.case_org(case_id) == p.org_id:
            access = "admin"
        else:
            authz.require_case(p, case_id, "case.read")   # raises NotFound / Denied
    if access == "admin":   # administrator who is not on the case: the listing data only, no contents
        c = cases.case_metadata(case_id)
    else:
        c = cases.get_case(case_id, with_notes=access in authz.CASE_ACTIONS["notes"])
    if not c:
        raise authz.NotFound("Case not found")
    c["access"] = None if access == "admin" else access
    c["can"] = {a: authz.can_case(p, case_id, a) for a in ("case.update", "case.delete", "case.members", "notes",
                                                            "documents.upload", "documents.delete", "drafts",
                                                            "provisions", "records")}
    return c


@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, body: CaseIn, p: Principal = P):
    authz.require_case(p, case_id, "case.update")
    try:
        c = cases.update_case(case_id, body.model_dump(exclude_none=True))
    except cases.FieldError as exc:
        return _field_error(exc)
    if not c:
        raise authz.NotFound("Case not found")
    return c


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str, p: Principal = P):
    authz.require_case(p, case_id, "case.delete")
    if not cases.delete_case(case_id):
        raise authz.NotFound("Case not found")
    return {"ok": True}


@app.post("/api/cases/{case_id}/notes")
def add_note(case_id: str, body: NoteIn, p: Principal = P):
    authz.require_case(p, case_id, "notes")
    c = cases.add_note(case_id, body.content, p.display_name or body.author)
    if not c:
        raise authz.NotFound("Case not found")
    return c


# ── Case members ─────────────────────────────────────────────────────────────
def _members(case_id: str) -> list[dict]:
    with db.system():   # names of the people on the case (visible to anyone who can see the case)
        rows = db.conn().execute(
            "SELECT cm.user_id, cm.access, cm.created_at, p.display_name, p.email, m.role FROM case_members cm "
            "JOIN profiles p ON p.user_id = cm.user_id JOIN cases c ON c.id = cm.case_id "
            "LEFT JOIN memberships m ON m.user_id = cm.user_id AND m.org_id = c.org_id "
            "WHERE cm.case_id=? ORDER BY cm.access, p.display_name", (case_id,))
        return [dict(r) for r in rows]


@app.get("/api/cases/{case_id}/members")
def case_members(case_id: str, p: Principal = P):
    if not authz.can_case(p, case_id, "case.members"):
        authz.require_case(p, case_id, "case.read")
    members = _members(case_id)
    if not authz.can_case(p, case_id, "case.members"):   # other team members see names and roles, not e-mails
        members = [{k: v for k, v in m.items() if k != "email"} for m in members]
    return {"members": members, "can_manage": authz.can_case(p, case_id, "case.members")}


class MemberIn(BaseModel):
    user_id: str | None = Field(None, max_length=64)
    email: str | None = Field(None, max_length=254)
    access: Literal["owner", "advocate"] = "advocate"


@app.post("/api/cases/{case_id}/members")
def add_case_member(case_id: str, body: MemberIn, p: Principal = P):
    authz.require_case(p, case_id, "case.members")
    org = authz.case_org(case_id)
    with db.system():
        if body.user_id:
            who = db.conn().execute("SELECT m.user_id, m.role FROM memberships m JOIN profiles p ON p.user_id = m.user_id "
                                    "WHERE m.org_id=? AND m.user_id=? AND p.status='active'", (org, body.user_id)).fetchone()
        else:
            who = db.conn().execute("SELECT m.user_id, m.role FROM memberships m JOIN profiles p ON p.user_id = m.user_id "
                                    "WHERE m.org_id=? AND lower(p.email)=? AND p.status='active'",
                                    (org, (body.email or "").strip().lower())).fetchone()
    if not who:
        raise authz.NotFound("No active member of your organisation has that account.")
    if not authz.compatible_access(who["role"], body.access):
        raise authz.Denied("That person's role does not allow this level of access.")
    with db.tx() as c:
        c.execute("INSERT INTO case_members(case_id, user_id, access, added_by, created_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(case_id, user_id) DO UPDATE SET access=excluded.access",
                  (case_id, who["user_id"], body.access, p.user_id, db.now()))
    self_assigned = who["user_id"] == p.user_id
    db.audit("case.member_add", "case", case_id, member=who["user_id"], access=body.access, self_assigned=self_assigned)
    if not self_assigned:
        db.notify("case_update", "You were added to a case", f"Access: {body.access}.", f"#/cases/{case_id}",
                  recipients=[who["user_id"]])
    return {"members": _members(case_id)}


@app.delete("/api/cases/{case_id}/members/{user_id}")
def remove_case_member(case_id: str, user_id: str, p: Principal = P):
    authz.require_case(p, case_id, "case.members")
    members = _members(case_id)
    target = next((m for m in members if m["user_id"] == user_id), None)
    if not target:
        raise authz.NotFound("Not a member of this case")
    if target["access"] == "owner" and sum(m["access"] == "owner" for m in members) <= 1:
        raise authz.Denied("A case must keep at least one owner.")
    with db.tx() as c:
        c.execute("DELETE FROM case_members WHERE case_id=? AND user_id=?", (case_id, user_id))
    db.audit("case.member_remove", "case", case_id, member=user_id)
    return {"members": _members(case_id)}


# ── Documents ────────────────────────────────────────────────────────────────
DOC_FIELDS = ("id, case_id, collection, doc_type, title, filename, pages, chars, n_chunks, status, error, meta, created_at, "
              "jurisdiction")


@app.get("/api/documents")
def list_documents(case_id: str | None = None, collection: str | None = None, p: Principal = P):
    sql, args = f"SELECT {DOC_FIELDS} FROM documents WHERE 1=1", []
    if case_id:
        authz.require_case(p, case_id, "documents.read")
        # Derived documents (English translations) are reached from their source document, not listed.
        sql += " AND case_id=? AND meta->>'derived_from' IS NULL"
        args.append(case_id)
    else:
        authz.require(p, "library.read")
        mine = authz.accessible_case_ids(p)
        sql += " AND (collection <> 'case'" + (f" OR case_id IN ({','.join('?' * len(mine))})" if mine else "") + ")"
        args += mine
    if collection:
        sql += " AND collection=?"
        args.append(collection)
    sql += " ORDER BY created_at DESC LIMIT 500"
    return {"documents": [db.row_to_dict(r) for r in db.conn().execute(sql, args)], "types": ingest.DOC_TYPES}


@app.post("/api/cases/{case_id}/documents", dependencies=[Depends(rate(60, "upload"))])
def upload(case_id: str, file: UploadFile = File(...), doc_type: str = Form("auto"), batch_item: str = Form(""),
           p: Principal = P):
    authz.require_case(p, case_id, "documents.upload")
    item = _batch_item_for_upload(batch_item, case_id, p, file.filename) if batch_item else None
    try:
        doc = _store_upload(case_id, file, doc_type, p)
    except HTTPException as exc:
        if item:
            _set_item(item["id"], "rejected", reason=str(exc.detail))
        raise
    if item:
        _set_item(item["id"], "duplicate" if doc.get("duplicate") else "stored", doc_id=doc["id"])
    return doc


def _store_upload(case_id: str, file: UploadFile, doc_type: str, p: Principal) -> dict:
    name = security.safe_filename(file.filename or "document")
    ext = Path(name).suffix.lower()
    if ext not in security.ALLOWED_EXT:
        raise HTTPException(415, "Accepted: PDF, DOC/DOCX, TXT, MD and images (PNG, JPG, TIFF, WEBP).")
    if doc_type not in ingest.DOC_TYPES and doc_type != "auto":
        doc_type = "auto"
    doc_id = db.new_id("doc_")
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    # The upload is streamed to a private temporary file in 1 MB pieces (never held in memory), validated, then
    # stored (Supabase Storage or the local upload folder). Nothing reaches permanent storage before validation.
    with tempfile.TemporaryDirectory(prefix="nyaya_up_") as tmp:
        staged = Path(tmp) / f"upload{ext}"
        with open(staged, "wb") as out:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File is larger than {config.MAX_UPLOAD_MB} MB.")
                out.write(chunk)
        if not size:
            raise HTTPException(422, "The file is empty.")
        if not security.sniff_ok(staged, ext):
            raise HTTPException(415, "File content does not match its extension.")
        try:
            filescan.check(staged, ext)
        except filescan.Rejected as exc:
            db.audit("document.rejected", "case", case_id, outcome="rejected", ext=ext, reason=str(exc)[:120])
            raise HTTPException(422, str(exc)) from None
        sha = ingest.sha256_file(staged)
        dup = ingest.find_duplicate(case_id, sha)   # idempotent: the same file twice is one document
        if dup:
            if dup["status"] in ("failed", "cancelled"):  # retry a previously failed or cancelled file
                _submit_ingest(dup["id"], p)
            return {**_public(dup), "duplicate": True}
        try:
            ref = storage.put_file(staged, case_id, doc_id, ext, security.MIME.get(ext, "application/octet-stream"))
        except storage.StorageError as exc:
            raise HTTPException(502, str(exc))
        try:
            doc, created = ingest.register(staged, collection="case", case_id=case_id,
                                           doc_type=None if doc_type == "auto" else doc_type,
                                           title=None, filename=name, doc_id=doc_id, stored=ref, sha=sha)
        except BaseException:
            storage.delete(ref)  # keep storage and metadata consistent: no file without its record
            raise
    if not created:  # lost a race with an identical concurrent upload
        storage.delete(ref)
        return {**_public(doc), "duplicate": True}
    job = _submit_ingest(doc["id"], p)
    db.audit("document.upload", "document", doc["id"], case_id=case_id, ext=ext, bytes=size)
    return {**_public(doc), "job_id": job["id"]}


# ── Bulk upload (batches) ────────────────────────────────────────────────────
class BatchFileIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)


class BatchIn(BaseModel):
    files: list[BatchFileIn] = Field(min_length=1, max_length=500)


def _batch_row(batch_id: str, p: Principal) -> dict:
    with db.system():
        b = db.conn().execute("SELECT * FROM upload_batches WHERE id=?", (batch_id,)).fetchone()
    if not b or b["owner_id"] != p.user_id:
        raise authz.NotFound("Upload batch not found")
    return dict(b)


def _batch_item_for_upload(item_id: str, case_id: str, p: Principal, filename: str | None) -> dict:
    """The client names the batch item it is uploading; the server checks it really is the caller's, for this case,
    still waiting, and for this file — a client-supplied id is never trusted on its own."""
    with db.system():
        r = db.conn().execute("SELECT i.*, b.owner_id, b.case_id, b.cancel_requested FROM batch_items i "
                              "JOIN upload_batches b ON b.id = i.batch_id WHERE i.id=?", (item_id[:40],)).fetchone()
    if not r or r["owner_id"] != p.user_id or r["case_id"] != case_id:
        raise authz.NotFound("Upload batch not found")
    if r["cancel_requested"]:
        raise HTTPException(409, "This upload batch was cancelled.")
    if r["status"] != "waiting":
        raise HTTPException(409, "This file of the batch was already handled.")
    if security.safe_filename(filename or "document") != r["filename"]:
        raise HTTPException(409, "The file does not match the batch entry.")
    return dict(r)


def _set_item(item_id: str, status: str, reason: str = "", doc_id: str | None = None) -> None:
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE batch_items SET status=?, reason=?, doc_id=COALESCE(?, doc_id), updated_at=? WHERE id=?",
                      (status, reason[:300], doc_id, db.now(), item_id))


def _batch_view(b: dict) -> dict:
    with db.system():
        items = [dict(r) for r in db.conn().execute(
            "SELECT i.id, i.seq, i.filename, i.bytes, i.status, i.reason, i.doc_id, d.status AS doc_status, d.error "
            "FROM batch_items i LEFT JOIN documents d ON d.id = i.doc_id WHERE i.batch_id=? ORDER BY i.seq", (b["id"],))]
    counts = {"waiting": 0, "processing": 0, "ready": 0, "failed": 0, "rejected": 0, "cancelled": 0}
    for it in items:
        if it["status"] in ("rejected", "cancelled"):
            state = it["status"]
        elif it["status"] == "waiting":
            state = "waiting"
        else:
            state = {"ready": "ready", "processing": "processing", "cancelled": "cancelled"}.get(it["doc_status"], "failed")
            if state == "failed":
                it["reason"] = it["error"] or it["reason"] or "Processing failed"
        it["state"] = state
        counts[state] += 1
    active = counts["waiting"] + counts["processing"]
    good, bad = counts["ready"], counts["failed"] + counts["rejected"]
    if b["cancel_requested"]:
        status = "cancelling" if counts["processing"] else "cancelled"
    elif active:
        status = "queued" if counts["waiting"] == len(items) else "processing"
    elif good and (bad or counts["cancelled"]):
        status = "partial"
    elif good:
        status = "completed"
    else:
        status = "failed"
    return {"id": b["id"], "case_id": b["case_id"], "created_at": b["created_at"], "status": status,
            "total": len(items), "counts": counts, "items": items}


@app.post("/api/cases/{case_id}/batches")
def create_batch(case_id: str, body: BatchIn, p: Principal = P):
    """Register a bulk upload before sending the files: limits are checked up front, each file gets a slot."""
    authz.require_case(p, case_id, "documents.upload")
    if len(body.files) > config.BATCH_MAX_FILES:
        raise HTTPException(413, f"At most {config.BATCH_MAX_FILES} files per batch.")
    total = sum(f.size for f in body.files)
    if total > config.BATCH_MAX_TOTAL_MB * 1024 * 1024:
        raise HTTPException(413, f"The batch is larger than {config.BATCH_MAX_TOTAL_MB} MB in total.")
    now, batch_id = db.now(), db.new_id("batch_")
    with db.tx() as c:   # RLS: the caller's own batch, for a case they can upload to
        c.execute("INSERT INTO upload_batches(id, owner_id, org_id, case_id, total, total_bytes, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?)", (batch_id, p.user_id, p.org_id, case_id, len(body.files), total, now, now))
        for i, f in enumerate(body.files, 1):
            name = security.safe_filename(f.name)
            ext = Path(name).suffix.lower()
            reason = ("File type not accepted." if ext not in security.ALLOWED_EXT else
                      f"Larger than {config.MAX_UPLOAD_MB} MB." if f.size > config.MAX_UPLOAD_MB * 1024 * 1024 else
                      "Empty file." if f.size == 0 else "")
            c.execute("INSERT INTO batch_items(id, batch_id, seq, filename, bytes, status, reason, updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (db.new_id("bi_"), batch_id, i, name, f.size,
                                                  "rejected" if reason else "waiting", reason, now))
    db.audit("batch.create", "case", case_id, batch=batch_id, files=len(body.files), bytes=total)
    return _batch_view(_batch_row(batch_id, p))


@app.get("/api/batches")
def list_batches(p: Principal = Depends(need("account.self"))):
    with db.system():
        rows = db.conn().execute("SELECT * FROM upload_batches WHERE owner_id=? ORDER BY created_at DESC LIMIT 20",
                                 (p.user_id,)).fetchall()
    return {"batches": [{k: v for k, v in _batch_view(dict(r)).items() if k != "items"} for r in rows]}


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str, p: Principal = Depends(need("account.self"))):
    return _batch_view(_batch_row(batch_id, p))


@app.post("/api/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str, p: Principal = Depends(need("account.self"))):
    b = _batch_row(batch_id, p)
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE upload_batches SET cancel_requested=1, updated_at=? WHERE id=?", (db.now(), batch_id))
            c.execute("UPDATE batch_items SET status='cancelled', reason='Cancelled before upload', updated_at=? "
                      "WHERE batch_id=? AND status='waiting'", (db.now(), batch_id))
        docs = [r["doc_id"] for r in db.conn().execute(
            "SELECT doc_id FROM batch_items WHERE batch_id=? AND doc_id IS NOT NULL", (batch_id,))]
        running = [r["id"] for r in db.conn().execute(
            f"SELECT id FROM jobs WHERE status IN ('queued','processing') AND dedupe_key IN "
            f"({','.join('?' * len(docs))})", [f"ingest:{d}" for d in docs])] if docs else []
    for job_id in running:   # files already stored stop processing; finished ones are kept
        jobs.cancel(job_id, p.user_id)
    db.audit("batch.cancel", "case", b["case_id"], batch=batch_id, stopped=len(running))
    return _batch_view(_batch_row(batch_id, p))


@app.get("/api/batches/{batch_id}/report.csv")
def batch_report(batch_id: str, p: Principal = Depends(need("account.self"))):
    """Per-file outcome for the person who uploaded: names and reasons only — never document contents."""
    import csv
    import io
    view = _batch_view(_batch_row(batch_id, p))

    def cell(v) -> str:   # spreadsheet formula injection: a cell may not start with = + - @
        v = str(v if v is not None else "")
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "file", "outcome", "reason", "document_id"])
    for it in view["items"]:
        w.writerow([it["seq"], cell(it["filename"]), it["state"], cell(it["reason"] or ""), it["doc_id"] or ""])
    db.audit("batch.report", "case", view["case_id"], batch=batch_id)
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="upload-report-{batch_id}.csv"', "Cache-Control": "private, no-store"})


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("path", "sha256")}


@app.get("/api/documents/{doc_id}")
def document(doc_id: str, p: Principal = P):
    authz.require_document(p, doc_id)
    row = db.conn().execute(f"SELECT {DOC_FIELDS} FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise authz.NotFound("Document not found")
    return db.row_to_dict(row)


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, p: Principal = P):
    collection, _ = authz.require_document(p, doc_id, "documents.delete")
    ingest.delete_document(doc_id)
    db.audit("document.delete", "document", doc_id)
    return {"ok": True}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str, p: Principal = P):
    collection, case_id = authz.require_document(p, doc_id)
    row = db.conn().execute("SELECT path, filename FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise authz.NotFound("Document not found")
    if collection == "case":
        db.audit("document.open", "document", doc_id, case_id=case_id)
    ext = Path(row["filename"]).suffix.lower() or Path(row["path"]).suffix.lower()
    disposition = "attachment" if ext in (".docx", ".doc") else "inline"
    media = security.MIME.get(ext, "application/octet-stream")
    if storage.is_remote(row["path"]):
        # Streamed through the backend: the browser never gets a storage URL or key, and the CSP below applies.
        try:
            body = storage.stream(row["path"])
        except storage.StorageError as exc:
            raise HTTPException(404, str(exc))
        resp = StreamingResponse(body, media_type=media, headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(row['filename'])}"})
    else:
        path = config.from_stored(row["path"]).resolve()
        roots = {config.ROOT.resolve(), config.DATA_DIR.resolve()}   # never serve files outside the app's folders
        if not any(r in path.parents for r in roots) or not path.is_file():
            raise HTTPException(404, "File missing")
        resp = FileResponse(path, media_type=media, filename=row["filename"], content_disposition_type=disposition)
    if ext != ".pdf":  # browsers' PDF viewers do not run under a sandbox CSP
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    else:  # the document viewer embeds PDFs; only this origin may frame them
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.get("/api/documents/{doc_id}/text")
def document_text(doc_id: str, p: Principal = P):
    authz.require_document(p, doc_id)
    row = db.conn().execute("SELECT path, collection, title FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise authz.NotFound("Document not found")
    if row["collection"] == "draft":
        return {"title": row["title"], "text": config.from_stored(row["path"]).read_text("utf-8")}
    chunks, _ = ingest.document_text(doc_id)
    return {"title": row["title"], "text": "\n\n".join(t for *_, t in chunks)[:200000]}


@app.get("/api/chunks/{chunk_id}")
def chunk(chunk_id: int, p: Principal = P):
    with db.system():
        owner = db.conn().execute("SELECT doc_id FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    if not owner:
        raise authz.NotFound("Passage not found")
    try:
        authz.require_document(p, owner["doc_id"])
    except authz.NotFound:
        raise authz.NotFound("Passage not found") from None
    row = db.conn().execute(
        "SELECT c.id, c.doc_id, c.header, c.text, c.label, c.page_start, c.page_end, d.title, d.collection "
        "FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=?", (chunk_id,)).fetchone()
    if not row:
        raise authz.NotFound("Passage not found")
    return dict(row)


# ── Library ──────────────────────────────────────────────────────────────────
@app.get("/api/library")
def library(p: Principal = Depends(need("library.read"))):
    out: dict[str, list] = {"bare_act": [], "draft": [], "judgment": []}
    for r in db.conn().execute(f"SELECT {DOC_FIELDS} FROM documents WHERE collection IN ('bare_act','draft','judgment') "
                               "AND status <> 'superseded' ORDER BY title"):   # current versions only
        d = db.row_to_dict(r)
        out[d["collection"]].append(d)
    return out


@app.get("/api/library/lookup")
def lookup(q: str = "", act: str | None = None, limit: int = 12, p: Principal = Depends(need("library.read"))):
    """Instant provision lookup (no LLM): exact section/article/rule match, else keyword search."""
    q = q.strip()[:300]
    if not q:
        return {"results": []}
    limit = max(1, min(limit, 30))
    query = f"{q} {act}" if act else q
    refs = citations.parse_refs(query)
    doc_ids = retrieval.act_doc_ids([act]) if act else None
    ids = retrieval.exact(refs, "library", None, doc_ids)
    if len(ids) < limit:
        ids += [i for i in retrieval.lexical(query, limit * 2, "library", None, doc_ids) if i not in ids]
    hits = [h for h in retrieval.load_hits(ids[:limit * 2]) if h.collection == "bare_act"][:limit]
    return {"results": [{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "label": h.label,
                         "header": h.header, "text": h.text, "page": h.page_start, "exact": h.chunk_id in ids[:len(refs) * 6]}
                        for h in hits], "refs": [r.key for r in refs]}


# ── Limitation ───────────────────────────────────────────────────────────────
class LimitationIn(BaseModel):
    cause: str = Field(max_length=60)
    start_date: str = Field(max_length=10)


@app.get("/api/limitation")
def limitation_categories(p: Principal = Depends(need("library.read"))):
    return {"categories": tools.categories(), "caveat": tools.CAVEAT}


@app.post("/api/limitation")
def limitation(body: LimitationIn, p: Principal = Depends(need("library.read"))):
    try:
        return tools.limitation(body.cause, body.start_date)
    except KeyError:
        raise HTTPException(400, "Unknown category")
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")


# ── Chats (personal research history) ───────────────────────────────────────
@app.get("/api/chats")
def chats(case_id: str | None = None, p: Principal = Depends(need("research.use"))):
    if case_id:
        authz.require_case(p, case_id, "chat")
        rows = db.conn().execute("SELECT * FROM chats WHERE case_id=? AND user_id=? ORDER BY updated_at DESC LIMIT 50",
                                 (case_id, p.user_id))
    else:
        rows = db.conn().execute("SELECT * FROM chats WHERE case_id IS NULL AND user_id=? ORDER BY updated_at DESC LIMIT 50",
                                 (p.user_id,))
    return {"chats": [dict(r) for r in rows]}


def _own_chat(p: Principal, chat_id: str) -> dict:
    chat = db.conn().execute("SELECT * FROM chats WHERE id=? AND user_id=?", (chat_id, p.user_id)).fetchone()
    if not chat or (chat["case_id"] and not authz.can_case(p, chat["case_id"], "chat")):
        raise authz.NotFound("Chat not found")
    return dict(chat)


# ── Recent history (Legal Research → Recent History): the caller's own conversations and searches ──
@app.get("/api/history")
def research_history(limit: int = 30, p: Principal = Depends(need("research.use"))):
    limit = max(1, min(limit, 100))
    chats_ = [dict(r) for r in db.conn().execute(
        "SELECT ch.id, ch.title, ch.case_id, ch.updated_at, c.title AS case_title FROM chats ch "
        "LEFT JOIN cases c ON c.id = ch.case_id WHERE ch.user_id = ? ORDER BY ch.updated_at DESC LIMIT ?",
        (p.user_id, limit * 2))]
    # a conversation about a matter the caller has since lost access to is not offered
    chats_ = [c for c in chats_ if not c["case_id"] or authz.can_case(p, c["case_id"], "chat")][:limit]
    searches = [db.row_to_dict(r) for r in db.conn().execute(
        "SELECT id, query, filters, results, created_at FROM search_history WHERE user_id = ? "
        "ORDER BY created_at DESC LIMIT ?", (p.user_id, limit))]
    for s in searches:
        try:
            s["filters"] = json.loads(s["filters"] or "{}")
        except (TypeError, ValueError):
            s["filters"] = {}
    return {"chats": chats_, "searches": searches}


@app.delete("/api/history/searches")
def clear_search_history(p: Principal = Depends(need("research.use"))):
    with db.tx() as c:
        n = c.execute("DELETE FROM search_history WHERE user_id = ?", (p.user_id,)).rowcount
    return {"deleted": n}


@app.delete("/api/history/searches/{sid}")
def delete_search(sid: str, p: Principal = Depends(need("research.use"))):
    with db.tx() as c:
        n = c.execute("DELETE FROM search_history WHERE id = ? AND user_id = ?", (sid[:40], p.user_id)).rowcount
    if not n:
        raise authz.NotFound("Search not found")
    return {"ok": True}


@app.get("/api/chats/{chat_id}")
def chat_messages(chat_id: str, p: Principal = Depends(need("research.use"))):
    chat = _own_chat(p, chat_id)
    msgs = [db.row_to_dict(r) for r in db.conn().execute(
        "SELECT id, role, content, sources, meta, created_at FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))]
    return {"chat": chat, "messages": msgs}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, p: Principal = Depends(need("research.use"))):
    _own_chat(p, chat_id)
    with db.tx() as c:
        c.execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, p.user_id))
    return {"ok": True}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=6000)
    chat_id: str | None = Field(None, max_length=40)
    case_id: str | None = Field(None, max_length=40)
    scope: Literal["all", "case", "library"] = "all"
    doc_id: str | None = Field(None, max_length=40)
    task: Literal["summary", "timeline", "issues", "parties", "arguments", "custom"] | None = None
    lang: Literal["en", "hi", "mr"] = "en"
    input_mode: Literal["text", "voice"] = "text"
    # None/"" = the case's state; "all" = central law and every state's acts; else a state / Union Territory
    jurisdiction: str | None = Field(None, max_length=60)

    @field_validator("jurisdiction")
    @classmethod
    def _known_jurisdiction(cls, v: str | None) -> str | None:
        if v in (None, "", "all") or v in reference.STATES:
            return v or None
        raise ValueError("Unknown state or Union Territory")


@app.post("/api/chat", dependencies=[Depends(rate(config.CHAT_RATE_PER_MIN, "chat"))])
def chat(body: ChatIn, p: Principal = Depends(need("research.use"))):
    # Deterministic authorization before the model sees anything: the case, the document and the chat must all
    # be the caller's to use. The agent never chooses them.
    if body.case_id:
        authz.require_case(p, body.case_id, "chat")
    if body.doc_id:
        collection, doc_case = authz.require_document(p, body.doc_id)
        if collection == "case" and doc_case != body.case_id:
            raise authz.NotFound("Document not found")
        if collection == "case":
            authz.require_case(p, doc_case, "documents.ai")
    if body.chat_id:
        _own_chat(p, body.chat_id)

    def events():
        try:
            for evt in agent.run(message=body.message, chat_id=body.chat_id, case_id=body.case_id, scope=body.scope,
                                 doc_id=body.doc_id, task=body.task, lang=body.lang, principal=p,
                                 jurisdiction=body.jurisdiction):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception:
            log.exception("chat stream failed")
            yield 'data: {"type":"error","text":"Internal error"}\n\n'

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ── Background jobs (only the owner can see, cancel or retry a job) ─────────
@app.get("/api/jobs")
def jobs_list(kind: str | None = None, limit: int = 50, p: Principal = Depends(need("account.self"))):
    return {"jobs": jobs.list_for(p.user_id, limit=limit, kind=kind[:20] if kind else None)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, p: Principal = Depends(need("account.self"))):
    j = jobs.get(job_id, owner=p.user_id)
    if not j:
        raise authz.NotFound("Job not found")
    return j


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str, p: Principal = Depends(need("account.self"))):
    j = jobs.cancel(job_id, p.user_id)
    if not j:
        raise authz.NotFound("Job not found")
    db.audit("job.cancel", "job", job_id)
    return j


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(rate(10, "jobretry"))])
def job_retry(job_id: str, p: Principal = Depends(need("account.self"))):
    try:
        j = jobs.retry(job_id, p.user_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if not j:
        raise authz.NotFound("Job not found")
    db.audit("job.retry", "job", job_id)
    return j


class ConsentIn(BaseModel):
    """Whole-document AI tasks send document text to the configured LLM; the UI must obtain consent."""
    consent: bool = False
    lang: Literal["en", "hi", "mr"] = "en"


def _need_consent(body: ConsentIn) -> None:
    if not body.consent:
        raise HTTPException(400, "This sends the document text to the AI service. Please confirm to continue.")


# ── Document intelligence ────────────────────────────────────────────────────
@app.get("/api/documents/{doc_id}/intel")
def document_intel(doc_id: str, p: Principal = P):
    authz.require_document(p, doc_id)
    try:
        return docintel.overview(doc_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


def _doc_ai(p: Principal, doc_id: str) -> str | None:
    tasks.authorize_doc_ai(p, {"doc_id": doc_id})   # the worker repeats this check before running
    return authz.document_case(doc_id)[1]


@app.post("/api/documents/{doc_id}/reprocess", dependencies=[Depends(rate(10, "reprocess"))])
def document_reprocess(doc_id: str, p: Principal = P):
    """Run extraction again for a failed, quarantined or cancelled document."""
    collection, _ = authz.require_document(p, doc_id, "documents.upload")
    with db.system():
        row = db.conn().execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()
    if collection != "case" or row["status"] not in ("failed", "quarantined", "cancelled"):
        raise HTTPException(409, "Only failed, quarantined or cancelled documents can be processed again.")
    db.audit("document.reprocess", "document", doc_id)
    return _submit_ingest(doc_id, p)


@app.get("/api/documents/{doc_id}/versions")
def document_versions(doc_id: str, p: Principal = P):
    """Versions of a library act or judgment (newest first); superseded texts stay readable for history."""
    authz.require_document(p, doc_id)
    return {"versions": ingest.versions(doc_id)}


@app.post("/api/documents/{doc_id}/translate", dependencies=[Depends(rate(10, "docai"))])
def document_translate(doc_id: str, body: ConsentIn, p: Principal = P):
    _doc_ai(p, doc_id)
    _need_consent(body)
    try:
        docintel._doc(doc_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return jobs.submit("translate", {"doc_id": doc_id}, key=f"translate:{doc_id}", owner=p.user_id, org_id=p.org_id,
                       case_id=authz.document_case(doc_id)[1])


@app.post("/api/documents/{doc_id}/summarize", dependencies=[Depends(rate(10, "docai"))])
def document_summarize(doc_id: str, body: ConsentIn, p: Principal = P):
    _doc_ai(p, doc_id)
    _need_consent(body)
    try:
        docintel._doc(doc_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return jobs.submit("summarize", {"doc_id": doc_id, "lang": body.lang}, key=f"summarize:{doc_id}:{body.lang}",
                       owner=p.user_id, org_id=p.org_id, case_id=authz.document_case(doc_id)[1])


# ── Drafting, notices, forums ────────────────────────────────────────────────
@app.get("/api/drafting/config")
def drafting_config(p: Principal = Depends(need("drafting.use"))):
    return {"types": [{k: t[k] for k in ("id", "label", "kind", "categories")} for t in drafting.doc_types()],
            "forums": [{k: f[k] for k in ("id", "name", "level")} for f in drafting.forums()],
            "styles": drafting.styles(), "saved_style": db.get_user_pref("draft_style") or "formal"}


@app.get("/api/drafting/types/{type_id}")
def drafting_type(type_id: str, p: Principal = Depends(need("drafting.use"))):
    try:
        return drafting.type_profile(type_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/drafting/forums/{forum_id}")
def drafting_forum(forum_id: str, p: Principal = Depends(need("drafting.use"))):
    try:
        return drafting.forum_profile(forum_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class ForumReqIn(BaseModel):
    text: str = Field(min_length=3, max_length=500)
    url: str = Field(min_length=8, max_length=500, pattern=r"^https://")
    verified_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.post("/api/drafting/forums/{forum_id}/requirements")
def add_forum_requirement(forum_id: str, body: ForumReqIn, p: Principal = Depends(need("forum.edit"))):
    try:
        drafting.forum(forum_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    items = drafting.user_requirements(forum_id) + [body.model_dump()]
    db.set_setting(f"forum_reqs:{forum_id}", json.dumps(items[-50:], ensure_ascii=False))
    db.audit("forum.requirement_add", "forum", forum_id, url=body.url)
    return drafting.forum_profile(forum_id)


class DraftIn(BaseModel):
    doc_type: str = Field(max_length=40)
    forum: str = Field(max_length=40)
    style: str = Field("formal", max_length=20)
    lang: Literal["en", "hi", "mr"] = "en"
    case_id: str | None = Field(None, max_length=40)
    doc_ids: list[str] = Field(default_factory=list, max_length=6)
    known: dict[str, str] = Field(default_factory=dict)
    instructions: str = Field("", max_length=4000)
    template_doc_id: str | None = Field(None, max_length=40)
    base_doc_id: str | None = Field(None, max_length=40)
    base_text: str | None = Field(None, max_length=20000)
    title: str | None = Field(None, max_length=200)
    draft_id: str | None = Field(None, max_length=40)
    consent: bool = False
    save_style: bool = False


def _require_draft(p: Principal, draft_id: str) -> dict:
    authz.require(p, "drafting.use")
    with db.system():
        row = db.conn().execute("SELECT case_id, created_by FROM drafts WHERE id=?", (draft_id,)).fetchone()
    ok = row and ((row["case_id"] and authz.can_case(p, row["case_id"], "drafts")) or
                  (not row["case_id"] and row["created_by"] == p.user_id))
    if not ok:
        raise authz.NotFound("Draft not found")
    return dict(row)


@app.post("/api/drafts/generate", dependencies=[Depends(rate(10, "draft"))])
def draft_generate(body: DraftIn, p: Principal = Depends(need("drafting.use"))):
    args = body.model_dump(exclude={"consent", "save_style"})
    tasks.authorize_draft(p, args)   # same check the worker repeats before running
    if body.doc_ids or body.base_doc_id:
        _need_consent(ConsentIn(consent=body.consent))
    try:
        drafting.doc_type(body.doc_type)
        drafting.forum(body.forum)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if len(body.known) > 40 or any(len(k) > 60 for k in body.known):
        raise HTTPException(400, "Too many details")
    if body.save_style:
        db.set_user_pref("draft_style", body.style)
    return jobs.submit("draft", args, owner=p.user_id, org_id=p.org_id, case_id=body.case_id)


@app.get("/api/drafts")
def drafts_list(case_id: str | None = None, kind: str | None = None, p: Principal = Depends(need("drafting.use"))):
    if case_id:
        authz.require_case(p, case_id, "drafts")
    return {"drafts": drafting.list_drafts(case_id, kind, user_id=p.user_id, team_cases=authz.team_case_ids(p))}


@app.get("/api/drafts/{draft_id}")
def draft_get(draft_id: str, p: Principal = P):
    _require_draft(p, draft_id)
    d = drafting.get(draft_id)
    if not d:
        raise authz.NotFound("Draft not found")
    return d


class DraftEditIn(BaseModel):
    content: str = Field(max_length=200000)
    title: str | None = Field(None, max_length=200)


@app.put("/api/drafts/{draft_id}")
def draft_put(draft_id: str, body: DraftEditIn, p: Principal = P):
    _require_draft(p, draft_id)
    d = drafting.update_content(draft_id, body.content, body.title)
    if not d:
        raise authz.NotFound("Draft not found")
    return d


@app.get("/api/drafts/{draft_id}/versions/{v}")
def draft_version(draft_id: str, v: int, p: Principal = P):
    _require_draft(p, draft_id)
    d = drafting.version(draft_id, v)
    if not d:
        raise authz.NotFound("Version not found")
    return d


@app.delete("/api/drafts/{draft_id}")
def draft_delete(draft_id: str, p: Principal = P):
    _require_draft(p, draft_id)
    if not drafting.delete(draft_id):
        raise authz.NotFound("Draft not found")
    return {"ok": True}


@app.get("/api/drafts/{draft_id}/export", dependencies=[Depends(rate(20, "export"))])
def draft_export(draft_id: str, format: Literal["docx", "pdf"] = "docx", p: Principal = P):
    _require_draft(p, draft_id)
    d = drafting.get(draft_id)
    if not d:
        raise authz.NotFound("Draft not found")
    approval = drafting.approval(d)   # only the approved version is exported without the DRAFT label
    if format == "docx":
        data, mime = export.to_docx(d["title"], d["content"], d["sources"], approval), security.MIME[".docx"]
    else:
        data, mime = export.to_pdf(d["title"], d["content"], d["sources"], approval), "application/pdf"
    name = security.safe_filename(f"{d['title'][:80]}_v{d['version']}.{format}")
    db.audit("draft.export", "draft", draft_id, format=format, version=d["version"], approved=bool(approval))
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                    "Cache-Control": "private, no-store"})


class ReviewIn(BaseModel):
    action: Literal["approve", "request_changes", "reopen"]
    note: str = Field("", max_length=1000)


@app.post("/api/drafts/{draft_id}/review")
def draft_review(draft_id: str, body: ReviewIn, p: Principal = P):
    """Human review of AI-generated drafts: approval is recorded per version, by a named advocate."""
    _require_draft(p, draft_id)
    try:
        return drafting.review(draft_id, body.action, body.note, reviewer=p.user_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# ── Applicable provisions ───────────────────────────────────────────────────
class ProvSuggestIn(BaseModel):
    consent: bool = False
    extra: str = Field("", max_length=6000)
    doc_ids: list[str] | None = Field(None, max_length=20)


@app.post("/api/cases/{case_id}/provisions/suggest", dependencies=[Depends(rate(10, "prov"))])
def provisions_suggest(case_id: str, body: ProvSuggestIn, p: Principal = P):
    authz.require_case(p, case_id, "provisions")
    _need_consent(ConsentIn(consent=body.consent))
    if not cases.get_case(case_id):
        raise authz.NotFound("Case not found")
    if body.doc_ids:
        for d in body.doc_ids:
            if (authz.document_case(d) or ("", None))[1] != case_id:
                raise authz.NotFound("A selected document was not found in this case")
    return jobs.submit("provisions", {"case_id": case_id, "doc_ids": body.doc_ids, "extra": body.extra},
                       key=f"prov:{case_id}", owner=p.user_id, org_id=p.org_id, case_id=case_id)


@app.get("/api/cases/{case_id}/provisions")
def provisions_list(case_id: str, p: Principal = P):
    authz.require_case(p, case_id, "provisions")
    return {"provisions": provisions.list_for(case_id)}


class ProvUpdateIn(BaseModel):
    status: Literal["accepted", "rejected", "suggested", "edited"] | None = None
    note: str | None = Field(None, max_length=1000)
    ref: str | None = Field(None, max_length=120)


@app.patch("/api/provisions/{prov_id}")
def provisions_update(prov_id: str, body: ProvUpdateIn, p: Principal = P):
    with db.system():
        row = db.conn().execute("SELECT case_id FROM case_provisions WHERE id=?", (prov_id,)).fetchone()
    if not row or not authz.can_case(p, row["case_id"], "provisions"):
        raise authz.NotFound("Not found")
    pr = provisions.update(prov_id, status=body.status, note=body.note, ref=body.ref)
    if not pr:
        raise authz.NotFound("Not found")
    return pr


# ── All-India law: jurisdictions and the official India Code catalogue ───────
@app.get("/api/jurisdictions")
def jurisdictions(p: Principal = Depends(need("library.read"))):
    counts = {r["jurisdiction"]: r["n"] for r in db.conn().execute(
        "SELECT jurisdiction, COUNT(*) AS n FROM documents WHERE collection='bare_act' AND status <> 'superseded' "
        "GROUP BY jurisdiction")}
    uts = set(reference.STATES[reference.STATES.index("Andaman and Nicobar Islands"):])
    return {"central": {"name": indiacode.CENTRAL, "in_library": counts.get(indiacode.CENTRAL, 0)},
            "states": [{"name": s, "union_territory": s in uts, "in_library": counts.get(s, 0)} for s in reference.STATES],
            "source": "India Code — Legislative Department, Ministry of Law and Justice", "source_url": indiacode.SITE}


@app.get("/api/library/indiacode", dependencies=[Depends(rate(30, "indiacode"))])
def indiacode_catalogue(state: str = "", q: str = "", page: int = 0, p: Principal = Depends(need("library.read"))):
    """Live search of the official India Code record (Central Acts, or one state's / UT's acts)."""
    if state and state != indiacode.CENTRAL and state not in reference.STATES:
        raise HTTPException(400, "Unknown state or Union Territory.")
    try:
        return indiacode.catalogue(state or None, q[:120], max(0, min(page, 500)))
    except indiacode.IndiaCodeError as exc:
        raise HTTPException(502, str(exc)) from None


class ImportActIn(BaseModel):
    uuid: str = Field(min_length=36, max_length=36)


@app.post("/api/library/indiacode/import", dependencies=[Depends(rate(10, "indiacode_import"))])
def indiacode_import(body: ImportActIn, p: Principal = Depends(need("library.import"))):
    """Add one official act to the shared library; it becomes citable once indexed (a background job)."""
    try:
        return indiacode.import_act(body.uuid, owner=p.user_id)
    except indiacode.IndiaCodeError as exc:
        raise HTTPException(502, str(exc)) from None


@app.get("/api/library/provision")
def library_provision(act: str, ref: str, p: Principal = Depends(need("library.read"))):
    from . import library
    return library.lookup_provision(act[:120], ref[:60])


# ── Case information search, imports, integrations ──────────────────────────
class CaseSearchIn(BaseModel):
    cnr_number: str | None = Field(None, max_length=30)
    case_number: str | None = Field(None, max_length=100)
    filing_number: str | None = Field(None, max_length=100)
    fir_number: str | None = Field(None, max_length=60)
    court: str | None = Field(None, max_length=200)
    case_type: str | None = Field(None, max_length=60)
    state: str | None = Field(None, max_length=80)
    district: str | None = Field(None, max_length=80)
    police_station: str | None = Field(None, max_length=120)
    filing_year: str | None = Field(None, max_length=4)
    petitioner: str | None = Field(None, max_length=200)
    respondent: str | None = Field(None, max_length=200)


@app.post("/api/casesearch", dependencies=[Depends(rate(30, "casesearch"))])
def case_search(body: CaseSearchIn, p: Principal = Depends(need("casesearch.use"))):
    return casesearch.search(body.model_dump(), principal=p)


class OrderIn(BaseModel):
    date: str | None = Field(None, max_length=20)
    title: str | None = Field(None, max_length=300)
    url: str | None = Field(None, max_length=500)


class RecordImportIn(CaseSearchIn):
    source: str = Field(max_length=40)
    title: str | None = Field(None, max_length=300)
    category: str | None = Field(None, max_length=20)
    status: str | None = Field(None, max_length=120)
    stage: str | None = Field(None, max_length=120)
    judge: str | None = Field(None, max_length=200)
    next_hearing_date: str | None = Field(None, max_length=10)
    sections: str | None = Field(None, max_length=300)
    orders: list[OrderIn] = Field(default_factory=list, max_length=50)
    source_url: str | None = Field(None, max_length=500, pattern=r"^https://")
    case_id: str | None = Field(None, max_length=40)


@app.post("/api/records/import")
def record_import(body: RecordImportIn, p: Principal = Depends(need("casesearch.use"))):
    if body.case_id:
        authz.require_case(p, body.case_id, "records")
    data = body.model_dump(exclude={"source", "case_id"})
    data["orders"] = [o for o in data["orders"]]
    try:
        return casesearch.import_record(body.source, data, body.case_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/records/{record_id}/save-case")
def record_save_case(record_id: str, p: Principal = Depends(need("case.create"))):
    with db.system():
        row = db.conn().execute("SELECT created_by, case_id FROM source_records WHERE id=? AND kind='case_record'",
                                (record_id,)).fetchone()
    if not row or (row["created_by"] != p.user_id and not (row["case_id"] and authz.can_case(p, row["case_id"], "records"))):
        raise authz.NotFound("Record not found")
    try:
        return casesearch.save_to_case(record_id)
    except cases.FieldError as exc:
        return _field_error(exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/cases/{case_id}/records")
def case_records(case_id: str, p: Principal = P):
    authz.require_case(p, case_id, "records")
    return {"records": casesearch.records_for_case(case_id)}


@app.get("/api/integrations")
def integrations_status(p: Principal = Depends(need("integrations.read"))):
    return {"integrations": integrations.status(), "scheduler": bool(_scheduler and _scheduler.is_alive()),
            "can_manage": p.can("integrations.manage")}


@app.post("/api/integrations/{iid}/sync", dependencies=[Depends(rate(6, "sync"))])
def integrations_sync(iid: str, p: Principal = Depends(need("integrations.manage"))):
    try:
        with db.system():   # public feeds: written as shared records after the administrator check above
            return integrations.run_sync(iid, "manual")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class EnabledIn(BaseModel):
    enabled: bool


@app.post("/api/integrations/{iid}/enabled")
def integrations_enable(iid: str, body: EnabledIn, p: Principal = Depends(need("integrations.manage"))):
    if iid not in integrations.ADAPTERS:
        raise HTTPException(404, "Unknown integration")
    with db.system():
        integrations.set_enabled(iid, body.enabled)
    return {"ok": True}


@app.get("/api/news")
def news(q: str = "", limit: int = 50, p: Principal = Depends(need("library.read"))):
    return {"items": integrations.news(max(1, min(limit, 200)), q[:100])}


# ── Legal awareness, unified search ─────────────────────────────────────────
@app.get("/api/awareness")
def awareness_list(state: str = "MH", subject: str | None = None, q: str = "", p: Principal = Depends(need("library.read"))):
    return awareness.listing(state[:20], subject, q[:100])


@app.get("/api/search")
def unified_search(q: str = "", types: str = "", court: str = "", state: str = "", year: str = "", case_type: str = "",
                   doc_type: str = "", source: str = "", p: Principal = Depends(need("library.read"))):
    t = {x for x in types.split(",") if x} or None
    res = search.search(q, types=t, court=court[:100], state=state[:60], year=year[:4], case_type=case_type[:20],
                        doc_type=doc_type[:20], source=source[:40], principal=p)
    if p.signed_in and q.strip():   # personal search history (Legal Research → Recent History)
        filters = {k: v for k, v in (("types", types), ("court", court), ("state", state), ("year", year),
                                     ("case_type", case_type), ("doc_type", doc_type), ("source", source)) if v}
        with db.tx() as c:
            c.execute("INSERT INTO search_history(id, user_id, org_id, query, filters, results, created_at) "
                      "VALUES(?,?,?,?,?,?,?)", (db.new_id("srch_"), p.user_id, p.org_id, q.strip()[:500],
                                                json.dumps(filters)[:1000], len(res.get("results", [])), db.now()))
            c.execute("DELETE FROM search_history WHERE user_id = ? AND id NOT IN (SELECT id FROM search_history "
                      "WHERE user_id = ? ORDER BY created_at DESC LIMIT 200)", (p.user_id, p.user_id))
    return res


# ── Calendar: hearings, deadlines and tasks with reminders (private to each lawyer) ──
class EventIn(BaseModel):
    title: str | None = Field(None, max_length=200)
    kind: Literal["hearing", "deadline", "follow_up", "submission", "meeting", "task"] | None = None
    description: str | None = Field(None, max_length=4000)
    case_id: str | None = Field(None, max_length=40)
    event_date: str | None = Field(None, max_length=10)
    event_time: str | None = Field(None, max_length=5)
    timezone: str | None = Field(None, max_length=64)
    priority: Literal["low", "normal", "high"] | None = None
    reminders: list[Literal["at_time", "1h", "day_of", "1d", "7d"]] | None = Field(None, max_length=5)
    email_reminders: bool | None = None
    status: Literal["scheduled", "completed", "cancelled"] | None = None


def _event_call(fn, *args):
    try:
        return fn(*args)
    except events.EventError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/calendar/config")
def calendar_config(p: Principal = Depends(need("calendar.use"))):
    return {"kinds": list(events.KINDS), "priorities": list(events.PRIORITIES), "reminders": list(events.RULES),
            "email_available": mailer.configured(), "default_timezone": config.DEFAULT_TIMEZONE,
            "default_reminders": ["day_of", "1d"]}


@app.get("/api/calendar/events")
def calendar_events(start: str, end: str, p: Principal = Depends(need("calendar.use"))):
    return {"events": _event_call(events.list_range, p, start[:10], end[:10])}


@app.get("/api/calendar/upcoming")
def calendar_upcoming(tz: str = "", days: int = 30, limit: int = 12, p: Principal = Depends(need("calendar.use"))):
    return _event_call(events.upcoming, p, tz[:64] or None, days, max(1, min(limit, 50)))


@app.get("/api/calendar/events/{event_id}")
def calendar_event(event_id: str, p: Principal = Depends(need("calendar.use"))):
    return events.get_own(p, event_id[:40])


@app.post("/api/calendar/events", dependencies=[Depends(rate(60, "calendar"))])
def calendar_create(body: EventIn, p: Principal = Depends(need("calendar.use"))):
    data = body.model_dump()
    if data.get("reminders") is None:
        data["reminders"] = ["day_of", "1d"]
    return _event_call(events.create, p, data)


@app.patch("/api/calendar/events/{event_id}", dependencies=[Depends(rate(120, "calendar"))])
def calendar_update(event_id: str, body: EventIn, p: Principal = Depends(need("calendar.use"))):
    return _event_call(events.update, p, event_id[:40], body.model_dump(exclude_unset=True))


@app.delete("/api/calendar/events/{event_id}")
def calendar_delete(event_id: str, p: Principal = Depends(need("calendar.use"))):
    events.delete(p, event_id[:40])
    return {"ok": True}


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
