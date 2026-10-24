"""Copy NyaySutra data from the local SQLite file (and data/uploads) into Supabase Postgres + Storage.

    python scripts/migrate_to_supabase.py --dry-run     # read-only: what would be copied
    python scripts/migrate_to_supabase.py               # copy, then verify
    python scripts/migrate_to_supabase.py --verify-only # compare SQLite and Supabase without writing

Guarantees
* Read-only on the source: the SQLite file and data/uploads are never modified or deleted (rollback = unset
  SUPABASE_DB_URL / SUPABASE_URL in .env and restart).
* Idempotent: rows are inserted with ON CONFLICT DO NOTHING on their primary key, files are uploaded to fixed
  keys; re-running copies only what is missing and never duplicates.
* IDs, relationships and timestamps are preserved exactly; identity sequences are advanced past copied ids.
* A client file is uploaded (and its hash checked) BEFORE its document row is written, so no row ever points at a
  missing object. Missing local files are reported, never invented.
Requires in .env: SUPABASE_DB_URL, SUPABASE_URL, SUPABASE_SECRET_KEY (schema from supabase/migrations applied).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nyaya import config, security, storage  # noqa: E402

# Parent tables before children (foreign keys).
TABLES = ["meta", "cases", "case_notes", "documents", "chunks", "chats", "messages", "prefs", "audit_log",
          "notifications", "integrations", "sync_log", "source_records", "doc_derivatives", "drafts", "draft_versions",
          "case_provisions"]
PK = {"meta": ["key"], "prefs": ["key"], "doc_derivatives": ["doc_id", "kind"], "draft_versions": ["draft_id", "version"]}
IDENTITY = ["chunks", "messages", "audit_log", "notifications", "sync_log"]
BATCH = 500


def log(msg: str) -> None:
    print(msg, flush=True)


def pg_connect():
    import psycopg

    if not config.SUPABASE_DB_URL:
        sys.exit("SUPABASE_DB_URL is not set in .env")
    return psycopg.connect(config.SUPABASE_DB_URL, autocommit=False, prepare_threshold=None, connect_timeout=20)


def sqlite_connect() -> sqlite3.Connection:
    src = config.DB_PATH
    if not src.exists():
        sys.exit(f"SQLite database not found: {src}")
    c = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)   # read-only: the source is never modified
    c.row_factory = sqlite3.Row
    return c


def pg_columns(pg, table: str) -> dict[str, str]:
    rows = pg.execute("SELECT column_name, data_type, is_generated FROM information_schema.columns "
                      "WHERE table_schema='public' AND table_name=%s", (table,)).fetchall()
    return {r[0]: r[1] for r in rows if r[2] != "ALWAYS"}   # generated columns (chunks.fts) are computed by Postgres


def migrate_file(pg, row: dict, report: dict, dry: bool) -> str | None:
    """Upload one client file; returns the new storage reference, or None if it cannot be migrated."""
    ref = row["path"]
    if storage.is_remote(ref):
        return ref
    done = pg.execute("SELECT path FROM public.documents WHERE id=%s", (row["id"],)).fetchone()
    if done and storage.is_remote(done[0]):
        report["files_already_migrated"] += 1   # re-run: the file and its row are already in Supabase
        return done[0]
    is_derived = '"derived_from"' in (row["meta"] or "")
    if is_derived:
        return None   # resolved later: derived docs point at their source document's reference
    local = config.from_stored(ref)
    if not local.is_file():
        report["files_missing"].append({"doc_id": row["id"], "path": ref})
        return None
    data_hash = hashlib.sha256(local.read_bytes()).hexdigest()
    if data_hash != row["sha256"]:
        report["files_hash_mismatch"].append({"doc_id": row["id"], "path": ref})
        return None
    ext = local.suffix.lower()
    if dry:
        report["files_to_upload"] += 1
        return storage.PREFIX + f"{config.SUPABASE_BUCKET}/{storage.object_key(row['case_id'] or 'none', row['id'], ext)}"
    new_ref = storage.put_file(local, row["case_id"] or "none", row["id"], ext, security.MIME.get(ext, "application/octet-stream"))
    # Read it back and compare hashes before any row points at it.
    with tempfile.TemporaryDirectory() as tmp:
        back = Path(tmp) / f"check{ext}"
        storage.download_to(new_ref, back)
        if hashlib.sha256(back.read_bytes()).hexdigest() != row["sha256"]:
            raise RuntimeError(f"Uploaded file for {row['id']} does not match its hash")
    report["files_uploaded"] += 1
    return new_ref


def copy_table(sq, pg, table: str, report: dict, dry: bool, refs: dict[str, str]) -> None:
    src_cols = [r[1] for r in sq.execute(f"PRAGMA table_info({table})")]
    dst = pg_columns(pg, table)
    cols = [c for c in src_cols if c in dst]
    dropped = sorted(set(src_cols) - set(cols))
    if dropped:
        report["columns_not_copied"][table] = dropped
    pk = PK.get(table, ["id"])
    placeholders = ", ".join("%s::jsonb" if dst[c] == "jsonb" else "%s" for c in cols)
    sql = (f"INSERT INTO public.{table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT ({', '.join(pk)}) DO NOTHING")
    total = sq.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    inserted, offset = 0, 0
    order = ", ".join(pk)
    if table == "documents":
        # Files first (all of them), so every row written below already has its object in Storage.
        for r in sq.execute("SELECT * FROM documents WHERE collection='case' ORDER BY id"):
            new = migrate_file(pg, dict(r), report, dry)
            if new:
                refs[r["id"]] = new
    while offset < total:
        rows = [dict(r) for r in sq.execute(f"SELECT * FROM {table} ORDER BY {order} LIMIT ? OFFSET ?", (BATCH, offset))]
        offset += len(rows)
        if table == "documents":
            for r in rows:   # derived documents (translations) reuse their source document's reference
                src = json.loads(r["meta"] or "{}").get("derived_from")
                if r["id"] in refs:
                    r["path"] = refs[r["id"]]
                elif src and src in refs:
                    r["path"] = refs[src]
        if dry:
            continue
        with pg.cursor() as cur:
            before = cur.execute(f"SELECT COUNT(*) FROM public.{table}").fetchone()[0]
            cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
            after = cur.execute(f"SELECT COUNT(*) FROM public.{table}").fetchone()[0]
        pg.commit()
        inserted += after - before
    report["tables"][table] = {"source_rows": total, "inserted": inserted if not dry else None}
    if not dry and table in IDENTITY:
        pg.execute(f"SELECT setval(pg_get_serial_sequence('public.{table}', 'id'), "
                   f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM public.{table}), 1))")
        pg.commit()
    log(f"  {table:16} source {total:6}  {'(dry run)' if dry else f'inserted {inserted}'}")


def verify(sq, pg, report: dict) -> bool:
    ok = True
    log("\nVerification")
    for table in TABLES:
        s = sq.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        p = pg.execute(f"SELECT COUNT(*) FROM public.{table}").fetchone()[0]
        status = "OK" if p >= s else "MISSING ROWS"
        ok &= p >= s
        report["verify"][table] = {"sqlite": s, "supabase": p, "status": status}
        log(f"  {table:16} sqlite {s:6}  supabase {p:6}  {status}")
    # Every source row present with identical content (sampled for chunks, full for small tables).
    ids = [r[0] for r in sq.execute("SELECT id FROM chunks")]
    sample = random.Random(7).sample(ids, min(200, len(ids)))
    bad = 0
    for cid in sample:
        a = sq.execute("SELECT text, emb, doc_id, ord FROM chunks WHERE id=?", (cid,)).fetchone()
        b = pg.execute("SELECT text, emb, doc_id, ord FROM public.chunks WHERE id=%s", (cid,)).fetchone()
        if not b or a[0] != b[0] or bytes(a[1] or b"") != bytes(b[1] or b"") or a[2] != b[2] or a[3] != b[3]:
            bad += 1
    report["verify"]["chunk_sample"] = {"checked": len(sample), "mismatched": bad}
    log(f"  chunks sample: {len(sample)} compared (text, embedding bytes, document, order), {bad} mismatched")
    ok &= bad == 0
    for table, key in (("cases", "id"), ("documents", "id"), ("drafts", "id"), ("messages", "id")):
        cols = [r[1] for r in sq.execute(f"PRAGMA table_info({table})") if r[1] not in ("path", "meta", "sources",
                                                                                          "facts", "missing", "inputs")]
        mism = 0
        for r in sq.execute(f"SELECT {', '.join(cols)} FROM {table}"):
            q = pg.execute(f"SELECT {', '.join(cols)} FROM public.{table} WHERE {key}=%s", (r[key],)).fetchone()
            if not q or tuple(q) != tuple(r):
                mism += 1
        report["verify"][f"{table}_fields"] = {"mismatched": mism}
        log(f"  {table:16} all rows field-by-field: {mism} mismatched")
        ok &= mism == 0
    orphans = pg.execute("SELECT COUNT(*) FROM public.chunks c LEFT JOIN public.documents d ON d.id=c.doc_id "
                         "WHERE d.id IS NULL").fetchone()[0]
    local_case_files = pg.execute("SELECT COUNT(*) FROM public.documents WHERE collection='case' "
                                  "AND path NOT LIKE 'sb://%%'").fetchone()[0]
    report["verify"]["orphan_chunks"] = orphans
    report["verify"]["case_documents_not_in_storage"] = local_case_files
    log(f"  orphan chunks: {orphans};  case documents still pointing at local files: {local_case_files}")
    ok &= orphans == 0
    return ok


def seed_vector_cache(sq, pg, report: dict) -> None:
    """Write the app's local embedding cache from the verified source, so the first start on Supabase does not have
    to download every embedding over the network. Only ids present in Supabase are written; embeddings for any id
    the cache lacks are still downloaded by the app."""
    import numpy as np

    from nyaya import embeddings
    pg_ids = {r[0] for r in pg.execute("SELECT id FROM public.chunks WHERE emb IS NOT NULL").fetchall()}
    rows = [(r[0], r[1]) for r in sq.execute("SELECT id, emb FROM chunks WHERE emb IS NOT NULL ORDER BY id") if r[0] in pg_ids]
    if not rows:
        return
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    safe = "".join(ch if ch.isalnum() else "_" for ch in embeddings.model_name())
    path = config.HOME_DIR / f"vector_cache_{safe}.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path.with_suffix(".tmp.npz"), ids=ids, mat=mat)
    path.with_suffix(".tmp.npz").replace(path)
    report["vector_cache"] = {"path": str(path), "embeddings": len(rows)}
    log(f"Local embedding cache written: {len(rows)} embeddings -> {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not args.verify_only and not storage.remote_enabled():
        sys.exit("SUPABASE_URL and SUPABASE_SECRET_KEY must be set in .env to migrate uploaded files.")
    sq, pg = sqlite_connect(), pg_connect()
    v = pg.execute("SELECT value FROM public.meta WHERE key='schema_version'").fetchone()
    if not v:
        sys.exit("Supabase schema not found: apply supabase/migrations first.")
    report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "dry_run": args.dry_run, "tables": {}, "verify": {},
              "columns_not_copied": {}, "files_uploaded": 0, "files_to_upload": 0, "files_already_migrated": 0,
              "files_missing": [],
              "files_hash_mismatch": []}
    t0 = time.time()
    if not args.verify_only:
        log(f"Copying {config.DB_PATH} -> Supabase ({'dry run' if args.dry_run else 'writing'})")
        refs: dict[str, str] = {}
        for table in TABLES:
            copy_table(sq, pg, table, report, args.dry_run, refs)
    ok = True if args.dry_run else verify(sq, pg, report)
    if ok and not args.dry_run:
        seed_vector_cache(sq, pg, report)
    ok &= not report["files_missing"] and not report["files_hash_mismatch"]
    report["seconds"] = round(time.time() - t0, 1)
    report["result"] = "ok" if ok else "attention"
    out = config.DATA_DIR / "migration_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
    log(f"\nFiles uploaded: {report['files_uploaded']}  missing locally: {len(report['files_missing'])}  "
        f"hash mismatches: {len(report['files_hash_mismatch'])}")
    log(f"Result: {report['result'].upper()} in {report['seconds']} s — report written to {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
