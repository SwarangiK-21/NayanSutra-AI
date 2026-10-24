"""In-memory stand-in for the Supabase Auth (GoTrue) REST API, used through httpx.MockTransport.

It implements the endpoints the backend calls with Supabase's documented behaviour — password grant, refresh-token
rotation (a used refresh token is rejected), PKCE codes that work once, e-mail confirmation, TOTP factors
(enrol → challenge → verify, aal1 → aal2, real RFC 6238 codes), sign-out scopes, bans and admin factor removal —
so the security tests exercise the real backend code without network access. All data is synthetic."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx


def totp(secret_b32: str, at: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    counter = int((at or time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def _err(status: int, code: str, msg: str = "") -> httpx.Response:
    return httpx.Response(status, json={"code": status, "error_code": code, "msg": msg or code})


class FakeSupabaseAuth:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}          # session_id -> {user_id, aal, revoked}
        self.refresh: dict[str, str] = {}            # refresh token -> session_id (deleted once used)
        self.access: dict[str, str] = {}             # access token -> session_id
        self.codes: dict[str, dict] = {}             # PKCE auth code -> {user_id, challenge, flow}
        self.challenges: dict[str, str] = {}         # challenge id -> factor id
        self.sent: list[dict] = []                   # "e-mails": {to, kind, code, redirect_to}
        self.pending_flows: dict[str, dict] = {}     # email -> {challenge, flow, redirect_to}
        self.calls: list[tuple[str, str]] = []
        self.transport = httpx.MockTransport(self.handle)

    # ── helpers for tests ──
    def add_user(self, email: str, password: str, *, confirmed: bool = True, name: str = "") -> str:
        uid = str(uuid.uuid4())
        self.users[uid] = {"id": uid, "email": email.lower(), "password": password, "banned": False, "factors": [],
                           "email_confirmed_at": "2026-01-01T00:00:00Z" if confirmed else None,
                           "user_metadata": {"display_name": name or email.split("@")[0]}}
        return uid

    def factor_secret(self, uid: str) -> str:
        return next(f["secret"] for f in self.users[uid]["factors"])

    def last_code(self, kind: str) -> dict:
        return next(m for m in reversed(self.sent) if m["kind"] == kind)

    # ── token plumbing ──
    def _jwt(self, sid: str) -> str:
        s = self.sessions[sid]
        payload = {"sub": s["user_id"], "session_id": sid, "aal": s["aal"], "exp": int(time.time()) + 3600, "role": "authenticated"}
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        tok = f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc(payload)}.{secrets.token_urlsafe(16)}"
        self.access[tok] = sid
        return tok

    def _public_user(self, uid: str) -> dict:
        u = self.users[uid]
        return {"id": uid, "email": u["email"], "email_confirmed_at": u["email_confirmed_at"],
                "confirmed_at": u["email_confirmed_at"], "user_metadata": u["user_metadata"],
                "factors": [{"id": f["id"], "factor_type": "totp", "status": f["status"], "friendly_name": f["name"]}
                            for f in u["factors"]]}

    def _session_response(self, sid: str) -> httpx.Response:
        rt = secrets.token_urlsafe(24)
        self.refresh[rt] = sid
        return httpx.Response(200, json={"access_token": self._jwt(sid), "refresh_token": rt, "token_type": "bearer",
                                         "expires_in": 3600, "expires_at": int(time.time()) + 3600,
                                         "user": self._public_user(self.sessions[sid]["user_id"])})

    def _new_session(self, uid: str) -> str:
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"user_id": uid, "aal": "aal1", "revoked": False}
        return sid

    def _caller(self, req: httpx.Request) -> tuple[str, dict] | None:
        tok = (req.headers.get("authorization") or "")[7:]
        sid = self.access.get(tok)
        if not sid or self.sessions[sid]["revoked"]:
            return None
        return sid, self.sessions[sid]

    # ── request handler ──
    def handle(self, req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        path = url.path.replace("/auth/v1", "", 1)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = json.loads(req.content or b"{}") if req.content else {}
        self.calls.append((req.method, path))
        if not req.headers.get("apikey"):
            return _err(401, "no_api_key")
        admin = req.headers["apikey"].startswith("sb_secret_")

        if path == "/token":
            grant = q.get("grant_type")
            if grant == "password":
                u = next((x for x in self.users.values() if x["email"] == (body.get("email") or "").lower()), None)
                if not u or not hmac.compare_digest(u["password"], body.get("password") or ""):
                    return _err(400, "invalid_credentials", "Invalid login credentials")
                if u["banned"]:
                    return _err(400, "user_banned")
                if not u["email_confirmed_at"]:
                    return _err(400, "email_not_confirmed")
                return self._session_response(self._new_session(u["id"]))
            if grant == "refresh_token":
                sid = self.refresh.pop(body.get("refresh_token") or "", None)   # rotation: single use
                if not sid or self.sessions[sid]["revoked"] or self.users[self.sessions[sid]["user_id"]]["banned"]:
                    return _err(400, "refresh_token_not_found")
                return self._session_response(sid)
            if grant == "pkce":
                c = self.codes.pop(body.get("auth_code") or "", None)          # a code works once
                if not c:
                    return _err(404, "flow_state_not_found")
                verifier = body.get("code_verifier") or ""
                expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if expect != c["challenge"]:
                    return _err(400, "bad_code_verifier")
                if c["flow"] == "signup":   # opening the e-mail link is what confirms the address
                    self.users[c["user_id"]]["email_confirmed_at"] = "2026-01-01T00:00:00Z"
                return self._session_response(self._new_session(c["user_id"]))
            return _err(400, "unsupported_grant_type")

        if path in ("/signup", "/recover", "/resend") and req.method == "POST":
            email = (body.get("email") or "").lower()
            u = next((x for x in self.users.values() if x["email"] == email), None)
            if path == "/signup":
                if u:
                    return httpx.Response(200, json={"id": str(uuid.uuid4())})   # Supabase hides existing accounts
                uid = self.add_user(email, body["password"], confirmed=False, name=(body.get("data") or {}).get("display_name", ""))
                u = self.users[uid]
            if not u:
                return httpx.Response(200, json={})
            if path == "/resend" and u["email_confirmed_at"]:
                return httpx.Response(200, json={})
            code = secrets.token_urlsafe(24)
            flow = "recovery" if path == "/recover" else "signup"
            challenge = body.get("code_challenge") or self.pending_flows.get(email, {}).get("challenge")
            self.codes[code] = {"user_id": u["id"], "challenge": challenge, "flow": flow}
            self.pending_flows[email] = {"challenge": challenge, "flow": flow}
            self.sent.append({"to": email, "kind": flow, "code": code, "redirect_to": q.get("redirect_to")})
            if flow == "signup":
                u["email_confirmed_at"] = None
            return httpx.Response(200, json={})

        if path.startswith("/admin/"):
            if not admin:
                return _err(403, "not_admin")
            parts = path.split("/")[2:]
            if parts == ["users"] and req.method == "POST":
                uid = self.add_user(body["email"], body["password"], confirmed=bool(body.get("email_confirm")),
                                    name=(body.get("user_metadata") or {}).get("display_name", ""))
                return httpx.Response(200, json=self._public_user(uid))
            uid = parts[1] if len(parts) > 1 else ""
            if uid not in self.users:
                return _err(404, "user_not_found")
            if len(parts) == 2 and req.method == "GET":
                return httpx.Response(200, json=self._public_user(uid))
            if len(parts) == 2 and req.method == "PUT":
                if "ban_duration" in body:
                    self.users[uid]["banned"] = body["ban_duration"] != "none"
                    if self.users[uid]["banned"]:
                        for s in self.sessions.values():
                            if s["user_id"] == uid:
                                s["revoked"] = True
                return httpx.Response(200, json=self._public_user(uid))
            if len(parts) == 2 and req.method == "DELETE":
                del self.users[uid]
                return httpx.Response(200, json={})
            if len(parts) == 4 and parts[2] == "factors" and req.method == "DELETE":
                self.users[uid]["factors"] = [f for f in self.users[uid]["factors"] if f["id"] != parts[3]]
                for s in self.sessions.values():
                    if s["user_id"] == uid:
                        s["aal"] = "aal1"
                return httpx.Response(200, json={})
            return _err(404, "not_found")

        caller = self._caller(req)
        if caller is None:
            return _err(401, "session_not_found")
        sid, sess = caller
        u = self.users[sess["user_id"]]

        if path == "/user" and req.method == "GET":
            return httpx.Response(200, json=self._public_user(u["id"]))
        if path == "/user" and req.method == "PUT":
            if "password" in body:
                if body["password"] == u["password"]:
                    return _err(422, "same_password")
                u["password"] = body["password"]
            return httpx.Response(200, json=self._public_user(u["id"]))
        if path == "/logout":
            scope = q.get("scope", "global")
            for k, s in self.sessions.items():
                if s["user_id"] == u["id"] and (scope == "global" or (scope == "local" and k == sid) or
                                                (scope == "others" and k != sid)):
                    s["revoked"] = True
            return httpx.Response(204)
        if path == "/factors" and req.method == "POST":
            fid = str(uuid.uuid4())
            secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
            u["factors"].append({"id": fid, "status": "unverified", "secret": secret, "name": body.get("friendly_name")})
            return httpx.Response(200, json={"id": fid, "type": "totp", "totp": {
                "qr_code": "data:image/svg+xml;utf-8,<svg xmlns='http://www.w3.org/2000/svg'/>", "secret": secret,
                "uri": f"otpauth://totp/NyaySutra:{u['email']}?secret={secret}"}})
        if path.startswith("/factors/"):
            parts = path.split("/")
            fid = parts[2]
            f = next((x for x in u["factors"] if x["id"] == fid), None)
            if not f:
                return _err(404, "mfa_factor_not_found")
            if len(parts) == 4 and parts[3] == "challenge":
                cid = str(uuid.uuid4())
                self.challenges[cid] = fid
                return httpx.Response(200, json={"id": cid, "type": "totp", "expires_at": int(time.time()) + 300})
            if len(parts) == 4 and parts[3] == "verify":
                if self.challenges.pop(body.get("challenge_id") or "", None) != fid:
                    return _err(422, "mfa_challenge_expired")
                code = body.get("code") or ""
                now = time.time()
                if code not in {totp(f["secret"], now + d) for d in (-30, 0, 30)}:
                    return _err(422, "mfa_verification_failed")
                f["status"] = "verified"
                sess["aal"] = "aal2"
                return self._session_response(sid)
            if len(parts) == 3 and req.method == "DELETE":
                if f["status"] == "verified" and sess["aal"] != "aal2":
                    return _err(403, "insufficient_aal")
                u["factors"].remove(f)
                return httpx.Response(200, json={"id": fid})
        return _err(404, "not_found")
