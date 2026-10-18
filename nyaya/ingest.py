"""Ingestion pipeline: file → text → structure-aware chunks → local embeddings →
SQLite (rows + FTS5) → in-memory vector index.

    python -m nyaya.ingest library            # build / update the shared law library
    python -m nyaya.ingest library --rebuild  # drop and rebuild the library
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

from . import chunking, config, db, embeddings, parsing
from .vectorindex import index

log = logging.getLogger(__name__)

# Embedding input window: section header + opening text. Full text stays in FTS5.
EMBED_CHARS = 1200

DOC_TYPES = {
    "statute": "Statute / Bare Act",
    "judgment": "Judgment / Order",
    "pleading": "Pleading / Petition",
    "contract": "Contract / Agreement / Deed",
    "evidence": "Evidence / Exhibit",
    "notice": "Notice / Correspondence",
    "template": "Draft Template",
    "other": "Other",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pretty_title(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[_]+", " ", stem).strip()
    m = re.match(r"(.+?)\s+on\s+(\d{1,2}\s+\w+\s+\d{4})$", stem)  # Indian Kanoon style names
    if m:
        return f"{m.group(1)} ({m.group(2)})"
    return stem[:150] or "Untitled"


def classify(filename: str, sample: str) -> str:
    """Best-effort document type from file name and first page."""
    name = filename.lower()
    head = sample[:4000].lower()
    if re.search(r"\b(act|code|sanhita|adhiniyam|rules|regulations|ordinance)\b.*\d{4}", name) or \
            ("arrangement of sections" in head and "be it enacted" in head):
        return "statute"
    if re.search(r"(_vs?_|\bvs?\.?\b|\bversus\b)", name) or re.search(
            r"\b(judgment|judgement|coram|hon'?ble|present:|reportable|the court|appellant|respondent)\b", head):
        if re.search(r"\b(plaint|petition|written statement|application under|prayer)\b", head) and \
                "judgment" not in head:
            return "pleading"
        return "judgment"
    if re.search(r"\b(agreement|deed|lease|contract|mou|memorandum of understanding|nda)\b", name + " " + head[:800]):
        return "contract"
    if re.search(r"\b(plaint|petition|written statement|affidavit|application|prayer)\b", name + " " + head[:800]):
        return "pleading"
    if re.search(r"\b(legal notice|notice|letter|email|correspondence)\b", name + " " + head[:400]):
        return "notice"
    if re.search(r"\b(fir|panchnama|statement of|exhibit|medical report|invoice|receipt|bank statement)\b",
                 name + " " + head[:800]):
        return "evidence"
    return "other"


def _insert_document(*, doc_id: str, case_id: str | None, collection: str, doc_type: str, title: str,
                     filename: str, path: Path, sha: str, meta: dict) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO documents(id, case_id, collection, doc_type, title, filename, path, sha256, status, meta, created_at)"
            " VALUES(?,?,?,?,?,?,?,?, 'processing', ?, ?)",
            (doc_id, case_id, collection, doc_type, title, filename, str(path), sha, json.dumps(meta), db.now()),
        )


def find_duplicate(case_id: str | None, sha: str) -> dict | None:
    row = db.conn().execute(
        "SELECT * FROM documents WHERE IFNULL(case_id,'') = ? AND sha256 = ?", (case_id or "", sha)
    ).fetchone()
    return db.row_to_dict(row)


def register(path: Path, *, collection: str, case_id: str | None, doc_type: str | None, title: str | None,
             filename: str, meta: dict | None = None, doc_id: str | None = None) -> tuple[dict, bool]:
    """Create the document row (status=processing). Returns (doc, created)."""
    sha = sha256_file(path)
    dup = find_duplicate(case_id, sha)
    if dup:
        return dup, False
    doc_id = doc_id or db.new_id("doc_")
    _insert_document(doc_id=doc_id, case_id=case_id, collection=collection, doc_type=doc_type or "other",
                     title=title or pretty_title(filename), filename=filename, path=path, sha=sha, meta=meta or {})
    return db.row_to_dict(db.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()), True


def process(doc_id: str) -> dict:
    """Parse, chunk, embed and index a registered document. Never raises."""
    row = db.row_to_dict(db.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    if not row:
        return {}
    t0 = time.time()
    try:
        parsed = parsing.extract(Path(row["path"]))
        doc_type = row["doc_type"]
        if doc_type in ("other", "auto", ""):
            doc_type = classify(row["filename"], parsed.pages[0] if parsed.pages else "")
        chunks = chunking.chunk_document(parsed.pages, doc_type, row["title"], row["meta"].get("short", ""))
        if not chunks:
            raise parsing.ParseError("No text could be split into passages.")
        vecs = embeddings.embed_passages([f"{c.header}\n{c.text}"[:EMBED_CHARS] for c in chunks])
        rows = [
            (doc_id, i, c.header, c.text, c.page_start, c.page_end, c.label, c.section, c.title, c.chapter,
             vecs[i].tobytes())
            for i, c in enumerate(chunks)
        ]
        with db.tx() as c:
            c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            c.executemany(
                "INSERT INTO chunks(doc_id, ord, header, text, page_start, page_end, label, section, title, chapter, emb)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE doc_id=? ORDER BY ord", (doc_id,))]
            c.execute("UPDATE documents SET status='ready', error=NULL, doc_type=?, pages=?, chars=?, n_chunks=? "
                      "WHERE id=?", (doc_type, len(parsed.pages), parsed.chars, len(chunks), doc_id))
        index.remove_doc(doc_id)
        index.add(ids, doc_id, row["case_id"], row["collection"], vecs)
        log.info("Indexed %s: %d pages, %d chunks in %.1fs", row["filename"], len(parsed.pages), len(chunks),
                 time.time() - t0)
    except parsing.ParseError as exc:
        _fail(doc_id, str(exc))
    except Exception as exc:  # unexpected: log details locally, show a generic message
        log.exception("Ingestion failed for %s", row["filename"])
        _fail(doc_id, f"Could not process this file ({type(exc).__name__}).")
    return db.row_to_dict(db.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def _fail(doc_id: str, msg: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE documents SET status='failed', error=? WHERE id=?", (msg, doc_id))


def delete_document(doc_id: str) -> bool:
    row = db.conn().execute("SELECT path, collection FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return False
    with db.tx() as c:
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))  # chunks + FTS rows cascade via triggers
    index.remove_doc(doc_id)
    p = Path(row["path"]).resolve()
    if row["collection"] == "case" and config.UPLOAD_DIR.resolve() in p.parents and p.exists():
        p.unlink()
    return True


def document_text(doc_id: str) -> tuple[list[tuple[int, int, str]], dict | None]:
    """All chunks of a document in order: [(page_start, page_end, text)]."""
    doc = db.row_to_dict(db.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    rows = db.conn().execute("SELECT page_start, page_end, text FROM chunks WHERE doc_id=? ORDER BY ord",
                             (doc_id,)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows], doc


# ── Library build ────────────────────────────────────────────────────────────
def _library_files() -> list[tuple[Path, str, str, str, dict]]:
    """(path, collection, doc_type, title, meta) for every library file on disk."""
    items: list[tuple[Path, str, str, str, dict]] = []
    manifest_path = config.BARE_ACTS_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
    for p in sorted(config.BARE_ACTS_DIR.glob("*.pdf")):
        m = manifest.get(p.name, {})
        title = m.get("title") or pretty_title(p.name)
        items.append((p, "bare_act", "statute", title, {k: v for k, v in m.items() if k != "sha256"}))
    for p in sorted(config.DRAFTS_DIR.rglob("*.md")):
        area = p.parent.name if p.parent != config.DRAFTS_DIR else "General"
        first = p.read_text("utf-8").split("\n", 1)[0].lstrip("# ").strip()
        items.append((p, "draft", "template", first or pretty_title(p.name), {"area": area}))
    jm_path = config.JUDGMENTS_DIR / "manifest.json"
    jm = json.loads(jm_path.read_text("utf-8")) if jm_path.exists() else {}
    for p in sorted(q for q in config.JUDGMENTS_DIR.iterdir() if q.suffix.lower() == ".pdf"):
        m = jm.get(p.name, {})
        items.append((p, "judgment", "judgment", m.get("title") or pretty_title(p.name), m))
    return items


def build_library(rebuild: bool = False, progress=print) -> dict:
    db.init()
    if rebuild:
        with db.tx() as c:
            c.execute("DELETE FROM documents WHERE collection IN ('bare_act','draft','judgment')")
    on_disk = _library_files()
    known_paths = {str(p) for p, *_ in on_disk}
    # Library documents whose file was removed from disk are dropped.
    for r in db.conn().execute("SELECT id, path FROM documents WHERE collection IN ('bare_act','draft','judgment')"):
        if r["path"] not in known_paths:
            delete_document(r["id"])
    added = skipped = failed = 0
    for i, (p, coll, dtype, title, meta) in enumerate(on_disk, 1):
        doc, created = register(p, collection=coll, case_id=None, doc_type=dtype, title=title,
                                filename=p.name, meta=meta)
        if not created and doc["status"] == "ready":
            skipped += 1
            continue
        if not created and doc["path"] != str(p):
            with db.tx() as c:
                c.execute("UPDATE documents SET path=? WHERE id=?", (str(p), doc["id"]))
        t = time.time()
        res = process(doc["id"])
        ok = res.get("status") == "ready"
        added += ok
        failed += not ok
        progress(f"[{i}/{len(on_disk)}] {'OK ' if ok else 'ERR'} {title[:70]} "
                 f"({res.get('n_chunks', 0)} chunks, {time.time() - t:.1f}s){'' if ok else ' — ' + str(res.get('error'))}")
    db.set_meta("embed_model", embeddings.model_name())
    return {"added": added, "skipped": skipped, "failed": failed, "total": len(on_disk)}


def reembed_all(progress=print) -> None:
    """Re-embed every chunk (needed after changing EMBED_MODEL)."""
    docs = db.conn().execute("SELECT id, title FROM documents WHERE status='ready'").fetchall()
    for i, d in enumerate(docs, 1):
        rows = db.conn().execute("SELECT id, header, text FROM chunks WHERE doc_id=? ORDER BY ord", (d["id"],)).fetchall()
        vecs = embeddings.embed_passages([f"{r['header']}\n{r['text']}"[:EMBED_CHARS] for r in rows])
        with db.tx() as c:
            c.executemany("UPDATE chunks SET emb=? WHERE id=?", [(vecs[j].tobytes(), r["id"]) for j, r in enumerate(rows)])
        progress(f"[{i}/{len(docs)}] re-embedded {d['title'][:70]}")
    db.set_meta("embed_model", embeddings.model_name())
    index.load()


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m nyaya.ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lib = sub.add_parser("library", help="index bare acts, draft templates and judgments")
    lib.add_argument("--rebuild", action="store_true")
    sub.add_parser("reembed", help="re-embed all chunks after changing EMBED_MODEL")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    db.init()
    t = time.time()
    if args.cmd == "library":
        stats = build_library(rebuild=args.rebuild, progress=lambda s: print(s, flush=True))
        print(f"\nLibrary: {stats} in {time.time() - t:.0f}s")
    else:
        reembed_all(progress=lambda s: print(s, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
