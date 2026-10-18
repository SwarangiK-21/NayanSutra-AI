"""Security controls for a local, single-user legal workspace that holds
privileged client documents.

Threats addressed
* Other websites in the same browser driving the local API (CSRF / DNS rebinding)
  → Host allow-list, SameSite=Strict HttpOnly session cookie, custom request
    header required on every state-changing call, Origin check.
* Other users on the network → server binds to 127.0.0.1; binding elsewhere
  requires APP_PASSWORD (enforced in run.py).
* Malicious uploads → extension allow-list, magic-byte check, size cap,
  server-generated storage names (no user-controlled paths), zip-bomb guard
  in parsing, PDFium for PDFs.
* XSS → strict Content-Security-Policy (no inline script, no third-party
  origins); the frontend escapes all text before rendering Markdown.
* Abuse / runaway cost → per-client rate limits on chat, upload and login.
* Information leaks → generic error messages; secrets never sent to the browser.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from . import config

COOKIE = "nyaya_session"
CSRF_HEADER = "x-nyaya-csrf"
SESSION_TTL = 12 * 3600

ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}
MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}

# Inline style *attributes* are allowed (layout tweaks in templates); inline <style>/<script> are not.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


def _secret() -> bytes:
    """Per-installation signing key, created once and kept outside the project folder."""
    path = config.HOME_DIR / "session.key"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


_SECRET = _secret()


def issue_token() -> str:
    exp = int(time.time()) + SESSION_TTL
    nonce = secrets.token_urlsafe(12)
    msg = f"{exp}.{nonce}"
    sig = hmac.new(_SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def valid_token(token: str | None) -> bool:
    if not token or token.count(".") != 2:
        return False
    exp, nonce, sig = token.split(".")
    expected = hmac.new(_SECRET, f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return exp.isdigit() and int(exp) > time.time()


def csrf_for(token: str) -> str:
    """CSRF value bound to the session (double-submit, but unforgeable without the key)."""
    return hmac.new(_SECRET, f"csrf:{token}".encode(), hashlib.sha256).hexdigest()[:32]


def check_password(candidate: str) -> bool:
    if not config.APP_PASSWORD:
        return True
    a = hashlib.sha256(candidate.encode()).digest()
    b = hashlib.sha256(config.APP_PASSWORD.encode()).digest()
    return hmac.compare_digest(a, b)


def host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0].strip("[]").lower() if not host_header.startswith("[") \
        else host_header.split("]")[0].strip("[").lower()
    return host in {h.lower() for h in config.ALLOWED_HOSTS} or host in {"127.0.0.1", "localhost", "::1"}


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    if not origin:
        return True  # same-origin GET navigations / non-browser clients carry no Origin
    m = re.match(r"^https?://([^/]+)$", origin)
    return bool(m) and host_header is not None and m.group(1).lower() == host_header.lower()


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


limiter = RateLimiter()

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ()&,]+")


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name  # drop any path the client sent
    name = _SAFE_NAME.sub("_", name).strip(" ._") or "document"
    stem, ext = os.path.splitext(name)
    return stem[:120] + ext.lower()[:8]


def sniff_ok(path: Path, ext: str) -> bool:
    """Content must match the claimed type (magic bytes)."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if ext == ".pdf":
        return head.lstrip()[:5] == b"%PDF-"
    if ext == ".docx":
        return head[:4] == b"PK\x03\x04"
    if ext in (".txt", ".md"):
        # Plain text: UTF-16 (has a BOM) or text without NUL bytes (rules out binaries renamed .txt).
        return head[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" not in head
    return False
