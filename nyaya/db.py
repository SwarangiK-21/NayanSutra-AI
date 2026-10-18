"""SQLite storage: one local file holds cases, documents, chunks, the FTS5
keyword index, embeddings and chat history.

Why SQLite: no server, no cloud account, ACID, ships with Python, and FTS5 gives
BM25 keyword search for free. Vectors are stored as float32 blobs and searched
in memory by vectorindex.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    case_type TEXT NOT NULL,
    case_number TEXT DEFAULT '',
    court TEXT DEFAULT '',
    client_name TEXT DEFAULT '',
    client_role TEXT DEFAULT '',
    opposite_party TEXT DEFAULT '',
    filing_date TEXT,
    next_hearing_date TEXT,
    status TEXT DEFAULT 'Active / In Court',
    acts_sections TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_notes (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    author TEXT DEFAULT 'Advocate',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- collection: case (a matter's own files) | bare_act | draft | judgment (shared library)
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
    collection TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    pages INTEGER DEFAULT 0,
    chars INTEGER DEFAULT 0,
    n_chunks INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'processing',
    error TEXT,
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_sha ON documents(IFNULL(case_id, ''), sha256);
CREATE INDEX IF NOT EXISTS ix_documents_case ON documents(case_id);
CREATE INDEX IF NOT EXISTS ix_documents_collection ON documents(collection);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    header TEXT NOT NULL,
    text TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    label TEXT,          -- e.g. 'Section 73', 'Article 21', 'Order XXXIX Rule 1', 'paras 12-15'
    section TEXT,        -- normalised number for exact lookup, e.g. '73', '138A', 'XXXIX.1'
    title TEXT,
    chapter TEXT,
    emb BLOB
);
CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(doc_id, ord);
CREATE INDEX IF NOT EXISTS ix_chunks_section ON chunks(section);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    header, text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, header, text) VALUES (new.id, new.header, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, header, text) VALUES ('delete', old.id, old.header, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF header, text ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, header, text) VALUES ('delete', old.id, old.header, old.text);
    INSERT INTO chunks_fts(rowid, header, text) VALUES (new.id, new.header, new.text);
END;

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chats_case ON chats(case_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources TEXT DEFAULT '[]',
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages(chat_id, id);
"""

_local = threading.local()
# SQLite allows one writer at a time; serialising writes in-process avoids
# "database is locked" errors when ingestion and chat run together.
write_lock = threading.RLock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")  # 32 MB page cache
    return conn


def conn() -> sqlite3.Connection:
    """Per-thread connection; reconnects if the configured DB path changed."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != config.DB_PATH:
        if c is not None:
            c.close()
        c = _connect()
        _local.conn, _local.path = c, config.DB_PATH
    return c


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction."""
    with write_lock:
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def init() -> None:
    config.ensure_dirs()
    with write_lock:
        conn().executescript(SCHEMA)


def reset_connections() -> None:
    """Drop this thread's cached connection (used by tests that swap DB files)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


def get_meta(key: str, default: str | None = None) -> str | None:
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def row_to_dict(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ("meta", "sources")) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except ValueError:
                d[f] = {} if f == "meta" else []
    return d
