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

# Versioned, additive migrations (applied once each, in order; recorded in meta.schema_version).
# Existing tables are extended rather than duplicated.
# Columns added to existing tables: (table, column, declaration). Added only when missing, so re-running is safe.
ADDED_COLUMNS = [
    ("cases", "opposite_role", "TEXT DEFAULT ''"),
    ("cases", "filing_number", "TEXT DEFAULT ''"),
    ("cases", "fir_number", "TEXT DEFAULT ''"),
    ("cases", "cnr_number", "TEXT DEFAULT ''"),
    ("cases", "state", "TEXT DEFAULT ''"),
    ("cases", "district", "TEXT DEFAULT ''"),
    ("cases", "police_station", "TEXT DEFAULT ''"),
    ("cases", "filing_year", "INTEGER"),
    ("cases", "case_subtype", "TEXT DEFAULT ''"),
    ("cases", "is_draft", "INTEGER DEFAULT 0"),
]

MIGRATIONS: list[str] = [
    # 1 — prefs, audit, notifications, integrations + sync log, source records, document derivatives,
    #     drafts + versions, case provisions (case columns come from ADDED_COLUMNS)
    """
    CREATE INDEX IF NOT EXISTS ix_cases_cnr ON cases(cnr_number);

    CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        action TEXT NOT NULL,          -- e.g. case.create, document.upload, draft.export, integration.sync
        entity TEXT NOT NULL,
        entity_id TEXT,
        detail TEXT DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);

    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,            -- case_update | source_update | document | system
        title TEXT NOT NULL,
        body TEXT DEFAULT '',
        link TEXT DEFAULT '',
        case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
        dedupe_key TEXT UNIQUE,
        created_at TEXT NOT NULL,
        read_at TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_notifications_created ON notifications(created_at);

    CREATE TABLE IF NOT EXISTS integrations (
        id TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'never',   -- never | ok | error | manual
        last_attempt_at TEXT,
        last_success_at TEXT,
        last_error TEXT,
        fail_count INTEGER NOT NULL DEFAULT 0,
        next_due_at TEXT,
        etag TEXT,
        last_modified TEXT
    );

    CREATE TABLE IF NOT EXISTS sync_log (
        id INTEGER PRIMARY KEY,
        integration_id TEXT NOT NULL,
        trigger TEXT NOT NULL,                  -- scheduled | manual
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,                           -- ok | not_modified | error
        items_new INTEGER DEFAULT 0,
        items_updated INTEGER DEFAULT 0,
        error TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_sync_log ON sync_log(integration_id, started_at);

    -- Records retrieved from (or imported by the user from) an external source.
    CREATE TABLE IF NOT EXISTS source_records (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,                   -- integration id
        external_id TEXT NOT NULL,              -- stable id at the source (guid, CNR, url)
        kind TEXT NOT NULL,                     -- news | case_record | order | act_version
        title TEXT NOT NULL,
        url TEXT DEFAULT '',
        jurisdiction TEXT DEFAULT '',
        published_at TEXT,
        retrieved_at TEXT NOT NULL,
        verified_at TEXT,
        content_hash TEXT NOT NULL,
        provenance TEXT NOT NULL,               -- feed | api | user_import
        data TEXT DEFAULT '{}',
        case_id TEXT REFERENCES cases(id) ON DELETE SET NULL,
        UNIQUE(source, external_id)
    );
    CREATE INDEX IF NOT EXISTS ix_source_records_case ON source_records(case_id);
    CREATE INDEX IF NOT EXISTS ix_source_records_kind ON source_records(kind, published_at);

    -- Derived text for a document: extracted text, English translation, summaries, extracted facts.
    CREATE TABLE IF NOT EXISTS doc_derivatives (
        doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,                     -- translation_en | summary_en | summary_hi | summary_mr | facts
        lang TEXT DEFAULT 'en',
        content TEXT NOT NULL,
        meta TEXT DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY (doc_id, kind)
    );

    CREATE TABLE IF NOT EXISTS drafts (
        id TEXT PRIMARY KEY,
        case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'draft',     -- draft | notice
        doc_type TEXT NOT NULL,
        forum TEXT DEFAULT '',
        style TEXT DEFAULT 'formal',
        lang TEXT DEFAULT 'en',
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        facts TEXT DEFAULT '[]',                -- [{text, origin: file|user|case|library, ref}]
        missing TEXT DEFAULT '[]',
        sources TEXT DEFAULT '[]',
        inputs TEXT DEFAULT '{}',
        version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_drafts_case ON drafts(case_id, updated_at);
    CREATE TABLE IF NOT EXISTS draft_versions (
        draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        content TEXT NOT NULL,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (draft_id, version)
    );

    CREATE TABLE IF NOT EXISTS case_provisions (
        id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        act TEXT NOT NULL,
        ref TEXT NOT NULL,                      -- e.g. 'Section 138'
        title TEXT DEFAULT '',
        chunk_id INTEGER,
        doc_id TEXT,
        page INTEGER,
        reason TEXT DEFAULT '',
        flags TEXT DEFAULT '[]',                -- temporal / repealed / uncertainty warnings
        verification TEXT NOT NULL,             -- verified_in_library | not_found
        status TEXT NOT NULL DEFAULT 'suggested', -- suggested | accepted | rejected | edited
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(case_id, act, ref)
    );
    """,
]

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
        c = conn()
        c.executescript(SCHEMA)
        for table, col, decl in ADDED_COLUMNS:
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        c.commit()
        row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        for n, script in enumerate(MIGRATIONS[version:], version + 1):
            # executescript commits on its own; the version bump follows only if every statement succeeded.
            c.executescript(script)
            c.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(n),))
            c.commit()


def audit(action: str, entity: str, entity_id: str | None = None, **detail: Any) -> None:
    """Append-only activity log (who-did-what is implicit: single-user local workspace)."""
    with tx() as c:
        c.execute("INSERT INTO audit_log(at, action, entity, entity_id, detail) VALUES(?,?,?,?,?)",
                  (now(), action, entity, entity_id, json.dumps(detail, ensure_ascii=False, default=str)[:4000]))


def notify(kind: str, title: str, body: str = "", link: str = "", case_id: str | None = None,
           dedupe_key: str | None = None) -> bool:
    """In-app notification; a repeated dedupe_key is ignored (duplicate prevention). Returns True if created."""
    with tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO notifications(kind, title, body, link, case_id, dedupe_key, created_at)"
                        " VALUES(?,?,?,?,?,?,?)", (kind, title[:300], body[:2000], link[:500], case_id, dedupe_key, now()))
        return cur.rowcount > 0


def get_pref(key: str, default: str | None = None) -> str | None:
    row = conn().execute("SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_pref(key: str, value: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO prefs(key, value, updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (key, value, now()))


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
