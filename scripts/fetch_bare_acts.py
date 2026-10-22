"""Download official bare acts (English PDFs) from India Code (indiacode.gov.in).

India Code is the Legislative Department's official repository of Central and
State Acts. This script uses its public DSpace REST API, downloads only from
that host, checks every file really is a PDF, and records provenance in
data/library/bare_acts/manifest.json.

    python scripts/fetch_bare_acts.py            # fetch everything missing
    python scripts/fetch_bare_acts.py --only "Limitation"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

API = "https://indiacode.gov.in/server/api"
ALLOWED_HOST = "indiacode.gov.in"
OUT = Path(__file__).resolve().parent.parent / "data" / "library" / "bare_acts"
MAX_BYTES = 60 * 1024 * 1024

# (exact India Code title, act-id prefix, short name, practice areas)
ACTS = [
    ("The Bharatiya Nyaya Sanhita, 2023", "AC_CEN", "BNS", ["Criminal"]),
    ("The Bharatiya Nagarik Suraksha Sanhita, 2023", "AC_CEN", "BNSS", ["Criminal"]),
    ("The Bharatiya Sakshya Adhiniyam, 2023", "AC_CEN", "BSA", ["Criminal", "Civil"]),
    ("The Code of Civil Procedure, 1908", "AC_CEN", "CPC", ["Civil"]),
    ("The Indian Contract Act, 1872", "AC_CEN", "Contract Act", ["Civil"]),
    ("The Specific Relief Act, 1963", "AC_CEN", "Specific Relief Act", ["Civil"]),
    ("The Limitation Act, 1963", "AC_CEN", "Limitation Act", ["Civil", "Criminal", "Family", "Revenue"]),
    ("The Negotiable Instruments Act, 1881", "AC_CEN", "NI Act", ["Criminal", "Civil"]),
    ("The Arbitration and Conciliation Act, 1996", "AC_CEN", "Arbitration Act", ["Civil"]),
    ("The Transfer of Property Act, 1882", "AC_CEN", "TPA", ["Civil", "Revenue"]),
    ("The Registration Act, 1908", "AC_CEN", "Registration Act", ["Civil", "Revenue"]),
    ("The Indian Stamp Act, 1899", "AC_CEN", "Stamp Act", ["Revenue", "Civil"]),
    ("The Commercial Courts Act, 2015", "AC_CEN", "Commercial Courts Act", ["Civil"]),
    ("The Consumer Protection Act, 2019", "AC_CEN", "Consumer Protection Act", ["Civil"]),
    ("The Information Technology Act, 2000", "AC_CEN", "IT Act", ["Criminal", "Civil"]),
    ("The Hindu Marriage Act, 1955", "AC_CEN", "HMA", ["Family"]),
    ("The Special Marriage Act, 1954", "AC_CEN", "Special Marriage Act", ["Family"]),
    ("The Hindu Succession Act, 1956", "AC_CEN", "Hindu Succession Act", ["Family", "Revenue"]),
    ("The Hindu Adoptions and Maintenance Act, 1956", "AC_CEN", "HAMA", ["Family"]),
    ("The Guardians and Wards Act, 1890", "AC_CEN", "Guardians and Wards Act", ["Family"]),
    ("The Family Courts Act, 1984", "AC_CEN", "Family Courts Act", ["Family"]),
    ("The Protection of Women from Domestic Violence Act, 2005", "AC_CEN", "DV Act", ["Family", "Criminal"]),
    ("The Dowry Prohibition Act, 1961", "AC_CEN", "Dowry Prohibition Act", ["Family", "Criminal"]),
    ("The Indian Succession Act, 1925", "AC_CEN", "Succession Act", ["Family", "Civil"]),
    ("The Protection of Children from Sexual Offences Act, 2012", "AC_CEN", "POCSO", ["Criminal"]),
    ("The Right to Fair Compensation and Transparency in Land Acquisition, Rehabilitation and Resettlement Act, 2013",
     "AC_CEN", "Land Acquisition Act 2013", ["Revenue"]),
    ("The Maharashtra Land Revenue Code, 1966", "AC_MH", "MLRC", ["Revenue"]),
    # Legal awareness (road safety, licences, registration, challans)
    ("The Motor Vehicles Act, 1988", "AC_CEN", "MV Act", ["Civil", "Criminal"]),
]


# Acts repealed in 2024 are no longer on India Code, but still govern pending
# matters. This IPC copy (India Code text) is hosted on an eCourts district site.
# The CrPC and Evidence Act were left out: no current official consolidation was
# reachable (the only Evidence Act copy found predates the 2000 amendments).
DIRECT = [
    ("The Indian Penal Code, 1860", "IPC", ["Criminal"], "45", "1860",
     "https://cdnbbsr.s3waas.gov.in/s3ec03a36b598abb934e4528412e5a2127/uploads/2023/09/2023091276.pdf",
     "District Court Chamarajanagara (eCourts, Govt. of India) — copy of India Code text"),
    # The Constitution is filed on India Code under state/UT records; this English copy is the most
    # current one available there (reflects amendments up to the 106th, 2023).
    ("The Constitution of India", "Constitution", ["Civil", "Criminal", "Family", "Revenue"], "", "1950",
     "https://indiacode.gov.in/server/api/core/bitstreams/5266be94-8cd4-45e5-b0ab-bd36b8c01f4f/content",
     "India Code (Legislative Department) — English text"),
]
DIRECT_HOSTS = {"cdnbbsr.s3waas.gov.in"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:90]


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": "NyaySutra-AI/1.0 (legal research prototype; bare-act fetcher)"},
        timeout=httpx.Timeout(90, connect=20),
        follow_redirects=False,
    )


def _get_json(c: httpx.Client, url: str, **params):
    for attempt in range(3):
        try:
            r = c.get(url, params=params or None)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def find_item(c: httpx.Client, title: str, prefix: str):
    q = f'title:"{title}" AND dc.identifier.collection:ACT'
    res = _get_json(c, f"{API}/discover/search/objects", query=q, dsoType="ITEM", size=20)
    for o in res["_embedded"]["searchResult"]["_embedded"]["objects"]:
        it = o["_embedded"]["indexableObject"]
        md = it["metadata"]

        def g(k: str) -> str:
            return (md.get(k) or [{}])[0].get("value") or ""

        if g("dc.title").strip().lower() == title.lower() and g("dc.identifier.act_id").startswith(prefix):
            keys = ("dc.identifier.act_number", "dc.date.act_year", "dc.date.enforcement_date",
                    "dc.identifier.repealed", "dc.identifier.act_id")
            return it, {k: g(k) for k in keys}
    return None, None


def english_pdf(c: httpx.Client, item_uuid: str):
    bundles = _get_json(c, f"{API}/core/items/{item_uuid}/bundles")
    for b in bundles["_embedded"]["bundles"]:
        if b["name"] != "ORIGINAL":
            continue
        streams = _get_json(c, b["_links"]["bitstreams"]["href"])["_embedded"]["bitstreams"]
        pdfs = [s for s in streams if s["name"].lower().endswith(".pdf")]
        # India Code names English text PDFs A<year>-<no>.pdf and Hindi ones H<year>-<no>.pdf;
        # other files are often scanned Gazette copies with no text layer.
        eng = [s for s in pdfs if re.match(r"^a\d{4}-\d+", s["name"], re.I)]
        eng = eng or [s for s in pdfs if not s["name"].upper().startswith("H")] or pdfs
        if eng:
            return min(eng, key=lambda s: s["sizeBytes"])
    return None


def download(c: httpx.Client, url: str, dest: Path) -> str:
    if httpx.URL(url).host not in {ALLOWED_HOST, *DIRECT_HOSTS}:
        raise ValueError(f"refusing to download from {url}")
    h = hashlib.sha256()
    tmp = dest.with_suffix(".part")
    with c.stream("GET", url) as r:
        r.raise_for_status()
        size, first = 0, True
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(65536):
                if first:
                    if not chunk.startswith(b"%PDF-"):
                        raise ValueError("response is not a PDF")
                    first = False
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("file too large")
                h.update(chunk)
                f.write(chunk)
    tmp.replace(dest)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="substring filter on act title")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}

    ok = fail = 0
    with _client() as c:
        for title, prefix, short, areas in ACTS:
            if args.only and args.only.lower() not in title.lower():
                continue
            fname = slug(title) + ".pdf"
            if fname in manifest and (OUT / fname).exists():
                print(f"= {title} (already downloaded)")
                ok += 1
                continue
            try:
                item, md = find_item(c, title, prefix)
                if not item:
                    raise LookupError("not found on India Code")
                bs = english_pdf(c, item["uuid"])
                if not bs:
                    raise LookupError("no PDF attached")
                url = bs["_links"]["content"]["href"]
                sha = download(c, url, OUT / fname)
                manifest[fname] = {
                    "title": title, "short": short, "areas": areas,
                    "act_number": md["dc.identifier.act_number"], "year": md["dc.date.act_year"],
                    "enforcement_date": md["dc.date.enforcement_date"],
                    "repealed": md["dc.identifier.repealed"] == "true",
                    "source": "India Code (Legislative Department, Ministry of Law and Justice)",
                    "item_url": f"https://indiacode.gov.in/items/{item['uuid']}",
                    "pdf_url": url, "pdf_name": bs["name"], "sha256": sha,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
                print(f"+ {title}  ({bs['sizeBytes'] // 1024} KB)", flush=True)
                ok += 1
            except Exception as exc:  # keep going; report at the end
                print(f"! {title}: {exc}", flush=True)
                fail += 1
            time.sleep(1.0)  # be polite to a government server

        for title, short, areas, act_no, year, url, source in DIRECT:
            if args.only and args.only.lower() not in title.lower():
                continue
            fname = slug(title) + ".pdf"
            if fname in manifest and (OUT / fname).exists():
                print(f"= {title} (already downloaded)")
                ok += 1
                continue
            try:
                sha = download(c, url, OUT / fname)
                repealed = short == "IPC"
                manifest[fname] = {
                    "title": title, "short": short, "areas": areas, "act_number": act_no, "year": year,
                    "repealed": repealed, "source": source, "pdf_url": url, "sha256": sha,
                    "note": "Repealed w.e.f. 1 July 2024 (replaced by BNS); kept for pending matters." if repealed else "",
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
                print(f"+ {title}", flush=True)
                ok += 1
            except Exception as exc:
                print(f"! {title}: {exc}", flush=True)
                fail += 1
    print(f"\nDone: {ok} available, {fail} failed. Files in {OUT}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
