"""Supabase storage layer tests.

Offline (always run): SQL translation for Postgres, row behaviour, the Storage client against httpx.MockTransport
(clearly a mock — no Supabase service is contacted), upload/metadata consistency.
Live (opt-in): set NYAYA_LIVE_SUPABASE=1 with SUPABASE_DB_URL / SUPABASE_URL / SUPABASE_SECRET_KEY to run CRUD and
file round-trips against the real project using synthetic records that are deleted afterwards.
"""
import hashlib
import io
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nyaya import config, db, storage

SYNTH = "[SYNTHETIC TEST RECORD]"


# ── SQL translation and rows ────────────────────────────────────────────────
def test_placeholders_and_percent_escaping():
    assert db._to_pg("SELECT * FROM t WHERE a=? AND b LIKE ?") == "SELECT * FROM t WHERE a=%s AND b LIKE %s"
    assert db._to_pg("SELECT '?' AS q, x FROM t WHERE y=?") == "SELECT '?' AS q, x FROM t WHERE y=%s"
    assert db._to_pg("SELECT * FROM t WHERE p NOT LIKE 'sb://%' AND id=?") == \
        "SELECT * FROM t WHERE p NOT LIKE 'sb://%%' AND id=%s"


def test_row_behaves_like_sqlite_row():
    r = db.Row({"id": 7, "title": "x"})
    assert r["id"] == 7 and r[0] == 7 and r[1] == "x" and dict(r) == {"id": 7, "title": "x"}
    res = db.Result([db.Row({"n": 1}), db.Row({"n": 2})], 2)
    assert res.fetchone()["n"] == 1 and [x["n"] for x in res.fetchall()] == [2] and res.fetchone() is None
    assert db.row_to_dict(db.Row({"meta": '{"a": 1}'}))["meta"] == {"a": 1}


def test_backend_selection(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "")
    assert db.backend() == "sqlite"
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "postgresql://u@h/db")
    assert db.backend() == "postgres"


def test_keyword_query_syntax_per_engine(monkeypatch):
    from nyaya import retrieval
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "")
    assert retrieval.fts_query("Is the deposit refundable?") == '"deposit" OR "refundable"'
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "postgresql://u@h/db")
    assert retrieval.fts_query("Is the deposit refundable?") == "deposit | refundable"
    assert retrieval.fts_query("x'); DROP TABLE users;--") == "drop | table | users"   # no syntax can be injected
    # Postgres filter: with 3+ keywords at least two must co-occur (index-friendly); names may match alone
    assert retrieval.pg_filter("deposit refundable") == "deposit | refundable"
    assert retrieval.pg_filter("Is the security deposit refundable") == \
        "(security & deposit) | (security & refundable) | (deposit & refundable)"
    assert retrieval.pg_filter("death sentence of Kasab confirmed") .endswith("| kasab")
    assert retrieval.pg_filter("x'); DROP TABLE users; -- & | !") == "(drop & table) | (drop & users) | (table & users)"
    # common words (every catalogue entry says "Supreme Court of India") are not keyword terms
    assert retrieval.fts_query("Supreme Court of India judgment on bail") == "bail"


def test_migrations_cover_every_table():
    sql = "".join(p.read_text("utf-8") for p in sorted((config.ROOT / "supabase" / "migrations").glob("*.sql")))
    for table in ("meta", "cases", "case_notes", "documents", "chunks", "chats", "messages", "prefs", "audit_log",
                  "notifications", "integrations", "sync_log", "source_records", "doc_derivatives", "drafts",
                  "draft_versions", "case_provisions"):
        assert f"create table if not exists public.{table} " in sql, table
        assert f"'{table}'" in sql, f"RLS list misses {table}"
    assert "public, false" not in sql and "revoke all on all tables in schema public from anon, authenticated" in sql


# ── Storage client (MOCKED Supabase Storage) ────────────────────────────────
class FakeStorage:
    """In-memory stand-in for the Supabase Storage REST API (a mock, not the real service)."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[httpx.Request] = []
        self.fail_next = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503)
        path = req.url.path.removeprefix("/storage/v1")
        if req.method == "POST" and path.startswith("/object/sign/"):
            return httpx.Response(200, json={"signedURL": f"{path}?token=t"})
        if req.method == "POST" and path.startswith("/object/"):
            key = path[len("/object/"):]
            if req.headers.get("content-type") not in ("text/plain", "application/pdf"):
                return httpx.Response(400, json={"message": "mime type not supported"})
            self.objects[key] = req.read()
            return httpx.Response(200, json={"Key": key})
        if req.method == "GET" and path.startswith("/object/"):
            key = path[len("/object/"):]
            return httpx.Response(200, content=self.objects[key]) if key in self.objects else httpx.Response(400)
        if req.method == "DELETE" and path.startswith("/object/"):
            bucket = path[len("/object/"):]
            for p in json.loads(req.content)["prefixes"]:
                self.objects.pop(f"{bucket}/{p}", None)
            return httpx.Response(200, json=[])
        if req.method == "GET" and path.startswith("/bucket/"):
            return httpx.Response(200, json={"id": "case-files", "public": False})
        return httpx.Response(404)


@pytest.fixture
def fake_storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr(config, "SUPABASE_BUCKET", "case-files")
    monkeypatch.setattr(storage, "_client", lambda: httpx.Client(
        base_url="https://example.supabase.co/storage/v1", transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    return fake


def test_storage_roundtrip_retry_and_headers(fake_storage, tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"lease text")
    fake_storage.fail_next = 1                                   # transient 503 is retried
    ref = storage.put_file(f, "CASE-CIV-x", "doc_1", ".txt", "text/plain; charset=utf-8")
    assert ref == "sb://case-files/CASE-CIV-x/doc_1.txt"
    req = fake_storage.calls[-1]
    assert req.headers["apikey"] == "sb_secret_test" and "authorization" not in req.headers   # new keys: apikey only
    assert req.headers["x-upsert"] == "true" and req.headers["content-type"] == "text/plain"
    with storage.local_copy(ref) as p:
        assert p.read_bytes() == b"lease text" and p.suffix == ".txt"
    assert b"".join(storage.stream(ref)) == b"lease text"
    assert storage.signed_url(ref, 60).startswith("https://example.supabase.co/storage/v1/object/sign/")
    storage.delete(ref)
    storage.delete(ref)                                           # idempotent
    with pytest.raises(storage.StorageError):
        storage.download_to(ref, tmp_path / "x")
    assert storage.health() == {"backend": "supabase", "bucket": "case-files", "ok": True, "public": False}


def test_storage_rejects_unknown_type_and_network_failure(fake_storage, tmp_path, monkeypatch):
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(storage.StorageError, match="file type"):
        storage.put_file(f, "C", "d", ".png", "image/png")

    def down(req):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(storage, "_client", lambda: httpx.Client(transport=httpx.MockTransport(down),
                                                                 base_url="https://example.supabase.co/storage/v1"))
    with pytest.raises(storage.StorageError, match="unreachable"):
        storage.put_file(tmp_path / "a.png", "C", "d", ".png", "image/png")


def test_upload_flow_uses_storage_and_keeps_metadata_consistent(fresh_db, fake_storage, monkeypatch):
    from nyaya import ingest
    from nyaya.server import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        c.headers["X-Nyaya-CSRF"] = c.get("/api/session").json()["csrf"]
        cid = c.post("/api/cases", json={"title": SYNTH, "client_name": "Asha Rao"}).json()["id"]
        r = c.post(f"/api/cases/{cid}/documents", files={"file": ("lease.txt", b"1. Deposit is refundable.\n" * 20, "text/plain")})
        assert r.status_code == 200, r.text
        doc_id = r.json()["id"]
        for _ in range(100):
            if c.get(f"/api/documents/{doc_id}").json()["status"] != "processing":
                break
            time.sleep(0.05)
        row = db.conn().execute("SELECT path, status FROM documents WHERE id=?", (doc_id,)).fetchone()
        assert row["path"] == f"sb://case-files/{cid}/{doc_id}.txt" and row["status"] == "ready"
        assert not (config.UPLOAD_DIR / cid).exists()                 # nothing written to the local upload folder
        got = c.get(f"/api/documents/{doc_id}/file")
        assert got.status_code == 200 and got.content.startswith(b"1. Deposit") and "sandbox" in got.headers["content-security-policy"]
        # A failed metadata write removes the just-uploaded object (no file without its record).
        monkeypatch.setattr(ingest, "register", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        before = set(fake_storage.objects)
        with pytest.raises(RuntimeError):
            c.post(f"/api/cases/{cid}/documents", files={"file": ("b.txt", b"other text " * 30, "text/plain")})
        assert set(fake_storage.objects) == before
        monkeypatch.undo()
        # Storage outage: clear message, nothing recorded.
        monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setattr(config, "SUPABASE_SECRET_KEY", "sb_secret_test")
        monkeypatch.setattr(storage, "put_file", lambda *a, **k: (_ for _ in ()).throw(storage.StorageError("File storage is unreachable.")))
        r = c.post(f"/api/cases/{cid}/documents", files={"file": ("c.txt", b"third text " * 30, "text/plain")})
        assert r.status_code == 502 and "unreachable" in r.json()["detail"]
        assert db.conn().execute("SELECT COUNT(*) FROM documents WHERE case_id=?", (cid,)).fetchone()[0] == 1
        monkeypatch.undo()


# ── Live Supabase (opt-in) ──────────────────────────────────────────────────
LIVE = os.getenv("NYAYA_LIVE_SUPABASE") == "1"   # credentials are read from .env inside the test


@pytest.mark.skipif(not LIVE, reason="set NYAYA_LIVE_SUPABASE=1 and Supabase credentials to run against the real project")
def test_live_supabase_crud_files_and_access(monkeypatch, tmp_path):
    from dotenv import dotenv_values
    env = dotenv_values(config.ROOT / ".env")
    for k in ("SUPABASE_DB_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY"):
        monkeypatch.setattr(config, k, os.getenv(k) or env.get(k, ""))
    from nyaya import cases
    db.close_pool()
    assert db.backend() == "postgres"
    token = db.set_identity(db.Identity(system=True))            # storage-layer test: internal context (RLS: system)
    db.init()                                                     # schema version check
    case = cases.create_case({"title": f"{SYNTH} live", "client_name": "Asha Rao", "case_type": "Civil"})
    try:
        assert cases.get_case(case["id"])["title"].startswith(SYNTH)
        cases.update_case(case["id"], {"description": "updated"})
        assert cases.get_case(case["id"])["description"] == "updated"
        f = tmp_path / "x.txt"
        f.write_bytes(b"synthetic file " * 10)
        ref = storage.put_file(f, case["id"], "doc_livetest", ".txt", "text/plain")
        with storage.local_copy(ref) as p:
            assert hashlib.sha256(p.read_bytes()).hexdigest() == hashlib.sha256(f.read_bytes()).hexdigest()
        # The bucket is private: an unauthenticated request is refused.
        bucket, key = storage.parse(ref)
        anon = httpx.get(f"{config.SUPABASE_URL}/storage/v1/object/public/{bucket}/{key}", timeout=20)
        assert anon.status_code >= 400
        storage.delete(ref)
    finally:
        assert cases.delete_case(case["id"])
        db.reset_identity(token)
        db.close_pool()
