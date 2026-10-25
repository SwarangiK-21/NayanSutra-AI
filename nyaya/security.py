"""HTTP-level security controls for a legal workspace that holds privileged client documents.

Threats addressed
* Other websites in the same browser driving the API (CSRF / DNS rebinding)
  → Host allow-list, SameSite=Strict HttpOnly session cookie, custom request
    header required on every state-changing call (sessions.csrf_for), Origin check.
* Unauthenticated network access → per-user accounts (Supabase Auth, see accounts.py) or, in local single-user
  mode, loopback binding (binding elsewhere requires APP_PASSWORD, enforced in run.py).
* Malicious uploads → extension allow-list, magic-byte check, active-content checks (filescan.py), size cap,
  server-generated storage names (no user-controlled paths), zip-bomb guard in parsing, PDFium for PDFs.
* XSS → strict Content-Security-Policy (no inline script, no third-party
  origins); the frontend escapes all text before rendering Markdown.
* Abuse / runaway cost → per-client rate limits on chat, upload and sign-in; account lockout (accounts.py).
* Information leaks → generic error messages; secrets never sent to the browser; redacted logs (privacy.py).
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

ALLOWED_EXT = {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt",
               ".csv", ".tsv", ".xlsx", ".xls", ".ods", ".pptx",
               ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".csv": "text/csv; charset=utf-8", ".tsv": "text/tab-separated-values; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}
# Opened in the browser; every other type is downloaded (office files are never rendered by the app's origin).
INLINE_EXT = {".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}

# Inline style *attributes* are allowed (layout tweaks in templates); inline <style>/<script> are not.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


_secret_cache: bytes | None = None


def secret_key() -> bytes:
    """Server key for CSRF values and recovery-code hashes: APP_SECRET_KEY (shared by every app instance), else a
    per-installation key created once and kept outside the project folder."""
    global _secret_cache
    if config.APP_SECRET_KEY:
        return hashlib.sha256(config.APP_SECRET_KEY.encode()).digest()
    if _secret_cache:
        return _secret_cache
    path = config.HOME_DIR / "session.key"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            _secret_cache = data
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
    _secret_cache = data
    return data


def check_password(candidate: str) -> bool:
    """Local single-user mode only: the shared APP_PASSWORD (constant-time comparison)."""
    if not config.APP_PASSWORD:
        return True
    a = hashlib.sha256(candidate.encode()).digest()
    b = hashlib.sha256(config.APP_PASSWORD.encode()).digest()
    return hmac.compare_digest(a, b)


def cookie_secure() -> bool:
    """Mark cookies Secure (and send HSTS) when the app is served over HTTPS."""
    return config.PUBLIC_URL.startswith("https://")


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
    if ext in (".docx", ".xlsx", ".pptx", ".odt", ".ods"):
        return head[:4] == b"PK\x03\x04"   # zip container (contents checked in filescan / the parser)
    if ext in (".doc", ".xls"):
        return head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE compound file
    if ext == ".rtf":
        return head.lstrip()[:5] == b"{\\rtf"
    if ext == ".bmp":
        return head[:2] == b"BM"
    if ext == ".gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext in (".tif", ".tiff"):
        return head[:4] in (b"II*\x00", b"MM\x00*")
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext in (".txt", ".md", ".csv", ".tsv"):
        # Plain text: UTF-16 (has a BOM) or text without NUL bytes (rules out binaries renamed .txt).
        return head[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" not in head
    return False
