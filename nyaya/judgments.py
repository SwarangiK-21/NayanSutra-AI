"""Supreme Court of India judgments — searchable catalogue of every judgment, full text on demand.

Source: the "Indian Supreme Court Judgments" open dataset (Dattam Labs, CC-BY-4.0), built from the Supreme Court of
India's own records and published on the AWS Open Data registry. It holds ~43,600 English judgments (1950–2025).

Why two tiers: the full text of every judgment would need ~8 GB, far above the 500 MB Supabase free tier. So:

* Catalogue (always present): one passage per judgment — parties, neutral and SCR citations, CNR, bench and author,
  decision date, case number, outcome and the official headnote summary — stored as passages of a single library
  document (collection 'judgment', doc_type 'catalogue'). It gets the same keyword + meaning search, re-ranking,
  citation checks and source cards as everything else. ~220 MB.
* Full text (on demand): when a catalogue entry is used as a source, or a user asks for it, a background job
  downloads that judgment's PDF from the dataset (one fixed host, PDF signature and size checked) and indexes it
  as an ordinary judgment. At most SC_FULLTEXT_MAX are kept; the least recently used are removed.

Answers built on a catalogue entry are told it is a headnote summary, not the judgment itself.
"""
from __future__ import annotations

import html
import io
import json
import logging
import re
import tarfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config, db

log = logging.getLogger("nyaya.judgments")

HOST = "indian-supreme-court-judgments.s3.ap-south-1.amazonaws.com"
BASE = f"https://{HOST}/"
ATTRIBUTION = ("Indian Supreme Court Judgments open dataset (Dattam Labs, CC-BY-4.0), compiled from Supreme Court of "
               "India records — https://registry.opendata.aws/indian-supreme-court-judgments")
CATALOGUE_TITLE = "Supreme Court of India — judgments catalogue (1950–2025)"
CATALOGUE_TYPE = "catalogue"
SOURCE = "sc_dataset"
MAX_PDF_BYTES = 40 * 1024 * 1024
FIRST_YEAR, LAST_YEAR = 1950, 2025
_cat_id: dict = {}
_lock = threading.Lock()


class JudgmentError(Exception):
    """Safe to show to the user."""


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": config.HTTP_USER_AGENT}, timeout=httpx.Timeout(120, connect=20),
                        follow_redirects=False)


def _get(c: httpx.Client, url: str) -> httpx.Response:
    if httpx.URL(url).host != HOST:
        raise JudgmentError("Refusing to contact a host other than the judgments dataset.")
    r = c.get(url)
    if r.status_code == 404:
        raise JudgmentError("The dataset has no file for this judgment.")
    r.raise_for_status()
    return r


def pdf_url(path: str, year: int | str) -> str:
    return f"{BASE}data/pdf/year={int(year)}/english/{path}_EN.pdf"


# ── Parsing the dataset's metadata records ───────────────────────────────────
def _text(fragment: str) -> str:
    fragment = re.sub(r"<sup[^>]*>\s*\*\s*</sup>", "*", fragment)          # author marker
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(fragment)).strip()


def _field(raw: str, label: str) -> str:
    m = re.search(rf"{label}\s*:\s*</span>\s*<font[^>]*>(.*?)</font>", raw, re.S | re.I)
    return _text(m.group(1)) if m else ""


def parse_record(rec: dict, year: int) -> dict | None:
    """One judgment's catalogue fields from the dataset's metadata record; None when it is not a usable record."""
    raw = rec.get("raw_html") or ""
    path = str(rec.get("path") or "").strip()
    if not raw or not re.fullmatch(r"[\w.-]{3,80}", path):
        return None
    raw = re.sub(r"<select.*?</select>", " ", raw, flags=re.S)
    raw = re.sub(r"<div class='modal.*?</div></div></div></div>", " ", raw, flags=re.S)   # site disclaimer
    m = re.search(r"<strong>(.*?)</strong>\s*-?\s*<span class='escrText'>", raw, re.S) or \
        re.search(r"<font size='4'>\s*<strong>(.*?)</strong>", raw, re.S)
    if not m:
        return None
    title = _text(m.group(1))
    parties = re.split(r"\s+versus\s+", title, maxsplit=1, flags=re.I)
    scr = _text(re.search(r"<span class='escrText'>(.*?)</span>", raw, re.S).group(1)) \
        if "escrText" in raw else ""
    nc = rec.get("nc_display") or ""
    ncm = re.search(r"<span class='ncDisplay'>(.*?)</span>", raw, re.S)
    nc_disp = _text(ncm.group(1)) if ncm else nc
    cnr = (re.search(r"id='cnr' value=([A-Z0-9]+)", raw) or [None, ""])[1]
    coram = ""
    cm = re.search(r"Coram\s*:(.*?)</strong>", raw, re.S)
    if cm:
        coram = _text(cm.group(1))
    head = ""
    hm = re.search(r"Coram\s*:.*?</strong>\s*(?:<br>)?(.*?)<strong class='caseDetailsTD'", raw, re.S)
    if hm:
        head = re.sub(r"\s+", " ", _text(hm.group(1))).strip()
        head = re.sub(r"^HEADNOTES\s*", "", head)
    decided = _field(raw, "Decision Date")
    iso = ""
    dm = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", decided)
    if dm:
        iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
    return {"path": path, "year": int(year), "title": title[:400],
            "petitioner": parties[0].strip()[:200], "respondent": parties[1].strip()[:200] if len(parties) > 1 else "",
            "scr": scr[:80], "neutral": nc_disp[:40], "cnr": cnr[:40], "coram": coram[:300],
            "author": ", ".join(re.findall(r"([A-Z][A-Z. ]+?)\s*\*", coram))[:120],
            "decided": iso, "case_no": _field(raw, "Case No")[:200], "disposal": _field(raw, "Disposal Nature")[:120],
            "bench": _field(raw, "Bench")[:40], "headnote": head[:2500]}


def passage(j: dict) -> tuple[str, str, str]:
    """(header, text, label) of a judgment's catalogue passage."""
    cite = " | ".join(x for x in (j["neutral"], j["scr"]) if x)
    header = f"{j['title']} — Supreme Court of India" + (f" | {cite}" if cite else "") + \
             (f" | decided {j['decided']}" if j["decided"] else "")
    lines = [f"Case: {j['title']}", f"Court: Supreme Court of India",
             j["decided"] and f"Decision date: {j['decided']}", cite and f"Citation: {cite}",
             j["case_no"] and f"Case number: {j['case_no']}", j["cnr"] and f"CNR: {j['cnr']}",
             j["coram"] and f"Bench (Coram): {j['coram']} (* = author)", j["bench"] and f"Bench strength: {j['bench']}",
             j["disposal"] and f"Outcome: {j['disposal']}",
             j["headnote"] and f"Headnote summary (official headnote, abridged): {j['headnote']}"]
    return header[:500], "\n".join(x for x in lines if x), (j["neutral"] or j["scr"] or j["path"])[:80]


def read_year(c: httpx.Client, year: int) -> list[dict]:
    """All parsed records of one year (metadata tar archives; parts named part-*.tar for large years)."""
    listing = _get(c, f"{BASE}?list-type=2&prefix=metadata/tar/year={year}/").text
    keys = [k for k in re.findall(r"<Key>([^<]+\.tar)</Key>", listing)]
    out = []
    for key in keys:
        r = _get(c, BASE + key)
        with tarfile.open(fileobj=io.BytesIO(r.content)) as t:
            for m in t.getmembers():
                if not (m.isfile() and m.name.endswith(".json")):
                    continue
                try:
                    rec = json.load(t.extractfile(m))
                except (ValueError, UnicodeDecodeError):
                    continue
                j = parse_record(rec, year)
                if j:
                    out.append(j)
    return out


# ── Catalogue document ───────────────────────────────────────────────────────
def catalogue_id() -> str | None:
    with _lock:
        if "id" not in _cat_id:
            row = db.conn().execute("SELECT id FROM documents WHERE collection='judgment' AND doc_type=? "
                                    "ORDER BY created_at LIMIT 1", (CATALOGUE_TYPE,)).fetchone()
            if not row:
                return None
            _cat_id["id"] = row["id"]
        return _cat_id["id"]


def _ensure_catalogue() -> str:
    from . import ingest
    cid = catalogue_id()
    if cid:
        return cid
    cid = db.new_id("doc_")
    ingest._insert_document(doc_id=cid, case_id=None, collection="judgment", doc_type=CATALOGUE_TYPE,
                            title=CATALOGUE_TITLE, filename="sc-judgments-catalogue", path="dataset:" + SOURCE,
                            sha=f"{SOURCE}:catalogue", meta={"source": SOURCE, "attribution": ATTRIBUTION,
                                                              "jurisdiction": "Supreme Court of India"})
    with db.tx() as c:
        c.execute("UPDATE documents SET status='ready', n_chunks=0 WHERE id=?", (cid,))
    _cat_id.pop("id", None)
    return catalogue_id() or cid


def add_to_catalogue(items: list[dict], batch: int = 256) -> int:
    """Index judgments into the catalogue (skips ones already there). Returns how many were added."""
    from . import embeddings
    from .ingest import EMBED_CHARS
    from .vectorindex import index
    cid = _ensure_catalogue()
    have = {r["section"] for r in db.conn().execute("SELECT section FROM chunks WHERE doc_id=?", (cid,))}
    todo = list({j["path"]: j for j in items if j["path"] not in have}.values())   # a judgment can recur in a year
    added = 0
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        texts = [passage(j) for j in part]
        # The meaning vector covers what the judgment is about (name, citation, headnote), not the boilerplate lines.
        vecs = embeddings.embed_passages([f"{h}\n{j['headnote'][:600]}"[:EMBED_CHARS] for (h, _, _), j in zip(texts, part)])
        with db.tx() as c:
            start = c.execute("SELECT COALESCE(MAX(ord), -1) + 1 AS n FROM chunks WHERE doc_id=?", (cid,)).fetchone()["n"]
            c.executemany(
                "INSERT INTO chunks(doc_id, ord, header, text, page_start, page_end, label, section, title, chapter, emb)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(cid, start + k, h, t, None, None, lab, j["path"], j["title"][:300], str(j["year"]), vecs[k].tobytes())
                 for k, ((h, t, lab), j) in enumerate(zip(texts, part))])
            ids = [r["id"] for r in c.execute("SELECT id FROM chunks WHERE doc_id=? AND ord >= ? ORDER BY ord",
                                              (cid, start))]
            c.execute("UPDATE documents SET n_chunks=(SELECT COUNT(*) FROM chunks WHERE doc_id=?) WHERE id=?", (cid, cid))
        index.add(ids, cid, None, "judgment", vecs)
        added += len(part)
    return added


def entry(section: str) -> dict | None:
    """The catalogue entry (chunk row) of one judgment, by its dataset path."""
    cid = catalogue_id()
    if not cid:
        return None
    row = db.conn().execute("SELECT id, header, text, label, section, title, chapter FROM chunks "
                            "WHERE doc_id=? AND section=?", (cid, section)).fetchone()
    return dict(row) if row else None


# ── Full text on demand ──────────────────────────────────────────────────────
def full_text_doc(path: str) -> dict | None:
    row = db.conn().execute("SELECT id, status FROM documents WHERE collection='judgment' AND sha256 LIKE ? ",
                            (f"{SOURCE}:{path}:%",)).fetchone()
    return dict(row) if row else None


def fetch_full_text(path: str) -> dict:
    """Download one judgment's PDF from the dataset, register it as a library judgment and index it now.
    Runs inside a background job. Returns the document row."""
    from . import ingest
    e = entry(path)
    if not e:
        raise JudgmentError("This judgment is not in the catalogue.")
    existing = full_text_doc(path)
    if existing and existing["status"] in ("ready", "processing"):
        touch(existing["id"])
        return existing
    year = int(e["chapter"])
    dest = config.JUDGMENTS_DIR / "supreme_court" / f"{path}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client() as c:
        r = _get(c, pdf_url(path, year))
    data = r.content
    if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
        raise JudgmentError("The dataset did not return a usable PDF for this judgment.")
    dest.write_bytes(data)
    meta = {"source": SOURCE, "attribution": ATTRIBUTION, "dataset_path": path, "year": year,
            "catalogue_chunk": e["id"], "citation": e["label"], "pdf_url": pdf_url(path, year),
            "jurisdiction": "Supreme Court of India", "last_used": _now()}
    import hashlib
    sha = f"{SOURCE}:{path}:{hashlib.sha256(data).hexdigest()[:16]}"
    with db.system():
        doc, created = ingest.register(dest, collection="judgment", case_id=None, doc_type="judgment",
                                       title=e["title"][:300], filename=dest.name, meta=meta, sha=sha)
        res = ingest.process(doc["id"], raise_transient=True) if created or doc["status"] == "processing" else doc
    enforce_cap()
    return res or doc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def touch(doc_id: str) -> None:
    """Mark a full-text judgment as recently used (it is then the last to be evicted)."""
    row = db.conn().execute("SELECT meta FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return
    meta = json.loads(row["meta"] or "{}") if isinstance(row["meta"], str) else dict(row["meta"] or {})
    meta["last_used"] = _now()
    with db.tx() as c:
        c.execute("UPDATE documents SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), doc_id))


def enforce_cap() -> int:
    """Remove the least recently used full-text judgments beyond SC_FULLTEXT_MAX. Returns how many were removed."""
    from . import ingest
    rows = db.conn().execute("SELECT id, meta, created_at FROM documents WHERE collection='judgment' AND doc_type='judgment' "
                             "AND sha256 LIKE ?", (f"{SOURCE}:%",)).fetchall()
    if len(rows) <= config.SC_FULLTEXT_MAX:
        return 0

    def last_used(r):
        m = r["meta"]
        m = json.loads(m) if isinstance(m, str) else (m or {})
        return m.get("last_used") or r["created_at"] or ""
    victims = sorted(rows, key=last_used)[: len(rows) - config.SC_FULLTEXT_MAX]
    with db.system():
        for r in victims:
            ingest.delete_document(r["id"])
    log.info("Removed %d least recently used full-text judgments (cap %d)", len(victims), config.SC_FULLTEXT_MAX)
    return len(victims)


def queue_fetch(paths: list[str], owner: str | None = None, org_id: str | None = None) -> list[str]:
    """Queue background full-text fetches (deduplicated per judgment; system jobs, so one fetch serves every user).
    Returns the paths queued."""
    owner = None
    from . import jobs
    out = []
    for p in paths[:3]:
        if not re.fullmatch(r"[\w.-]{3,80}", p or "") or not entry(p):
            continue
        doc = full_text_doc(p)
        if doc and doc["status"] == "ready":
            touch(doc["id"])
            continue
        try:
            jobs.submit("judgment_fetch", {"path": p}, key=f"judgment:{p}", owner=owner, org_id=org_id or db.DEFAULT_ORG)
            out.append(p)
        except Exception:   # a duplicate key or a full queue never breaks the answer
            log.info("Full-text fetch not queued for %s", p)
    return out
