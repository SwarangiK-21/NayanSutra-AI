"""Reference data for forms and jurisdiction checks: states, districts, courts/forums,
case categories, case types, party roles and the official sources a record can come from.

Only facts that are stable and verifiable live here. Anything that changes (court rules,
fees, penalties) is configured with a source link and a verification date elsewhere.
"""
from __future__ import annotations

import re

STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    # Union Territories
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

# District lists are added state by state (Maharashtra first). For other states the form accepts free text.
DISTRICTS = {
    "Maharashtra": [
        "Ahilyanagar (Ahmednagar)", "Akola", "Amravati", "Beed", "Bhandara", "Buldhana", "Chandrapur",
        "Chhatrapati Sambhajinagar (Aurangabad)", "Dharashiv (Osmanabad)", "Dhule", "Gadchiroli", "Gondia",
        "Hingoli", "Jalgaon", "Jalna", "Kolhapur", "Latur", "Mumbai City", "Mumbai Suburban", "Nagpur", "Nanded",
        "Nandurbar", "Nashik", "Palghar", "Parbhani", "Pune", "Raigad", "Ratnagiri", "Sangli", "Satara",
        "Sindhudurg", "Solapur", "Thane", "Wardha", "Washim", "Yavatmal",
    ],
}

HIGH_COURTS = [
    "Allahabad", "Andhra Pradesh", "Bombay", "Calcutta", "Chhattisgarh", "Delhi", "Gauhati", "Gujarat",
    "Himachal Pradesh", "Jammu & Kashmir and Ladakh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Madras", "Manipur", "Meghalaya", "Orissa", "Patna", "Punjab and Haryana", "Rajasthan", "Sikkim",
    "Telangana", "Tripura", "Uttarakhand",
]

# (forum name, level) — level drives the drafting engine and which official source applies.
COURTS: list[tuple[str, str]] = [
    ("Supreme Court of India", "supreme"),
    *[(f"High Court of {hc}" if hc != "Bombay" else "High Court of Judicature at Bombay", "high") for hc in HIGH_COURTS],
    ("Bombay High Court — Nagpur Bench", "high"),
    ("Bombay High Court — Aurangabad Bench", "high"),
    ("High Court of Bombay at Goa", "high"),
    ("District and Sessions Court", "district"),
    ("Civil Judge (Senior Division)", "district"),
    ("Civil Judge (Junior Division)", "district"),
    ("City Civil and Sessions Court, Mumbai", "district"),
    ("Court of Small Causes", "district"),
    ("Commercial Court", "district"),
    ("Chief Judicial Magistrate", "district"),
    ("Judicial Magistrate First Class", "district"),
    ("Metropolitan Magistrate", "district"),
    ("Family Court", "district"),
    ("Motor Accident Claims Tribunal", "tribunal"),
    ("National Consumer Disputes Redressal Commission", "consumer"),
    ("State Consumer Disputes Redressal Commission", "consumer"),
    ("District Consumer Disputes Redressal Commission", "consumer"),
    ("National Company Law Tribunal", "tribunal"),
    ("National Company Law Appellate Tribunal", "tribunal"),
    ("National Green Tribunal", "tribunal"),
    ("Debts Recovery Tribunal", "tribunal"),
    ("Debts Recovery Appellate Tribunal", "tribunal"),
    ("Industrial Court / Labour Court", "tribunal"),
    ("Co-operative Court", "tribunal"),
    ("Maharashtra Administrative Tribunal", "tribunal"),
    ("Maharashtra Revenue Tribunal", "revenue"),
    ("Divisional Commissioner", "revenue"),
    ("Collector", "revenue"),
    ("Sub-Divisional Officer", "revenue"),
    ("Tahsildar", "revenue"),
]

CATEGORIES = ["Civil", "Criminal", "Family", "Revenue", "Consumer", "Writ"]

CASE_TYPES = {
    "Civil": ["Civil Suit — Recovery of Money", "Civil Suit — Injunction", "Civil Suit — Specific Performance",
              "Civil Suit — Partition", "Civil Suit — Declaration / Possession", "Commercial Suit",
              "Regular Civil Appeal", "Civil Revision Application", "Execution Petition",
              "Arbitration Petition (Section 9)", "Arbitration Petition (Section 11)",
              "Arbitration Petition (Section 34)", "Miscellaneous Civil Application"],
    "Criminal": ["FIR / Investigation stage", "Regular Bail Application", "Anticipatory Bail Application",
                 "Complaint — Section 138 NI Act", "Private Complaint", "Sessions Case", "Summons / Warrant Case",
                 "Criminal Revision Application", "Criminal Appeal", "Quashing Petition (BNSS 528 / CrPC 482)",
                 "Criminal Miscellaneous Application"],
    "Family": ["Divorce Petition", "Mutual Consent Divorce Petition", "Maintenance Application",
               "Domestic Violence Application", "Custody / Guardianship Petition",
               "Restitution of Conjugal Rights", "Judicial Separation"],
    "Revenue": ["Mutation Objection", "Appeal (MLRC Section 247)", "Revision (MLRC Section 257)",
                "Tenancy Matter", "Land Acquisition Reference", "Boundary / Encroachment Dispute"],
    "Consumer": ["Consumer Complaint", "First Appeal", "Revision Petition", "Execution Application"],
    "Writ": ["Writ Petition (Civil)", "Writ Petition (Criminal)", "Public Interest Litigation",
             "Special Leave Petition"],
}

# Client role -> the usual opposite role. The first entry is the default for the category.
ROLE_PAIRS = {
    "Civil": [("Plaintiff", "Defendant"), ("Defendant", "Plaintiff"), ("Appellant", "Respondent"),
              ("Respondent", "Appellant"), ("Petitioner", "Respondent"), ("Applicant", "Opponent"),
              ("Opponent", "Applicant"), ("Decree-holder", "Judgment-debtor"),
              ("Judgment-debtor", "Decree-holder"), ("Claimant", "Respondent")],
    "Criminal": [("Complainant", "Accused"), ("Accused", "Complainant"), ("Accused", "State (Prosecution)"),
                 ("Applicant (Accused)", "State (Prosecution)"), ("Informant / Victim", "Accused"),
                 ("Petitioner", "Respondent (State)"), ("Appellant", "Respondent"),
                 ("Revision Applicant", "Respondent")],
    "Family": [("Petitioner", "Respondent"), ("Respondent", "Petitioner"), ("Applicant", "Non-applicant"),
               ("Non-applicant", "Applicant"), ("Aggrieved Person", "Respondent"),
               ("Respondent", "Aggrieved Person")],
    "Revenue": [("Appellant", "Respondent"), ("Respondent", "Appellant"), ("Applicant", "Opponent"),
                ("Opponent", "Applicant"), ("Objector", "Applicant")],
    "Consumer": [("Complainant", "Opposite Party"), ("Opposite Party", "Complainant"),
                 ("Appellant", "Respondent"), ("Respondent", "Appellant")],
    "Writ": [("Petitioner", "Respondent"), ("Respondent", "Petitioner")],
}


def roles_for(category: str) -> dict:
    pairs = ROLE_PAIRS.get(category) or ROLE_PAIRS["Civil"]
    client = list(dict.fromkeys(a for a, _ in pairs))
    opposite: dict[str, list[str]] = {}
    for a, b in pairs:
        opposite.setdefault(a, []).append(b)
    return {"client": client, "opposite": opposite,
            "all_opposite": list(dict.fromkeys(b for _, b in pairs))}


def needs_police_station(category: str, case_type: str = "", fir_number: str = "") -> bool:
    return category == "Criminal" or bool((fir_number or "").strip())


# ── Identifier validation ───────────────────────────────────────────────────
# CNR (eCourts Case Number Record): 16 characters = 4-letter establishment code (state + district/establishment)
# + 6-digit case serial + 4-digit year, e.g. MHPU010012342023.
CNR_RE = re.compile(r"^[A-Z]{4}\d{12}$")
# State prefixes of CNR establishment codes that we are confident about; anything else is reported as unrecognised.
CNR_STATE = {"MH": "Maharashtra", "DL": "Delhi", "KA": "Karnataka", "KL": "Kerala", "GJ": "Gujarat",
             "RJ": "Rajasthan", "UP": "Uttar Pradesh", "MP": "Madhya Pradesh", "WB": "West Bengal",
             "PB": "Punjab", "HR": "Haryana", "BR": "Bihar", "JH": "Jharkhand", "AS": "Assam",
             "HP": "Himachal Pradesh", "GA": "Goa", "TN": "Tamil Nadu", "AP": "Andhra Pradesh"}


def parse_cnr(raw: str) -> dict:
    cnr = re.sub(r"[\s\-/]", "", raw or "").upper()
    if not CNR_RE.match(cnr):
        return {"valid": False, "cnr": cnr,
                "error": "A CNR has 16 characters: 4 letters followed by 12 digits (e.g. MHPU010012342023)."}
    year = int(cnr[-4:])
    out = {"valid": True, "cnr": cnr, "establishment_code": cnr[:6], "serial": cnr[6:12], "year": year,
           "state_code": cnr[:2], "state": CNR_STATE.get(cnr[:2]), "inferred": True}
    if not 1950 <= year <= 2100:
        out.update(valid=False, error=f"The last four digits ({year}) should be the filing year.")
    return out


# ── Official sources ────────────────────────────────────────────────────────
# access: feed (machine-readable feed, polled) | api | manual (human-only: CAPTCHA/login, no API).
# 'checked' is the date the URL and access method were verified by hand.
SOURCES = {
    "ecourts": {
        "name": "eCourts Services (District & Taluka Courts)", "kind": "case_records", "access": "manual",
        "url": "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/index",
        "home": "https://services.ecourts.gov.in/ecourtindia_v6/",
        "why_manual": "Case status search requires solving a CAPTCHA; no public API is offered.",
        "search_modes": {
            "cnr": ["cnr_number"],
            "case_number": ["state", "district", "court", "case_type", "case_number", "filing_year"],
            "filing_number": ["state", "district", "court", "filing_number", "filing_year"],
            "fir_number": ["state", "district", "police_station", "fir_number", "filing_year"],
            "party_name": ["state", "district", "court", "party_name", "filing_year"],
        },
        "checked": "2026-10-22",
    },
    "sci": {
        "name": "Supreme Court of India", "kind": "case_records", "access": "manual",
        "url": "https://www.sci.gov.in/case-status-case-no/", "home": "https://www.sci.gov.in/",
        "extra_links": {"Case status by diary no.": "https://www.sci.gov.in/case-status-diary-no/",
                        "Judgments by case no.": "https://www.sci.gov.in/judgements-case-no/"},
        "why_manual": "Case status and judgment search require a CAPTCHA; no public API is offered.",
        "search_modes": {"case_number": ["case_type", "case_number", "filing_year"],
                         "diary_number": ["filing_number", "filing_year"]},
        "checked": "2026-10-22",
    },
    "bombay_hc": {
        "name": "High Court of Judicature at Bombay", "kind": "case_records", "access": "manual",
        "url": "https://bombayhighcourt.gov.in/bhc/casestatus/casenumber",
        "home": "https://bombayhighcourt.gov.in/bhc/",
        "why_manual": "Case status search requires a CAPTCHA; no public API is offered.",
        "search_modes": {"case_number": ["court", "case_type", "case_number", "filing_year"],
                         "party_name": ["court", "party_name", "filing_year"]},
        "checked": "2026-10-22",
    },
    "mh_police_fir": {
        "name": "Maharashtra Police — Citizen Portal (FIR copies)", "kind": "fir", "access": "manual",
        "url": "https://citizen.mahapolice.gov.in/Citizen/MH/index.aspx",
        "home": "https://citizen.mahapolice.gov.in/",
        "why_manual": "FIR viewing requires district, police station, year and FIR number plus a CAPTCHA; "
                      "no API. The portal did not respond from this computer when checked.",
        "search_modes": {"fir_number": ["state", "district", "police_station", "fir_number", "filing_year"]},
        "checked": "2026-10-22",
    },
    "livelaw": {
        "name": "LiveLaw (legal news)", "kind": "news", "access": "feed",
        "url": "https://www.livelaw.in/google_feeds.xml", "home": "https://www.livelaw.in/",
        "note": "Official RSS feed advertised on the LiveLaw home page. Headlines and links only; "
                "full articles open on livelaw.in.",
        "checked": "2026-10-22",
    },
    "latestlaws": {
        "name": "LatestLaws (legal news & research)", "kind": "news", "access": "manual",
        "url": "https://www.latestlaws.com/", "home": "https://www.latestlaws.com/",
        "why_manual": "No public feed or API is advertised, and robots.txt disallows its API and feed paths.",
        "checked": "2026-10-22",
    },
    "indiacode": {
        "name": "India Code (official bare acts)", "kind": "statutes", "access": "api",
        "url": "https://indiacode.gov.in/", "home": "https://indiacode.gov.in/",
        "note": "Public DSpace REST API used to download the bare acts; checked for updated files.",
        "checked": "2026-10-22",
    },
}


def source_for_court(court: str) -> str:
    c = (court or "").lower()
    if "supreme court" in c:
        return "sci"
    if "bombay" in c and ("high court" in c or "bench" in c):
        return "bombay_hc"
    return "ecourts"


def form_options() -> dict:
    return {"states": STATES, "districts": DISTRICTS, "courts": [{"name": n, "level": lvl} for n, lvl in COURTS],
            "categories": CATEGORIES, "case_types": CASE_TYPES,
            "roles": {c: roles_for(c) for c in CATEGORIES}}
