"""Bulk-import a curated list of widely used Central Acts from India Code into the law library.

Acts that India Code no longer lists under these titles (e.g. the CrPC 1973 and Indian Evidence Act 1872, replaced
by the BNSS and BSA, and labour laws replaced by the four Labour Codes) are deliberately not in the list.

Each name is searched in India Code's Central Acts and imported only when a record's title and year match exactly
(punctuation and a leading "The" ignored); anything else is reported and skipped, never guessed. Acts already in the
library are skipped by import_act itself. Indexing runs in the app's background job queue, so the app must be running.

    python scripts/import_central_acts.py --dry-run     # show matches only
    python scripts/import_central_acts.py               # import
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nyaya import db, indiacode, ingest, tasks  # noqa: E402,F401  (tasks registers the "ingest" job kind)

ACTS = [
    # Criminal law (old codes still govern pre-1 July 2024 matters) and special criminal statutes
    ("Unlawful Activities (Prevention) Act", 1967), ("Criminal Procedure (Identification) Act", 2022),
    ("Prevention of Money-laundering Act", 2002), ("Narcotic Drugs and Psychotropic Substances Act", 1985),
    ("Prevention of Corruption Act", 1988), ("Arms Act", 1959), ("National Investigation Agency Act", 2008),
    ("Scheduled Castes and the Scheduled Tribes (Prevention of Atrocities) Act", 1989),
    ("Juvenile Justice (Care and Protection of Children) Act", 2015), ("Probation of Offenders Act", 1958),
    ("Immoral Traffic (Prevention) Act", 1956), ("Official Secrets Act", 1923), ("Explosive Substances Act", 1908),
    # Family and personal law
    ("Dissolution of Muslim Marriages Act", 1939), ("Muslim Personal Law (Shariat) Application Act", 1937),
    ("Muslim Women (Protection of Rights on Marriage) Act", 2019), ("Divorce Act", 1869),
    ("Parsi Marriage and Divorce Act", 1936), ("Hindu Minority and Guardianship Act", 1956),
    ("Maintenance and Welfare of Parents and Senior Citizens Act", 2007), ("Prohibition of Child Marriage Act", 2006),
    ("Medical Termination of Pregnancy Act", 1971),
    ("Pre-conception and Pre-natal Diagnostic Techniques (Prohibition of Sex Selection) Act", 1994),
    # Civil, property and procedure
    ("Indian Easements Act", 1882), ("Indian Trusts Act", 1882), ("Powers-of-Attorney Act", 1882),
    ("Sale of Goods Act", 1930), ("Indian Partnership Act", 1932), ("Court-fees Act", 1870), ("Suits Valuation Act", 1887),
    ("General Clauses Act", 1897), ("Prohibition of Benami Property Transactions Act", 1988), ("Notaries Act", 1952),
    ("Oaths Act", 1969), ("Real Estate (Regulation and Development) Act", 2016), ("Mediation Act", 2023),
    ("Public Premises (Eviction of Unauthorised Occupants) Act", 1971), ("Legal Services Authorities Act", 1987),
    ("Advocates Act", 1961), ("Contempt of Courts Act", 1971),
    # Corporate, commercial, banking, tax
    ("Companies Act", 2013), ("Insolvency and Bankruptcy Code", 2016), ("Limited Liability Partnership Act", 2008),
    ("Competition Act", 2002), ("Securities and Exchange Board of India Act", 1992), ("Banking Regulation Act", 1949),
    ("Reserve Bank of India Act", 1934),
    ("Securitisation and Reconstruction of Financial Assets and Enforcement of Security Interest Act", 2002),
    ("Recovery of Debts and Bankruptcy Act", 1993), ("Foreign Exchange Management Act", 1999),
    ("Payment and Settlement Systems Act", 2007), ("Insurance Act", 1938),
    ("Central Goods and Services Tax Act", 2017), ("Integrated Goods and Services Tax Act", 2017), ("Customs Act", 1962),
    # Intellectual property
    ("Trade Marks Act", 1999), ("Copyright Act", 1957), ("Patents Act", 1970), ("Designs Act", 2000),
    # Labour
    ("Code on Wages", 2019), ("Industrial Relations Code", 2020), ("Code on Social Security", 2020),
    ("Occupational Safety, Health and Working Conditions Code", 2020), ("Bonded Labour System (Abolition) Act", 1976),
    ("Child and Adolescent Labour (Prohibition and Regulation) Act", 1986),
    ("Sexual Harassment of Women at Workplace (Prevention, Prohibition and Redressal) Act", 2013),
    # Rights, information, technology, data
    ("Right to Information Act", 2005), ("Digital Personal Data Protection Act", 2023), ("Protection of Human Rights Act", 1993),
    ("Rights of Persons with Disabilities Act", 2016), ("Right of Children to Free and Compulsory Education Act", 2009),
    ("Telecommunications Act", 2023),
    ("Aadhaar (Targeted Delivery of Financial and Other Subsidies, Benefits and Services) Act", 2016),
    ("Mental Healthcare Act", 2017), ("Citizenship Act", 1955), ("Passports Act", 1967), ("Immigration and Foreigners Act", 2025),
    ("Representation of the People Act", 1951), ("Registration of Births and Deaths Act", 1969),
    # Regulatory, environment, health, transport
    ("Environment (Protection) Act", 1986), ("Water (Prevention and Control of Pollution) Act", 1974),
    ("Air (Prevention and Control of Pollution) Act", 1981), ("Wild Life (Protection) Act", 1972),
    ("National Green Tribunal Act", 2010), ("Disaster Management Act", 2005), ("Epidemic Diseases Act", 1897),
    ("Drugs and Cosmetics Act", 1940), ("Food Safety and Standards Act", 2006), ("Essential Commodities Act", 1955),
    ("Legal Metrology Act", 2009), ("Electricity Act", 2003), ("Carriage by Road Act", 2007), ("Railways Act", 1989),
    ("National Medical Commission Act", 2019), ("Prevention of Cruelty to Animals Act", 1960), ("Indian Forest Act", 1927),
    ("Scheduled Tribes and Other Traditional Forest Dwellers (Recognition of Forest Rights) Act", 2006),
    ("Muslim Women (Protection of Rights on Divorce) Act", 1986), ("Gram Nyayalayas Act", 2008),
    ("Lokpal and Lokayuktas Act", 2013), ("Whistle Blowers Protection Act", 2014), ("Protection of Civil Rights Act", 1955),
    ("Indian Contract Act", 1872),   # already in the library: checks the duplicate guard
]


def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower().replace("&", " and "))
    s = re.sub(r"\b(the|act|code|of|and|in|on)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find(name: str, year: int) -> dict | None:
    """The India Code record whose title is exactly this act (the year in the title wins over the record's year)."""
    want = norm(name)
    for q in (f"{name} {year}", name):
        for it in indiacode.catalogue(indiacode.CENTRAL, q, size=20)["items"]:
            title = it["title"] or ""
            m = re.search(r",?\s*(\d{4})\s*\.?$", title)
            t_year = m.group(1) if m else str(it.get("year") or "")[:4]
            if norm(title[: m.start()] if m else title) == want and t_year == str(year):
                return it
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ok, skipped, dup, failed = [], [], [], []
    if not a.dry_run:   # an act saved by an interrupted earlier run but never queued for indexing
        with db.system():
            stuck = [r["id"] for r in db.conn().execute(
                "SELECT id FROM documents WHERE collection='bare_act' AND status='processing' AND NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE jobs.dedupe_key = 'ingest:' || documents.id)")]
            for doc_id in stuck:
                ingest.queue(doc_id, owner=None, org_id=db.DEFAULT_ORG)
                print(f"  re-queued interrupted import: {doc_id}", flush=True)
    for name, year in ACTS:
        try:
            it = find(name, year)
        except indiacode.IndiaCodeError as exc:
            failed.append((name, str(exc)))
            continue
        if not it:
            skipped.append(f"{name}, {year}")
            print(f"  no exact match: {name}, {year}", flush=True)
            continue
        if it.get("in_library"):
            dup.append(name)
            continue
        if a.dry_run:
            print(f"  match: {it['title']} ({it.get('year')})", flush=True)
            ok.append(name)
            continue
        try:
            r = indiacode.import_act(it["uuid"], owner=None)
            (dup if r.get("duplicate") else ok).append(name)
            print(f"  {'already in library' if r.get('duplicate') else 'queued'}: {it['title']}", flush=True)
        except indiacode.IndiaCodeError as exc:
            failed.append((name, str(exc)))
            print(f"  failed: {name}: {exc}", flush=True)
        time.sleep(1.0)   # be polite to India Code
    print(f"\n{'matched' if a.dry_run else 'queued'}: {len(ok)} · already in library: {len(dup)} · "
          f"no exact match: {len(skipped)} · failed: {len(failed)}")
    for f in failed:
        print("  failed:", f)


if __name__ == "__main__":
    main()
