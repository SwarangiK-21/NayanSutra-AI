"""India Code (indiacode.gov.in) — the Legislative Department's official repository of Central and State Acts.

Used for all-India coverage:
  * catalogue(state, q)  live search of the official record for one state / Union Territory (or Central Acts);
                         nothing is cached as if it were law — results show India Code's own metadata and link.
  * import_act(uuid)     downloads that act's official English PDF (from indiacode.gov.in only, checked to really be
                         a PDF) into the library, records its provenance and jurisdiction, and queues it for indexing,
                         after which Legal Research can cite it.

Jurisdiction comes from India Code itself: act ids starting AC_CEN are Central Acts; every other act carries the
state's name (dc.identifier.state_name). Nothing here invents a statute: an act that India Code does not return
cannot be added, and repealed acts are labelled as India Code marks them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config, db, reference

log = logging.getLogger("nyaya.indiacode")

API = "https://indiacode.gov.in/server/api"
SITE = "https://indiacode.gov.in"
ALLOWED_HOST = "indiacode.gov.in"
MAX_BYTES = 60 * 1024 * 1024
CENTRAL = "Central"
STATES_DIR = "states"
_manifest_lock = threading.Lock()


class IndiaCodeError(Exception):
    """Safe to show: India Code could not be reached or did not return a usable act."""


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": "NyaySutra-AI/1.0 (legal research prototype; bare-act fetcher)"},
                        timeout=httpx.Timeout(90, connect=20), follow_redirects=False)


def _get_json(c: httpx.Client, url: str, **params) -> dict:
    if httpx.URL(url).host != ALLOWED_HOST:
        raise IndiaCodeError("Refusing to contact a host other than indiacode.gov.in.")
    last: Exception | None = None
    for _ in range(3):
        try:
            r = c.get(url, params=params or None)
            if 400 <= r.status_code < 500:   # a refused request will not succeed on a retry
                raise IndiaCodeError("India Code could not run this search. Try different words.")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
    raise IndiaCodeError("India Code did not respond. Try again later.") from last


def _md(item: dict, key: str) -> str:
    return ((item.get("metadata") or {}).get(key) or [{}])[0].get("value") or ""


def canonical_state(name: str) -> str | None:
    """Our spelling of a state / UT name ('puducherry' → 'Puducherry'), or None if it is not one."""
    low = (name or "").strip().lower()
    return next((s for s in reference.STATES if s.lower() == low), None)


def jurisdiction_of(item: dict) -> str:
    if _md(item, "dc.identifier.act_id").startswith("AC_CEN"):
        return CENTRAL
    return canonical_state(_md(item, "dc.identifier.state_name")) or _md(item, "dc.identifier.state_name") or ""


def describe(item: dict) -> dict:
    repealed = _md(item, "dc.identifier.repealed").lower()
    return {
        "uuid": item["uuid"], "title": re.sub(r"\s+", " ", _md(item, "dc.title")).strip(),
        "year": _md(item, "dc.date.act_year"), "act_number": _md(item, "dc.identifier.act_number"),
        "jurisdiction": jurisdiction_of(item), "act_id": _md(item, "dc.identifier.act_id"),
        "enforcement_date": _md(item, "dc.date.enforcement_date"),
        "repealed": True if repealed == "true" else False if repealed == "false" else None,
        "item_url": f"{SITE}/items/{item['uuid']}",
    }


def _quote(q: str) -> str:
    """Free text for India Code's Solr query: words only (no operators the user did not mean)."""
    words = re.findall(r"[\w'-]+", q or "", re.UNICODE)[:12]
    return " ".join(w.replace('"', "") for w in words)


def catalogue(state: str | None, q: str = "", page: int = 0, size: int = 20) -> dict:
    """One page of India Code's acts for a state / UT (or Central Acts when state is 'Central')."""
    size = max(1, min(size, 40))
    parts = ["dc.identifier.collection:ACT"]
    if state and state != CENTRAL:
        st = canonical_state(state)
        if not st:
            raise IndiaCodeError("Unknown state or Union Territory.")
        parts.append(f'dc.identifier.state_name:"{st}"')
    elif state == CENTRAL:
        parts.append('dc.identifier.state_name:"Central"')   # India Code files Central Acts under "CENTRAL"
    words = _quote(q)
    if words:
        parts.insert(0, f"({words})")
    with client() as c:   # best match first when searching; otherwise the most recently filed acts
        res = _get_json(c, f"{API}/discover/search/objects", query=" AND ".join(parts), dsoType="ITEM",
                        size=size, page=max(0, page), sort="score,DESC" if words else "dc.date.issued,DESC")
    sr = res["_embedded"]["searchResult"]
    items = [describe(o["_embedded"]["indexableObject"]) for o in sr["_embedded"]["objects"]]
    in_lib = _library_by_item([i["item_url"] for i in items])
    for i in items:
        i["in_library"] = in_lib.get(i["item_url"])
    return {"items": items, "total": sr["page"]["totalElements"], "page": sr["page"]["number"],
            "pages": sr["page"]["totalPages"], "source": "India Code — Legislative Department, Ministry of Law and Justice",
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _library_by_item(urls: list[str]) -> dict[str, dict]:
    """Library documents already imported from these India Code records (by item URL)."""
    out: dict[str, dict] = {}
    if not urls:
        return out
    with db.system():
        for r in db.conn().execute("SELECT id, status, meta FROM documents WHERE collection='bare_act' AND status <> 'superseded'"):
            url = (json.loads(r["meta"] or "{}") if isinstance(r["meta"], str) else (r["meta"] or {})).get("item_url")
            if url in urls:
                out[url] = {"doc_id": r["id"], "status": r["status"]}
    return out


def english_pdf(c: httpx.Client, item_uuid: str) -> dict | None:
    """The act's English text PDF among its files (India Code also files Hindi / regional-language versions)."""
    bundles = _get_json(c, f"{API}/core/items/{item_uuid}/bundles")
    for b in bundles["_embedded"]["bundles"]:
        if b["name"] != "ORIGINAL":
            continue
        streams = _get_json(c, b["_links"]["bitstreams"]["href"])["_embedded"]["bitstreams"]
        pdfs = [s for s in streams if s["name"].lower().endswith(".pdf")]
        # Central Acts: A<year>-<no>.pdf (English) / H… (Hindi). State Acts: often …(e).pdf / …(k).pdf etc.
        for pref in (lambda n: re.match(r"^a\d{4}-\d+", n, re.I),
                     lambda n: re.search(r"\(e\)|[_\s-]e\.pdf$|english|_eng", n, re.I),
                     lambda n: not n.upper().startswith("H") and not re.search(r"\([a-df-z]\)|hindi", n, re.I)):
            eng = [s for s in pdfs if pref(s["name"])]
            if eng:
                return min(eng, key=lambda s: s["sizeBytes"])
    return None


def _download(c: httpx.Client, url: str, dest: Path) -> str:
    if httpx.URL(url).host != ALLOWED_HOST:
        raise IndiaCodeError("Refusing to download from a host other than indiacode.gov.in.")
    h, size = hashlib.sha256(), 0
    tmp = dest.with_suffix(".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    if not size and not chunk.startswith(b"%PDF"):
                        raise IndiaCodeError("India Code did not return a PDF for this act.")
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise IndiaCodeError("The act's file is too large to import.")
                    h.update(chunk)
                    f.write(chunk)
        tmp.replace(dest)
    except httpx.HTTPError as exc:
        raise IndiaCodeError("India Code did not respond. Try again later.") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return h.hexdigest()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:90]


def read_manifest() -> dict:
    """Provenance of every library act: the curated set (manifest.json) plus acts imported at run time
    (states/manifest.json — kept with their files and out of version control). Keys are paths under bare_acts/."""
    out: dict = {}
    for path in (config.BARE_ACTS_DIR / "manifest.json", config.BARE_ACTS_DIR / STATES_DIR / "manifest.json"):
        try:
            out.update(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError):
            pass
    return out


def update_manifest(key: str, entry: dict) -> None:
    path = config.BARE_ACTS_DIR / STATES_DIR / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _manifest_lock:
        data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        data[key] = entry
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)


def import_act(item_uuid: str, owner: str | None) -> dict:
    """Download one act from India Code into the library and queue it for indexing. Returns the document."""
    from . import ingest

    if not re.fullmatch(r"[0-9a-f-]{36}", item_uuid or ""):
        raise IndiaCodeError("Not an India Code record id.")
    with client() as c:
        item = _get_json(c, f"{API}/core/items/{item_uuid}")
        if item.get("withdrawn"):
            raise IndiaCodeError("India Code has withdrawn this record.")
        if _md(item, "dc.identifier.collection") != "ACT":
            raise IndiaCodeError("This India Code record is not an Act.")
        info = describe(item)
        if not info["jurisdiction"]:
            raise IndiaCodeError("India Code does not say which jurisdiction this act belongs to.")
        existing = _library_by_item([info["item_url"]]).get(info["item_url"])
        if existing:
            return {"id": existing["doc_id"], "status": existing["status"], "duplicate": True, **info}
        pdf = english_pdf(c, item_uuid)
        if not pdf:
            raise IndiaCodeError("India Code has no English text file for this act.")
        folder = config.BARE_ACTS_DIR / STATES_DIR / slug(info["jurisdiction"])   # Central Acts: states/central/
        dest = folder / f"{slug(info['title'])}_{info['year'] or 'na'}.pdf"
        pdf_url = f"{API}/core/bitstreams/{pdf['uuid']}/content"
        sha = _download(c, pdf_url, dest)
    meta = {"title": info["title"], "act_number": info["act_number"], "year": info["year"],
            "enforcement_date": info["enforcement_date"], "repealed": info["repealed"],
            "jurisdiction": info["jurisdiction"], "act_id": info["act_id"],
            "source": "India Code (Legislative Department, Ministry of Law and Justice)",
            "item_url": info["item_url"], "pdf_url": pdf_url, "pdf_name": pdf["name"],
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    update_manifest(dest.relative_to(config.BARE_ACTS_DIR).as_posix(), {**meta, "sha256": sha})
    with db.system():
        doc, created = ingest.register(dest, collection="bare_act", case_id=None, doc_type="statute",
                                       title=info["title"], filename=dest.name, meta=meta, sha=sha)
        if created:
            ingest.queue(doc["id"], owner=owner, org_id=db.DEFAULT_ORG)
    db.audit("library.import", "document", doc["id"], source="indiacode", jurisdiction=info["jurisdiction"],
             item=item_uuid)
    return {"id": doc["id"], "status": doc["status"], "duplicate": not created, **info}
