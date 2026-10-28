"""Build the Supreme Court judgments catalogue from the open dataset (Dattam Labs, CC-BY-4.0).

Year by year: download that year's metadata archive, parse every judgment (parties, citations, bench, date, outcome,
headnote), embed and add it to the catalogue. Resumable — judgments already in the catalogue are skipped — and it
stops on its own if the database grows past --max-db-mb (the Supabase free tier ends at 500 MB).

Restart the app afterwards so its in-memory search index picks up the new entries.

    python scripts/import_sc_catalogue.py                  # 1950–2025
    python scripts/import_sc_catalogue.py --from 2020      # only recent years
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nyaya import config, db, judgments  # noqa: E402


def db_mb() -> float | None:
    if config.auth_mode() != "supabase" or not config.SUPABASE_DB_URL:
        return None
    with db.system():
        row = db.conn().execute("SELECT pg_database_size(current_database()) AS b").fetchone()
    return row["b"] / 1048576


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=int, default=judgments.FIRST_YEAR)
    ap.add_argument("--to", dest="end", type=int, default=judgments.LAST_YEAR)
    ap.add_argument("--max-db-mb", type=float, default=400.0)
    a = ap.parse_args()
    total = 0
    with db.system(), judgments.client() as c:
        for year in range(a.start, a.end + 1):
            size = db_mb()
            if size is not None and size > a.max_db_mb:
                print(f"Stopping: database is {size:.0f} MB (limit set to {a.max_db_mb:.0f} MB).", flush=True)
                break
            t0 = time.time()
            try:
                items = judgments.read_year(c, year)
            except Exception as exc:   # one bad year never stops the rest
                print(f"{year}: could not read ({type(exc).__name__}: {exc})", flush=True)
                continue
            added = judgments.add_to_catalogue(items)
            total += added
            print(f"{year}: {len(items)} judgments, {added} added ({time.time() - t0:.0f}s)"
                  + (f" · database {size:.0f} MB" if size is not None else ""), flush=True)
    print(f"Done: {total} judgments added. Catalogue now holds "
          f"{db.conn().execute('SELECT n_chunks FROM documents WHERE id=?', (judgments.catalogue_id(),)).fetchone()['n_chunks']}.")


if __name__ == "__main__":
    main()
