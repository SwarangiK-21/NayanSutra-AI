"""Supabase Auth (GoTrue) client used by the backend only.

Supabase does the identity work: password hashing (bcrypt), e-mail verification, single-use expiring reset links,
TOTP MFA (enrol / challenge / verify), refresh-token rotation and revocation, and its own rate limits. This module
only calls the documented REST endpoints; nothing here re-implements those features.

User-facing calls use the publishable key; administrative calls (ban, delete a factor, create a user from the
bootstrap script) use the secret key. Neither key, nor any user token, is ever sent to the browser.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading

import httpx

from . import config

log = logging.getLogger(__name__)


class AuthError(Exception):
    """Supabase Auth refused the request. `code` is Supabase's error_code; the message is safe to show."""

    def __init__(self, message: str, code: str = "", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


# Supabase error codes → messages that never reveal whether an account exists.
_MESSAGES = {
    "invalid_credentials": "Incorrect e-mail or password.",
    "email_not_confirmed": "Please verify your e-mail address first — use the link we sent you.",
    "user_banned": "This account is disabled. Contact your administrator.",
    "weak_password": "Choose a stronger password.",
    "same_password": "The new password must be different from the old one.",
    "over_request_rate_limit": "Too many attempts. Please wait a few minutes and try again.",
    "over_email_send_rate_limit": "Too many e-mails were requested. Please wait and try again later.",
    "mfa_verification_failed": "That code is not valid. Check your authenticator app and try again.",
    "mfa_challenge_expired": "The code expired. Please try again.",
    "mfa_factor_not_found": "No authenticator is set up for this account.",
    "flow_state_not_found": "This link is invalid or was opened in a different browser. Request a new one.",
    "flow_state_expired": "This link has expired. Request a new one.",
    "otp_expired": "This link has expired. Request a new one.",
    "bad_code_verifier": "This link was opened in a different browser. Request a new one from this browser.",
    "session_not_found": "Your session has ended. Please sign in again.",
    "refresh_token_not_found": "Your session has ended. Please sign in again.",
    "refresh_token_already_used": "Your session has ended. Please sign in again.",
    "reauthentication_needed": "Please sign in again before changing your password.",
    "signup_disabled": "New registrations are closed.",
}

_client: httpx.Client | None = None
_lock = threading.Lock()
transport: httpx.BaseTransport | None = None   # tests inject a fake Supabase Auth server here


def enabled() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_PUBLISHABLE_KEY)


def _http() -> httpx.Client:
    global _client
    with _lock:
        if _client is None or (transport is not None and getattr(_client, "_nyaya_transport", None) is not transport):
            kw = {"transport": transport} if transport is not None else {}
            _client = httpx.Client(base_url=f"{config.SUPABASE_URL}/auth/v1", timeout=httpx.Timeout(20, connect=10), **kw)
            _client._nyaya_transport = transport
        return _client


def reset_client() -> None:
    global _client
    with _lock:
        _client = None


def _headers(access_token: str | None = None, admin: bool = False) -> dict:
    key = config.SUPABASE_SECRET_KEY if admin else config.SUPABASE_PUBLISHABLE_KEY
    h = {"apikey": key, "Content-Type": "application/json", "X-Client-Info": "nyaysutra-backend/1.0"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    elif admin and not key.startswith("sb_secret_"):
        h["Authorization"] = f"Bearer {key}"   # legacy service_role JWT
    return h


def _call(method: str, path: str, *, body: dict | None = None, params: dict | None = None,
          token: str | None = None, admin: bool = False) -> dict:
    if admin and not config.SUPABASE_SECRET_KEY:
        raise AuthError("Account administration is not configured on the server.", "not_configured", 503)
    try:
        r = _http().request(method, path, json=body, params=params, headers=_headers(token, admin))
    except httpx.HTTPError as exc:
        log.warning("Supabase Auth unreachable: %s", type(exc).__name__)
        raise AuthError("The sign-in service is unreachable. Check the internet connection and try again.",
                        "unreachable", 503) from exc
    if r.status_code >= 400:
        try:
            j = r.json()
        except ValueError:
            j = {}
        code = j.get("error_code") or j.get("code") or j.get("error") or ""
        code = code if isinstance(code, str) else ""
        log.info("Supabase Auth %s %s -> %s %s", method, path.split("?")[0], r.status_code, code)
        status = 429 if r.status_code == 429 else 401 if r.status_code in (401, 403) else 400
        raise AuthError(_MESSAGES.get(code, "The request could not be completed. Please try again."), code, status)
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}


def claims(access_token: str) -> dict:
    """Payload of an access token received directly from Supabase over TLS (not used for authorization of the
    browser: the browser never holds it). Gives the session id and assurance level (aal1 / aal2)."""
    try:
        part = access_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


# ── PKCE (e-mail links come back to the backend with a one-time code) ───────
def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# ── User flows ───────────────────────────────────────────────────────────────
def sign_in(email: str, password: str) -> dict:
    return _call("POST", "/token", params={"grant_type": "password"}, body={"email": email, "password": password})


def refresh(refresh_token: str) -> dict:
    return _call("POST", "/token", params={"grant_type": "refresh_token"}, body={"refresh_token": refresh_token})


def exchange_code(auth_code: str, verifier: str) -> dict:
    return _call("POST", "/token", params={"grant_type": "pkce"}, body={"auth_code": auth_code, "code_verifier": verifier})


def sign_up(email: str, password: str, display_name: str, challenge: str, redirect_to: str) -> dict:
    return _call("POST", "/signup", params={"redirect_to": redirect_to},
                 body={"email": email, "password": password, "data": {"display_name": display_name},
                       "code_challenge": challenge, "code_challenge_method": "s256"})


def resend_signup(email: str, redirect_to: str) -> dict:
    return _call("POST", "/resend", params={"redirect_to": redirect_to}, body={"type": "signup", "email": email})


def recover(email: str, challenge: str, redirect_to: str) -> dict:
    return _call("POST", "/recover", params={"redirect_to": redirect_to},
                 body={"email": email, "code_challenge": challenge, "code_challenge_method": "s256"})


def get_user(access_token: str) -> dict:
    return _call("GET", "/user", token=access_token)


def update_password(access_token: str, password: str) -> dict:
    return _call("PUT", "/user", token=access_token, body={"password": password})


def sign_out(access_token: str, scope: str = "local") -> None:
    try:
        _call("POST", "/logout", params={"scope": scope}, token=access_token)
    except AuthError as exc:
        if exc.code not in ("session_not_found", "") and exc.status != 401:
            raise


# ── MFA (TOTP) ───────────────────────────────────────────────────────────────
def mfa_enroll(access_token: str, friendly_name: str) -> dict:
    return _call("POST", "/factors", token=access_token,
                 body={"factor_type": "totp", "friendly_name": friendly_name, "issuer": "NyaySutra AI"})


def mfa_challenge(access_token: str, factor_id: str) -> dict:
    return _call("POST", f"/factors/{factor_id}/challenge", token=access_token)


def mfa_verify(access_token: str, factor_id: str, challenge_id: str, code: str) -> dict:
    return _call("POST", f"/factors/{factor_id}/verify", token=access_token,
                 body={"challenge_id": challenge_id, "code": code})


def mfa_unenroll(access_token: str, factor_id: str) -> None:
    _call("DELETE", f"/factors/{factor_id}", token=access_token)


def verified_factors(user: dict) -> list[dict]:
    return [f for f in user.get("factors") or [] if f.get("status") == "verified" and f.get("factor_type") == "totp"]


# ── Administration (secret key, server-side only) ───────────────────────────
def admin_get_user(user_id: str) -> dict:
    return _call("GET", f"/admin/users/{user_id}", admin=True)


def admin_create_user(email: str, password: str, display_name: str = "", email_confirm: bool = True) -> dict:
    return _call("POST", "/admin/users", admin=True,
                 body={"email": email, "password": password, "email_confirm": email_confirm,
                       "user_metadata": {"display_name": display_name}})


def admin_set_banned(user_id: str, banned: bool) -> dict:
    return _call("PUT", f"/admin/users/{user_id}", admin=True, body={"ban_duration": "876000h" if banned else "none"})


def admin_delete_factor(user_id: str, factor_id: str) -> None:
    _call("DELETE", f"/admin/users/{user_id}/factors/{factor_id}", admin=True)


def admin_delete_user(user_id: str) -> None:
    _call("DELETE", f"/admin/users/{user_id}", admin=True)
