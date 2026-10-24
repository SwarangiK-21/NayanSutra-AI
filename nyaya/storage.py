"""File storage for uploaded client documents: Supabase Storage (private bucket) or the local folder.

Stored references in documents.path:
    sb://<bucket>/<case_id>/<doc_id><ext>     Supabase Storage object
    data/uploads/<case_id>/<doc_id><ext>      local file (offline mode and not-yet-migrated rows)
    data/library/...                          shipped law library (always local, part of the repository)

The Storage API is called only from this backend with the server-side secret key; the browser never receives a
Supabase key or a bucket URL — files are streamed through the authenticated /api/documents/{id}/file endpoint.
Object keys are server-generated (case id + document id), so client file names never reach a storage path.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import httpx

from . import config

log = logging.getLogger(__name__)

PREFIX = "sb://"


class StorageError(Exception):
    """Message is safe to show to the user."""


def remote_enabled() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SECRET_KEY)


def _headers(extra: dict | None = None) -> dict:
    key = config.SUPABASE_SECRET_KEY
    h = {"apikey": key}
    if not key.startswith("sb_"):            # legacy service_role JWT also goes in Authorization
        h["Authorization"] = f"Bearer {key}"
    return {**h, **(extra or {})}


def _client() -> httpx.Client:
    return httpx.Client(base_url=f"{config.SUPABASE_URL}/storage/v1", timeout=httpx.Timeout(120, connect=15))


def _request(method: str, url: str, attempts: int = 3, **kw) -> httpx.Response:
    """Storage call with retries on network errors and 5xx (requests are idempotent: fixed keys, upsert)."""
    last: Exception | None = None
    for i in range(attempts):
        if hasattr(kw.get("content"), "seek"):
            kw["content"].seek(0)   # a retried upload must resend the whole file, not the rest of a consumed stream
        try:
            with _client() as c:
                r = c.request(method, url, **kw)
            if r.status_code >= 500 and i < attempts - 1:
                time.sleep(1.5 * (i + 1))
                continue
            return r
        except httpx.HTTPError as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    raise StorageError("File storage is unreachable. Check the internet connection and try again.") from last


def parse(ref: str) -> tuple[str, str]:
    bucket, _, key = ref[len(PREFIX):].partition("/")
    return bucket, key


def is_remote(ref: str) -> bool:
    return (ref or "").startswith(PREFIX)


def object_key(case_id: str, doc_id: str, ext: str) -> str:
    safe_case = "".join(ch for ch in case_id if ch.isalnum() or ch in "-_")
    return f"{safe_case}/{doc_id}{ext.lower()}"


def put_file(local: Path, case_id: str, doc_id: str, ext: str, content_type: str) -> str:
    """Store an uploaded file; returns the reference to save in documents.path."""
    if not remote_enabled():
        folder = config.UPLOAD_DIR / "".join(ch for ch in case_id if ch.isalnum() or ch in "-_")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{doc_id}{ext.lower()}"
        shutil.copyfile(local, dest)
        return config.to_stored(dest)
    key = object_key(case_id, doc_id, ext)
    with open(local, "rb") as fh:
        # Upsert: a retried upload of the same document id overwrites its own partial attempt, never another file.
        r = _request("POST", f"/object/{config.SUPABASE_BUCKET}/{quote(key)}", content=fh,
                     headers=_headers({"Content-Type": content_type.split(";")[0], "x-upsert": "true",
                                       "cache-control": "no-store"}))
    if r.status_code >= 400:
        log.warning("Storage upload failed %s: %s", r.status_code, r.text[:300])
        if r.status_code == 413:
            raise StorageError("The file is larger than the storage limit.")
        if r.status_code in (400, 415) and "mime" in r.text.lower():
            raise StorageError("This file type is not accepted by the document store.")
        raise StorageError("The file could not be saved to document storage. Please try again.")
    return f"{PREFIX}{config.SUPABASE_BUCKET}/{key}"


def download_to(ref: str, dest: Path) -> None:
    bucket, key = parse(ref)
    with _client() as c:
        with c.stream("GET", f"/object/{bucket}/{quote(key)}", headers=_headers()) as r:
            if r.status_code == 404 or r.status_code == 400:
                raise StorageError("The stored file is missing.")
            if r.status_code >= 400:
                raise StorageError("The file could not be read from document storage.")
            with open(dest, "wb") as out:
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)


def stream(ref: str) -> Iterator[bytes]:
    """Yield the bytes of a stored object (used to serve downloads without buffering the whole file)."""
    bucket, key = parse(ref)
    c = _client()
    req = c.build_request("GET", f"/object/{bucket}/{quote(key)}", headers=_headers())
    r = c.send(req, stream=True)
    if r.status_code >= 400:
        r.close()
        c.close()
        raise StorageError("The stored file is missing." if r.status_code in (400, 404)
                           else "The file could not be read from document storage.")

    def gen():
        try:
            yield from r.iter_bytes(1 << 16)
        finally:
            r.close()
            c.close()
    return gen()


@contextmanager
def local_copy(ref: str, suffix: str = "") -> Iterator[Path]:
    """A local path for parsing: the file itself for local references, a temporary download for Storage objects."""
    if not is_remote(ref):
        yield config.from_stored(ref)
        return
    with tempfile.TemporaryDirectory(prefix="nyaya_dl_") as tmp:
        dest = Path(tmp) / f"file{suffix or Path(ref).suffix}"
        download_to(ref, dest)
        yield dest


def delete(ref: str) -> None:
    """Remove a stored upload. Missing objects are not an error (deletion is idempotent)."""
    if not is_remote(ref):
        p = config.from_stored(ref).resolve()
        if config.UPLOAD_DIR.resolve() in p.parents and p.exists():
            p.unlink()
        return
    bucket, key = parse(ref)
    r = _request("DELETE", f"/object/{bucket}", json={"prefixes": [key]}, headers=_headers())
    if r.status_code >= 400 and r.status_code != 404:
        log.warning("Storage delete failed %s: %s", r.status_code, r.text[:300])
        raise StorageError("The file could not be deleted from document storage.")


def signed_url(ref: str, expires_in: int = 300) -> str:
    """Short-lived URL for a private object (for server-side tools and verification; not handed to the browser)."""
    bucket, key = parse(ref)
    r = _request("POST", f"/object/sign/{bucket}/{quote(key)}", json={"expiresIn": int(expires_in)}, headers=_headers())
    if r.status_code >= 400:
        raise StorageError("Could not create a temporary link for this file.")
    return f"{config.SUPABASE_URL}/storage/v1{r.json()['signedURL']}"


def health() -> dict:
    if not remote_enabled():
        return {"backend": "local"}
    try:
        r = _request("GET", f"/bucket/{config.SUPABASE_BUCKET}", attempts=1, headers=_headers())
        ok = r.status_code == 200
        return {"backend": "supabase", "bucket": config.SUPABASE_BUCKET, "ok": ok,
                "public": bool(r.json().get("public")) if ok else None}
    except StorageError:
        return {"backend": "supabase", "bucket": config.SUPABASE_BUCKET, "ok": False}
