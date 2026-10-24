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
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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
    CREATE INDEX IF NOT EXISTS ix_notifications_case ON notifications(case_id);

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
    # 2 — identities, roles, case membership, server-side sessions (mirrors supabase/migrations/…05)
    """
    CREATE TABLE IF NOT EXISTS organizations (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
    INSERT OR IGNORE INTO organizations(id, name, created_at)
        VALUES('org_default', 'NyaySutra workspace', strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'));

    CREATE TABLE IF NOT EXISTS profiles (
        user_id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',  -- active | disabled
        mfa_enabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_profiles_email ON profiles(lower(email));

    CREATE TABLE IF NOT EXISTS memberships (
        org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('client', 'advocate', 'admin', 'auditor')),
        created_at TEXT NOT NULL,
        created_by TEXT,
        PRIMARY KEY (org_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS ix_memberships_user ON memberships(user_id);

    CREATE TABLE IF NOT EXISTS invitations (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        email TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('client', 'advocate', 'admin', 'auditor')),
        invited_by TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        accepted_at TEXT,
        accepted_by TEXT,
        meta TEXT DEFAULT '{}'
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_invitations_open ON invitations(org_id, lower(email)) WHERE accepted_at IS NULL;

    CREATE TABLE IF NOT EXISTS case_members (
        case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
        access TEXT NOT NULL CHECK (access IN ('owner', 'advocate', 'client')),
        added_by TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (case_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS ix_case_members_user ON case_members(user_id);

    CREATE TABLE IF NOT EXISTS user_prefs (user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                                           updated_at TEXT NOT NULL, PRIMARY KEY (user_id, key));
    CREATE TABLE IF NOT EXISTS org_settings (org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                                             key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
                                             updated_by TEXT, PRIMARY KEY (org_id, key));

    CREATE TABLE IF NOT EXISTS app_sessions (
        id TEXT PRIMARY KEY,                    -- SHA-256 of the cookie token
        public_id TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL,
        org_id TEXT,
        state TEXT NOT NULL DEFAULT 'active',   -- active | mfa_pending | mfa_enroll | recovery
        aal TEXT NOT NULL DEFAULT 'aal1',
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        ip TEXT DEFAULT '',
        user_agent TEXT DEFAULT '',
        sb_session_id TEXT,
        token_ref TEXT,
        access_expires_at TEXT,
        mfa_attempts INTEGER NOT NULL DEFAULT 0,
        revoked_at TEXT,
        meta TEXT DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS ix_app_sessions_user ON app_sessions(user_id);

    CREATE TABLE IF NOT EXISTS recovery_codes (
        user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        used_at TEXT,
        PRIMARY KEY (user_id, code_hash)
    );
    CREATE TABLE IF NOT EXISTS auth_throttle (key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                                              window_start TEXT NOT NULL, locked_until TEXT);

    ALTER TABLE cases ADD COLUMN org_id TEXT REFERENCES organizations(id);
    ALTER TABLE cases ADD COLUMN created_by TEXT;
    ALTER TABLE documents ADD COLUMN uploaded_by TEXT;
    ALTER TABLE chats ADD COLUMN user_id TEXT;
    ALTER TABLE chats ADD COLUMN org_id TEXT;
    ALTER TABLE drafts ADD COLUMN created_by TEXT;
    ALTER TABLE drafts ADD COLUMN org_id TEXT;
    ALTER TABLE notifications ADD COLUMN user_id TEXT;
    ALTER TABLE audit_log ADD COLUMN actor_id TEXT;
    ALTER TABLE audit_log ADD COLUMN actor_role TEXT;
    ALTER TABLE audit_log ADD COLUMN org_id TEXT;
    ALTER TABLE audit_log ADD COLUMN ip TEXT;
    ALTER TABLE audit_log ADD COLUMN outcome TEXT DEFAULT 'ok';
    CREATE INDEX IF NOT EXISTS ix_cases_org ON cases(org_id);
    CREATE INDEX IF NOT EXISTS ix_chats_user ON chats(user_id, updated_at);
    CREATE INDEX IF NOT EXISTS ix_drafts_owner ON drafts(created_by, updated_at);
    CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, created_at);
    CREATE INDEX IF NOT EXISTS ix_audit_org ON audit_log(org_id, id);

    UPDATE cases SET org_id = 'org_default' WHERE org_id IS NULL;
    UPDATE chats SET org_id = COALESCE((SELECT c.org_id FROM cases c WHERE c.id = chats.case_id), 'org_default');
    UPDATE drafts SET org_id = COALESCE((SELECT c.org_id FROM cases c WHERE c.id = drafts.case_id), 'org_default');
    INSERT OR IGNORE INTO org_settings(org_id, key, value, updated_at)
        SELECT 'org_default', key, value, updated_at FROM prefs WHERE key = 'chat_retention_days' OR key LIKE 'forum_reqs:%';

    -- One user's import of a court record is separate from another user's import of the same record.
    CREATE TABLE source_records_v2 (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT DEFAULT '',
        jurisdiction TEXT DEFAULT '',
        published_at TEXT,
        retrieved_at TEXT NOT NULL,
        verified_at TEXT,
        content_hash TEXT NOT NULL,
        provenance TEXT NOT NULL,
        data TEXT DEFAULT '{}',
        case_id TEXT REFERENCES cases(id) ON DELETE SET NULL,
        created_by TEXT,
        org_id TEXT
    );
    INSERT INTO source_records_v2 SELECT id, source, external_id, kind, title, url, jurisdiction, published_at,
        retrieved_at, verified_at, content_hash, provenance, data, case_id, NULL,
        CASE WHEN kind = 'case_record' THEN 'org_default' END FROM source_records;
    DROP TABLE source_records;
    ALTER TABLE source_records_v2 RENAME TO source_records;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_source_records_owner ON source_records(source, external_id, IFNULL(created_by, ''));
    CREATE INDEX IF NOT EXISTS ix_source_records_case ON source_records(case_id);
    CREATE INDEX IF NOT EXISTS ix_source_records_kind ON source_records(kind, published_at);

    -- The creator of a case is its first owner.
    CREATE TRIGGER IF NOT EXISTS trg_case_owner AFTER INSERT ON cases WHEN new.created_by IS NOT NULL BEGIN
        INSERT OR IGNORE INTO case_members(case_id, user_id, access, added_by, created_at)
        VALUES (new.id, new.created_by, 'owner', new.created_by, new.created_at);
    END;
    """,
]

_local = threading.local()
# SQLite allows one writer at a time; serialising writes in-process avoids
# "database is locked" errors when ingestion and chat run together.
write_lock = threading.RLock()

# Postgres schema version this code expects (supabase/migrations/*). The app never runs DDL against Postgres:
# migrations are applied by the project owner (Supabase CLI / dashboard / MCP), and the app checks the version.
PG_SCHEMA_VERSION = 2
DEFAULT_ORG = "org_default"


# ── Identity of the code that is running (who the database should treat as the caller) ──
@dataclass(frozen=True)
class Identity:
    """The signed-in user (or the internal system) on whose behalf statements run.

    Postgres receives it as `request.jwt.claims`, exactly like PostgREST, so row-level security policies use
    Supabase's auth.uid(). The default is anonymous: code that forgets to set an identity sees only public data."""
    user_id: str | None = None
    org_id: str | None = None
    role: str = "guest"
    system: bool = False
    actor: str | None = None   # for system work started by a user: who to attribute it to in the audit log
    ip: str | None = None

    def claims(self) -> str:
        if self.system:
            return json.dumps({"nyaya_system": True})
        if self.user_id:
            return json.dumps({"sub": self.user_id, "org_id": self.org_id, "nyaya_role": self.role})
        return "{}"


ANONYMOUS = Identity()
_identity: ContextVar[Identity] = ContextVar("nyaya_identity", default=ANONYMOUS)


def current() -> Identity:
    return _identity.get()


def set_identity(ident: Identity):
    """Bind an identity to the current context (returns a token for reset_identity)."""
    return _identity.set(ident)


def reset_identity(token) -> None:
    _identity.reset(token)


@contextmanager
def as_identity(ident: Identity) -> Iterator[Identity]:
    token = _identity.set(ident)
    try:
        yield ident
    finally:
        _identity.reset(token)


@contextmanager
def system() -> Iterator[Identity]:
    """Run internal work (ingestion, sync, sign-in bookkeeping) with database-wide access.

    The acting user, if any, is kept for audit attribution. Statements inside never join a transaction opened by
    the caller under a different identity: they use their own connection."""
    cur = current()
    ident = Identity(system=True, actor=cur.user_id or cur.actor, org_id=cur.org_id, role=cur.role, ip=cur.ip)
    stash = getattr(_local, "pgtx", None)
    _local.pgtx = None
    try:
        with as_identity(ident):
            yield ident
    finally:
        _local.pgtx = stash


def backend() -> str:
    """'postgres' (Supabase) when SUPABASE_DB_URL is set, otherwise the local SQLite file."""
    return "postgres" if config.SUPABASE_DB_URL else "sqlite"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ── SQLite ───────────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")  # 32 MB page cache
    return conn


def _sqlite_conn() -> sqlite3.Connection:
    """Per-thread connection; reconnects if the configured DB path changed."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != config.DB_PATH:
        if c is not None:
            c.close()
        c = _connect()
        _local.conn, _local.path = c, config.DB_PATH
    return c


# ── Postgres (Supabase) ──────────────────────────────────────────────────────
# Application SQL is written once with '?' placeholders and constructs both engines understand
# (ON CONFLICT, RETURNING, COALESCE, ->> on JSON). Only full-text search differs (see retrieval.lexical).
_pool = None
_pool_lock = threading.Lock()


class Row(dict):
    """Row usable like sqlite3.Row: row['col'], row[0] and dict(row)."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description or []]
    return lambda values: Row(zip(names, values))


def _to_pg(sql: str) -> str:
    """'?' placeholders -> '%s' (outside quoted literals); literal '%' escaped for psycopg."""
    out, quoted = [], False
    for ch in sql:
        if ch == "'":
            quoted = not quoted
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not quoted:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class Result:
    """Materialised result with the cursor API the code uses (fetchone / fetchall / iteration / rowcount)."""

    def __init__(self, rows: list, rowcount: int):
        self._rows, self.rowcount, self._i = rows, rowcount, 0

    def fetchone(self):
        if self._i < len(self._rows):
            self._i += 1
            return self._rows[self._i - 1]
        return None

    def fetchall(self):
        rows, self._i = self._rows[self._i:], len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


def _run(pgconn, sql: str, params=()) -> Result:
    # psycopg parses placeholders (and '%') only when parameters are passed.
    cur = pgconn.execute(_to_pg(sql), tuple(params)) if params else pgconn.execute(sql)
    rows = cur.fetchall() if cur.description else []
    return Result(rows, cur.rowcount)


# Claims last applied to each pooled connection, so the identity is only re-sent when it changes.
_applied: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()


def _bind_identity(pgconn) -> None:
    """Make Postgres see the current caller (request.jwt.claims) before a statement or transaction runs.

    Applied outside any transaction (session level) so a rollback can never revert it behind the cache's back."""
    claims = current().claims()
    if _applied.get(pgconn) == claims:
        return
    try:
        pgconn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims,))
    except BaseException:
        _applied.pop(pgconn, None)
        raise
    _applied[pgconn] = claims


class PgSession:
    """Statement runner bound to one pooled connection (inside tx) or borrowing one per statement (autocommit)."""

    def __init__(self, pgconn=None):
        self._c = pgconn

    def execute(self, sql: str, params=()) -> Result:
        if self._c is not None:
            return _run(self._c, sql, params)
        import psycopg

        reads = sql.lstrip()[:6].upper() == "SELECT"
        for attempt in range(2):
            try:
                with _get_pool().connection() as c:
                    _bind_identity(c)
                    return _run(c, sql, params)
            except psycopg.OperationalError:
                # A dropped pooled connection is retried once — only for reads, so a write is never applied twice.
                if attempt or not reads:
                    raise
        raise RuntimeError("unreachable")

    def executemany(self, sql: str, seq) -> None:
        if self._c is None:
            with tx() as t:
                return t.executemany(sql, seq)
        with self._c.cursor() as cur:
            cur.executemany(_to_pg(sql), [tuple(p) for p in seq])


def _configure(pgconn) -> None:
    from psycopg.types.string import TextLoader

    # JSON columns come back as text, exactly like SQLite, so the existing json.loads() handling is unchanged.
    pgconn.adapters.register_loader("jsonb", TextLoader)
    pgconn.adapters.register_loader("json", TextLoader)


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool

            kwargs = {"autocommit": True, "row_factory": _row_factory, "prepare_threshold": None,
                      "connect_timeout": 15, "application_name": "nyaysutra"}
            if "sslmode" not in config.SUPABASE_DB_URL:
                kwargs["sslmode"] = "require"   # never fall back to an unencrypted connection
            _pool = ConnectionPool(
                config.SUPABASE_DB_URL, min_size=1, max_size=config.DB_POOL_MAX, timeout=30, open=True,
                configure=_configure, check=ConnectionPool.check_connection, kwargs=kwargs)
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# ── Public API used by every module ──────────────────────────────────────────
def conn():
    """Run a statement: SQLite per-thread connection, or Postgres (joins the open transaction on this thread)."""
    if backend() == "sqlite":
        return _sqlite_conn()
    return getattr(_local, "pgtx", None) or PgSession()


@contextmanager
def tx() -> Iterator[Any]:
    """Write transaction (atomic). SQLite: serialised; Postgres: one pooled connection, nested calls join it."""
    if backend() == "sqlite":
        with write_lock:
            c = _sqlite_conn()
            try:
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return
    open_tx = getattr(_local, "pgtx", None)
    if open_tx is not None:
        yield open_tx
        return
    with _get_pool().connection() as c:
        _bind_identity(c)
        with c.transaction():
            session = PgSession(c)
            _local.pgtx = session
            try:
                yield session
            finally:
                _local.pgtx = None


def init() -> None:
    config.ensure_dirs()
    if backend() == "postgres":
        row = conn().execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version < PG_SCHEMA_VERSION:
            raise RuntimeError(f"Supabase schema version {version} < {PG_SCHEMA_VERSION}: apply supabase/migrations "
                               "(see docs/SUPABASE_MIGRATION.md) before starting the app.")
        return
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


# Audit details never carry document contents, credentials or codes, only what happened to which record.
_AUDIT_DROP = ("content", "text", "password", "token", "secret", "code", "answer", "message", "prompt")


def _audit_detail(detail: dict) -> str:
    clean = {k: ("[redacted]" if any(w in k.lower() for w in _AUDIT_DROP) else v) for k, v in detail.items()}
    return json.dumps(clean, ensure_ascii=False, default=str)[:4000]


def audit(action: str, entity: str, entity_id: str | None = None, *, outcome: str = "ok", **detail: Any) -> None:
    """Append-only activity log: who (actor, role, organisation, IP) did what to which record, and the outcome."""
    ident = current()
    actor = ident.actor if ident.system else ident.user_id
    org = ident.org_id or DEFAULT_ORG   # single-organisation installs: unattributed events belong to it
    with system():  # written by the backend itself, so a user can never forge another actor's entry
        with tx() as c:
            c.execute("INSERT INTO audit_log(at, action, entity, entity_id, detail, actor_id, actor_role, org_id, ip, outcome)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (now(), action, entity, entity_id, _audit_detail(detail), actor,
                       "system" if ident.system and not actor else ident.role, org, ident.ip, outcome))


def _recipients(kind: str, case_id: str | None) -> list[str]:
    if case_id:
        rows = conn().execute(
            "SELECT cm.user_id FROM case_members cm JOIN profiles p ON p.user_id = cm.user_id "
            "WHERE cm.case_id = ? AND p.status = 'active'", (case_id,))
    elif kind == "system":
        rows = conn().execute("SELECT DISTINCT m.user_id FROM memberships m JOIN profiles p ON p.user_id = m.user_id "
                              "WHERE m.role = 'admin' AND p.status = 'active'")
    else:  # public updates (legal news, statute changes) go to everyone with access to the workspace
        rows = conn().execute("SELECT DISTINCT m.user_id FROM memberships m JOIN profiles p ON p.user_id = m.user_id "
                              "WHERE p.status = 'active'")
    return [r["user_id"] for r in rows]


def notify(kind: str, title: str, body: str = "", link: str = "", case_id: str | None = None,
           dedupe_key: str | None = None, recipients: list[str] | None = None) -> bool:
    """In-app notification for each recipient (the case team, administrators, or everyone for public updates).

    A repeated dedupe_key is ignored per recipient (duplicate prevention). Returns True if any was created.
    Call it outside an open transaction."""
    created = False
    with system():
        users = recipients if recipients is not None else _recipients(kind, case_id)
        if not users:
            return False
        with tx() as c:
            for uid in dict.fromkeys(users):
                cur = c.execute("INSERT INTO notifications(kind, title, body, link, case_id, dedupe_key, created_at, user_id)"
                                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                                (kind, title[:300], body[:2000], link[:500], case_id,
                                 f"{dedupe_key}:{uid}" if dedupe_key else None, now(), uid))
                created = created or cur.rowcount > 0
    return created


def get_pref(key: str, default: str | None = None) -> str | None:
    """Installation-wide value (system use only; user and organisation settings have their own tables)."""
    with system():
        row = conn().execute("SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_pref(key: str, value: str) -> None:
    with system():
        with tx() as c:
            c.execute("INSERT INTO prefs(key, value, updated_at) VALUES(?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (key, value, now()))


def get_user_pref(key: str, default: str | None = None, user_id: str | None = None) -> str | None:
    uid = user_id or current().user_id
    if not uid:
        return default
    row = conn().execute("SELECT value FROM user_prefs WHERE user_id=? AND key=?", (uid, key)).fetchone()
    return row["value"] if row else default


def user_prefs(user_id: str | None = None) -> dict[str, str]:
    uid = user_id or current().user_id
    if not uid:
        return {}
    return {r["key"]: r["value"] for r in conn().execute("SELECT key, value FROM user_prefs WHERE user_id=?", (uid,))}


def set_user_pref(key: str, value: str, user_id: str | None = None) -> None:
    uid = user_id or current().user_id
    if not uid:
        raise PermissionError("Sign in to save preferences")
    with tx() as c:
        c.execute("INSERT INTO user_prefs(user_id, key, value, updated_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (uid, key, value, now()))


def get_setting(key: str, default: str | None = None, org_id: str | None = None) -> str | None:
    """Organisation setting (retention period, forum requirements, …)."""
    org = org_id or current().org_id or DEFAULT_ORG
    row = conn().execute("SELECT value FROM org_settings WHERE org_id=? AND key=?", (org, key)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, org_id: str | None = None) -> None:
    ident = current()
    org = org_id or ident.org_id or DEFAULT_ORG
    with tx() as c:
        c.execute("INSERT INTO org_settings(org_id, key, value, updated_at, updated_by) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(org_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
                  "updated_by=excluded.updated_by", (org, key, value, now(), ident.user_id or ident.actor))


def reset_connections() -> None:
    """Drop this thread's cached SQLite connection (used by tests that swap DB files)."""
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
