"""Move NyaySutra from one Supabase project to another (e.g. Seoul -> Mumbai), making the target an exact copy.

    python scripts/move_supabase_project.py --check     # connections + differences only, writes nothing
    python scripts/move_supabase_project.py             # copy, reconcile, verify
    python scripts/move_supabase_project.py --switch    # ... and, if verification passes, point .env at the target

.env needs BOTH projects:
    SUPABASE_DB_URL,        SUPABASE_URL,        SUPABASE_SECRET_KEY          (current project — the app keeps using it)
    TARGET_SUPABASE_DB_URL, TARGET_SUPABASE_URL, TARGET_SUPABASE_SECRET_KEY   (the new project)
With --switch, after a successful verification .env is rewritten so the app uses the new project (the old values
are kept as OLD_SUPABASE_* for rollback). Target schema: supabase/migrations applied. Stop the app first so the
source does not change during the move.

Pass 1 (bulk): the local SQLite copy -> target via scripts/migrate_to_supabase.py (fast, local, verified identical
to the source at switch-over). Pass 2 (reconcile; the source project is the truth): every table is upserted from
the source, rows that no longer exist in the source are removed, chunks missing in the target are fetched by id,
Storage objects are compared and copied, identity sequences advanced; then both projects are compared table by
table and object by object. Idempotent; the source project is only read, never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from psycopg.types.string import TextLoader  # noqa: E402

import migrate_to_supabase as bulk  # noqa: E402
from nyaya import config, security, storage  # noqa: E402

TABLES, PK, IDENTITY = bulk.TABLES, bulk.PK, bulk.IDENTITY
APPEND_ONLY = {"audit_log"}          # the app role may insert but never update/delete history
BATCH = 50                            # chunks per source query (embeddings are large; the source link can be slow)


def _reconnect_source():
    return connect(SRC["db"])

SRC = {"db": config.SUPABASE_DB_URL, "url": config.SUPABASE_URL, "key": config.SUPABASE_SECRET_KEY}
TGT = {"db": "", "url": "", "key": ""}


def log(msg: str) -> None:
    print(msg, flush=True)


def _load_env_targets() -> None:
    from dotenv import dotenv_values
    env = dotenv_values(ROOT / ".env")
    for k, name in (("db", "TARGET_SUPABASE_DB_URL"), ("url", "TARGET_SUPABASE_URL"), ("key", "TARGET_SUPABASE_SECRET_KEY")):
        v = (os.getenv(name) or env.get(name) or "").strip().strip('"').strip("'")
        TGT[k] = v.rstrip("/") if k == "url" else v


def connect(url: str):
    c = psycopg.connect(url, autocommit=False, prepare_threshold=None, connect_timeout=20)
    c.adapters.register_loader("jsonb", TextLoader)   # JSON as text, re-cast with ::jsonb on insert
    c.adapters.register_loader("json", TextLoader)
    return c


@contextmanager
def project(url: str, key: str):
    """Point the storage client at a given project for the duration of the block."""
    old = config.SUPABASE_URL, config.SUPABASE_SECRET_KEY
    config.SUPABASE_URL, config.SUPABASE_SECRET_KEY = url, key
    try:
        yield
    finally:
        config.SUPABASE_URL, config.SUPABASE_SECRET_KEY = old


def list_objects(url: str, key: str, bucket: str) -> dict[str, int]:
    """All object keys in a bucket (recursing into folders) -> size in bytes."""
    out: dict[str, int] = {}
    with project(url, key):
        def walk(prefix: str) -> None:
            offset = 0
            while True:
                r = storage._request("POST", f"/object/list/{bucket}", headers=storage._headers(),
                                     json={"prefix": prefix, "limit": 1000, "offset": offset,
                                           "sortBy": {"column": "name", "order": "asc"}})
                if r.status_code >= 400:
                    raise SystemExit(f"Could not list bucket {bucket} at {url}: HTTP {r.status_code}")
                items = r.json()
                for it in items:
                    full = f"{prefix}{it['name']}"
                    if it.get("id") is None:          # a folder
                        walk(full + "/")
                    else:
                        out[full] = int((it.get("metadata") or {}).get("size") or 0)
                if len(items) < 1000:
                    break
                offset += 1000
        walk("")
    return out


def pk_list(conn, table: str) -> set[tuple]:
    pk = PK.get(table, ["id"])
    return {tuple(r) for r in conn.execute(f"SELECT {', '.join(pk)} FROM public.{table}").fetchall()}


def reconcile_table(src, dst, table: str, dry: bool, report: dict) -> None:
    pk = PK.get(table, ["id"])
    dst_types = bulk.pg_columns(dst, table)
    cols = list(dst_types)
    s_keys, d_keys = pk_list(src, table), pk_list(dst, table)
    src.commit()
    extra = d_keys - s_keys
    need = sorted(s_keys - d_keys) if table == "chunks" else sorted(s_keys)
    report[table] = {"source_rows": len(s_keys), "target_before": len(d_keys), "upserted": len(need),
                     "removed": len(extra) if table not in APPEND_ONLY else 0}
    if dry:
        return   # counting only: nothing is fetched or written
    if table == "chunks":
        # Chunks are immutable (re-indexing inserts new ids): only fetch the ones the target lacks, in small
        # batches with a reconnect on a dropped connection (the link to the source region can be slow).
        rows = []
        for i in range(0, len(need), BATCH):
            part = [k[0] for k in need[i:i + BATCH]]
            for attempt in range(3):
                try:
                    rows += src.execute(f"SELECT {', '.join(cols)} FROM public.chunks WHERE id = ANY(%s)", (part,)).fetchall()
                    src.commit()
                    break
                except psycopg.OperationalError:
                    if attempt == 2:
                        raise
                    src = _reconnect_source()
    else:
        rows = src.execute(f"SELECT {', '.join(cols)} FROM public.{table} ORDER BY {', '.join(pk)}").fetchall()
        src.commit()
    placeholders = ", ".join("%s::jsonb" if dst_types[c] == "jsonb" else "%s" for c in cols)
    non_pk = [c for c in cols if c not in pk]
    if table in APPEND_ONLY or table == "chunks" or not non_pk:
        conflict = "DO NOTHING"
    else:
        conflict = "DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in non_pk)
    sql = f"INSERT INTO public.{table} ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT ({', '.join(pk)}) {conflict}"
    with dst.cursor() as cur:
        for i in range(0, len(rows), 500):
            cur.executemany(sql, [tuple(r) for r in rows[i:i + 500]])
        if extra and table not in APPEND_ONLY:
            cond = " AND ".join(f"{c} = %s" for c in pk)
            cur.executemany(f"DELETE FROM public.{table} WHERE {cond}", sorted(extra))
    if table in IDENTITY:
        dst.execute(f"SELECT setval(pg_get_serial_sequence('public.{table}', 'id'), "
                    f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM public.{table}), 1))")
    dst.commit()


def storage_mime(ext: str) -> str:
    return security.MIME.get(ext.lower(), "application/octet-stream").split(";")[0]


def copy_objects(src_objs: dict, dst_objs: dict, bucket: str, dry: bool, report: dict) -> None:
    missing = sorted(set(src_objs) - set(dst_objs))
    extra = sorted(set(dst_objs) - set(src_objs))
    report["storage"] = {"source_objects": len(src_objs), "target_before": len(dst_objs), "to_copy": len(missing),
                         "to_remove": len(extra)}
    if dry:
        return
    with tempfile.TemporaryDirectory(prefix="nyaya_move_") as tmp:
        for key in missing:
            local = Path(tmp) / Path(key).name
            with project(SRC["url"], SRC["key"]):
                storage.download_to(f"{storage.PREFIX}{bucket}/{key}", local)
            case_id, _, name = key.partition("/")
            with project(TGT["url"], TGT["key"]):
                storage.put_file(local, case_id, Path(name).stem, Path(name).suffix, storage_mime(Path(name).suffix))
    with project(TGT["url"], TGT["key"]):
        for key in extra:
            storage.delete(f"{storage.PREFIX}{bucket}/{key}")


def _checksum(conn, table: str, cols: list[str]) -> str | None:
    order = ", ".join(PK.get(table, ["id"]))
    v = conn.execute(f"SELECT md5(string_agg(t::text, '|' ORDER BY {order})) FROM "
                     f"(SELECT {', '.join(cols)} FROM public.{table}) t").fetchone()[0]
    conn.commit()
    return v


def verify(src, dst, bucket: str) -> bool:
    ok = True
    log("\nVerification (source project vs target project)")
    for table in TABLES:
        s, d = pk_list(src, table), pk_list(dst, table)
        same = s == d
        ok &= same
        log(f"  {table:16} source {len(s):6}  target {len(d):6}  {'OK' if same else 'DIFFERENT'}")
    for table in ("cases", "documents", "drafts", "messages", "notifications", "integrations", "source_records"):
        cols = list(bulk.pg_columns(dst, table))
        same = _checksum(src, table, cols) == _checksum(dst, table, cols)
        ok &= same
        log(f"  {table:16} full-content checksum {'identical' if same else 'DIFFERENT'}")
    sample = [r[0] for r in src.execute("SELECT id FROM public.chunks ORDER BY md5(id::text) LIMIT 100").fetchall()]
    q = "SELECT md5(string_agg(text || encode(emb, 'hex'), '|' ORDER BY id)) FROM public.chunks WHERE id = ANY(%s)"
    same = src.execute(q, (sample,)).fetchone()[0] == dst.execute(q, (sample,)).fetchone()[0]
    ok &= same
    log(f"  chunks sample: 100 chunks (text + embedding) {'identical' if same else 'DIFFERENT'}")
    s_objs = list_objects(SRC["url"], SRC["key"], bucket)
    d_objs = list_objects(TGT["url"], TGT["key"], bucket)
    same = s_objs == d_objs
    ok &= same
    log(f"  storage objects: source {len(s_objs)}  target {len(d_objs)}  {'OK (names and sizes)' if same else 'DIFFERENT'}")
    hashes_ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for key in d_objs:
            p1, p2 = Path(tmp) / "a", Path(tmp) / "b"
            with project(SRC["url"], SRC["key"]):
                storage.download_to(f"{storage.PREFIX}{bucket}/{key}", p1)
            with project(TGT["url"], TGT["key"]):
                storage.download_to(f"{storage.PREFIX}{bucket}/{key}", p2)
            hashes_ok &= hashlib.sha256(p1.read_bytes()).digest() == hashlib.sha256(p2.read_bytes()).digest()
    ok &= hashes_ok
    log(f"  storage contents: {'identical (SHA-256)' if hashes_ok else 'DIFFERENT'}")
    return ok


def switch_env() -> None:
    """Point the app at the target project. Old values are kept as OLD_SUPABASE_* (rollback); TARGET_* renamed."""
    path = ROOT / ".env"
    text = path.read_text("utf-8")
    for name in ("SUPABASE_DB_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY"):
        text = re.sub(rf"^{name}\s*=", f"OLD_{name}=", text, count=1, flags=re.M)
        text = re.sub(rf"^TARGET_{name}\s*=", f"{name}=", text, count=1, flags=re.M)
    path.write_text(text, "utf-8")
    log("\n.env switched: the app uses the new project from its next start (old settings kept as OLD_SUPABASE_*).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="show differences only; write nothing")
    ap.add_argument("--skip-bulk", action="store_true", help="skip pass 1 (local copy -> target)")
    ap.add_argument("--switch", action="store_true", help="after a successful verification, point .env at the target")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _load_env_targets()
    missing = [n for n, v in (("SUPABASE_DB_URL", SRC["db"]), ("SUPABASE_URL", SRC["url"]),
                              ("SUPABASE_SECRET_KEY", SRC["key"]), ("TARGET_SUPABASE_DB_URL", TGT["db"]),
                              ("TARGET_SUPABASE_URL", TGT["url"]), ("TARGET_SUPABASE_SECRET_KEY", TGT["key"])) if not v]
    if missing:
        sys.exit("Missing in .env: " + ", ".join(missing))
    if SRC["url"] == TGT["url"]:
        sys.exit("Source and target are the same project.")
    bucket = config.SUPABASE_BUCKET
    src, dst = connect(SRC["db"]), connect(TGT["db"])
    for name, c in (("source", src), ("target", dst)):
        v = c.execute("SELECT value FROM public.meta WHERE key='schema_version'").fetchone()
        who = c.execute("SELECT current_user").fetchone()[0]
        c.commit()
        log(f"{name}: connected as {who}, schema_version {v[0] if v else 'MISSING'}")
        if not v:
            sys.exit(f"{name} project has no schema: apply supabase/migrations first.")
    with project(TGT["url"], TGT["key"]):
        h = storage.health()
    log(f"target storage: bucket {h.get('bucket')} reachable={h.get('ok')} public={h.get('public')}")
    if not h.get("ok"):
        sys.exit("Target storage is not reachable with TARGET_SUPABASE_SECRET_KEY.")
    if not args.check and not args.skip_bulk:
        log("\nPass 1 — bulk copy from the local verified copy")
        sq = bulk.sqlite_connect()
        report = {"tables": {}, "columns_not_copied": {}, "files_uploaded": 0, "files_to_upload": 0,
                  "files_already_migrated": 0, "files_missing": [], "files_hash_mismatch": []}
        refs: dict[str, str] = {}
        with project(TGT["url"], TGT["key"]):
            for table in TABLES:
                bulk.copy_table(sq, dst, table, report, False, refs)
        bulk.seed_vector_cache(sq, dst, report)
    log(f"\nPass 2 — reconcile with the source project ({'check only' if args.check else 'writing'})")
    rec: dict = {}
    for table in TABLES:
        reconcile_table(src, dst, table, args.check, rec)
        r = rec[table]
        log(f"  {table:16} source {r['source_rows']:6}  target had {r['target_before']:6}  "
            f"upsert {r['upserted']:6}  remove {r['removed']:4}")
    copy_objects(list_objects(SRC["url"], SRC["key"], bucket), list_objects(TGT["url"], TGT["key"], bucket),
                 bucket, args.check, rec)
    log(f"  storage          source {rec['storage']['source_objects']}  target had {rec['storage']['target_before']}  "
        f"copy {rec['storage']['to_copy']}  remove {rec['storage']['to_remove']}")
    if args.check:
        log("\nCheck only — nothing was written.")
        return 0
    ok = verify(src, dst, bucket)
    log(f"\nResult: {'OK — the target is an exact copy of the source' if ok else 'ATTENTION — see DIFFERENT lines'}")
    if ok and args.switch:
        switch_env()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
