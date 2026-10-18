"""Case management module for NyaySutra AI.

Provides persistent storage, querying, and formatting for legal cases
categorized by type (Civil, Criminal, Family, Revenue).
"""
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import config

log = logging.getLogger(__name__)

# Primary case types as requested: civil / criminal / family / revenue
CASE_TYPES = ["Civil", "Criminal", "Family", "Revenue"]

# Case type metadata: display names, icons, badge classes, descriptions
CASE_TYPE_META = {
    "Civil": {
        "icon": "📜",
        "badge_class": "badge-civil",
        "color": "#58a6ff",
        "desc": "Commercial disputes, contracts, torts, property, injunctions, recovery suits",
    },
    "Criminal": {
        "icon": "⚖️",
        "badge_class": "badge-criminal",
        "color": "#f85149",
        "desc": "Bail applications, trial defense, FIR quashing, appeals, white-collar crimes",
    },
    "Family": {
        "icon": "👨‍👩‍👦",
        "badge_class": "badge-family",
        "color": "#bc8cff",
        "desc": "Matrimonial disputes, divorce, custody, maintenance, domestic violence, succession",
    },
    "Revenue": {
        "icon": "🏛️",
        "badge_class": "badge-revenue",
        "color": "#e3b341",
        "desc": "Land revenue, mutation appeals, stamp duty, tax tribunals, tenancy disputes",
    },
}

CASE_STATUSES = [
    "Draft / Pre-filing",
    "Active / In Court",
    "Evidence Stage",
    "Final Arguments",
    "Reserved for Orders",
    "Disposed / Closed",
]


@dataclass
class CaseNote:
    note_id: str
    timestamp: str
    author: str
    content: str


@dataclass
class LegalCase:
    case_id: str
    title: str
    case_type: str  # Civil, Criminal, Family, Revenue
    case_number: str
    court: str
    client_name: str
    client_role: str
    opposite_party: str
    filing_date: str
    next_hearing_date: Optional[str] = None
    status: str = "Active / In Court"
    acts_sections: str = ""
    description: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    notes: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LegalCase":
        # Safe migration if fields are missing
        return cls(
            case_id=data.get("case_id") or str(uuid.uuid4())[:8],
            title=data.get("title", "Untitled Case"),
            case_type=data.get("case_type", "Civil"),
            case_number=data.get("case_number", "Unregistered"),
            court=data.get("court", "District Court"),
            client_name=data.get("client_name", "Anonymous Client"),
            client_role=data.get("client_role", "Petitioner / Plaintiff"),
            opposite_party=data.get("opposite_party", "Opposite Party"),
            filing_date=data.get("filing_date", date.today().isoformat()),
            next_hearing_date=data.get("next_hearing_date"),
            status=data.get("status", "Active / In Court"),
            acts_sections=data.get("acts_sections", ""),
            description=data.get("description", ""),
            created_at=data.get("created_at", datetime.now().isoformat()),
            notes=data.get("notes", []),
        )


def _get_storage_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "cases.json"


def _get_seed_cases() -> List[LegalCase]:
    """Pre-loaded sample cases representing Civil, Criminal, Family, and Revenue matters."""
    return [
        LegalCase(
            case_id="case-civ-001",
            title="M/S Gayatri Construction vs. PCMC & Ors.",
            case_type="Civil",
            case_number="ARB.APPL. 412/2024",
            court="High Court of Judicature at Bombay",
            client_name="M/S Gayatri Construction Company",
            client_role="Petitioner / Contractor",
            opposite_party="Pimpri Chinchwad Municipal Corporation",
            filing_date="2024-02-15",
            next_hearing_date="2024-11-28",
            status="Active / In Court",
            acts_sections="Arbitration & Conciliation Act Sec. 11, Sec. 34; Contract Act Sec. 73, 74",
            description="Petition under Sec. 11 for appointment of sole arbitrator following termination of civic pipeline contract. Recovery of withheld bank guarantee and unpaid running account bills.",
            notes=[
                {
                    "note_id": "n1",
                    "timestamp": "2024-03-01 11:30",
                    "author": "Advocate",
                    "content": "Notice invoking arbitration issued on 15 Jan 2024. Respondent failed to reply within 30 days.",
                }
            ],
        ),
        LegalCase(
            case_id="case-crim-002",
            title="State of NCT of Delhi vs. Vikram Malhotra",
            case_type="Criminal",
            case_number="BAIL APPLN. 1290/2024",
            court="High Court of Delhi at New Delhi",
            client_name="Vikram Malhotra",
            client_role="Applicant / Accused",
            opposite_party="State (Govt. of NCT of Delhi) & Complainant",
            filing_date="2024-04-10",
            next_hearing_date="2024-11-15",
            status="Active / In Court",
            acts_sections="BNSS Sec. 483 (CrPC Sec. 439), BNS Sec. 316, 318 (IPC Sec. 409, 420)",
            description="Regular bail application in FIR No. 204/2024 registered at PS EOW. Allegations of corporate breach of trust. Applicant has cooperated with investigation; charge-sheet filed without arrest.",
            notes=[
                {
                    "note_id": "n2",
                    "timestamp": "2024-04-12 16:00",
                    "author": "Advocate",
                    "content": "Status report called from Investigating Officer. IO admitted no custodial interrogation requested.",
                }
            ],
        ),
        LegalCase(
            case_id="case-fam-003",
            title="Rohini Deshmukh vs. Sameer Deshmukh",
            case_type="Family",
            case_number="HMA PET. 58/2024",
            court="Principal Family Court, Pune",
            client_name="Rohini Deshmukh",
            client_role="Petitioner / Wife",
            opposite_party="Sameer Deshmukh",
            filing_date="2024-01-20",
            next_hearing_date="2024-12-05",
            status="Evidence Stage",
            acts_sections="Hindu Marriage Act 1955 Sec. 13(1)(ia), Sec. 24, Sec. 25; DV Act Sec. 12",
            description="Petition for dissolution of marriage on ground of cruelty and desertion, accompanied by interim maintenance application u/s 24 HMA and child custody application for minor daughter.",
            notes=[
                {
                    "note_id": "n3",
                    "timestamp": "2024-02-18 14:15",
                    "author": "Advocate",
                    "content": "Counselling mediation failed. Matter referred back to court for framing of issues.",
                }
            ],
        ),
        LegalCase(
            case_id="case-rev-004",
            title="Kisan Agro Developers vs. Sub-Divisional Officer & Tahsildar",
            case_type="Revenue",
            case_number="REV.REV.APPL. 89/2024",
            court="Maharashtra Revenue Tribunal (MRT), Pune Bench",
            client_name="Kisan Agro Developers LLP",
            client_role="Revision Applicant",
            opposite_party="SDO Haveli, Tahsildar & Village Gram Panchayat",
            filing_date="2024-03-05",
            next_hearing_date="2024-11-22",
            status="Final Arguments",
            acts_sections="Maharashtra Land Revenue Code 1966 Sec. 44, Sec. 247, Sec. 257 (NA Conversion)",
            description="Revision appeal against order of SDO rejecting Non-Agricultural (NA) commercial conversion permission for Survey No. 42/1A on erroneous interpretation of regional master plan zone boundaries.",
            notes=[
                {
                    "note_id": "n4",
                    "timestamp": "2024-03-25 10:00",
                    "author": "Advocate",
                    "content": "Town Planning Department NOC filed on record. SDO report contradicted by official DP sheet.",
                }
            ],
        ),
    ]


def load_cases() -> List[LegalCase]:
    """Load cases from JSON storage. Initializes seed cases if file does not exist."""
    path = _get_storage_path()
    if not path.exists():
        seeds = _get_seed_cases()
        save_cases(seeds)
        return seeds
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [LegalCase.from_dict(item) for item in data]
    except Exception as e:
        log.warning("Could not read cases file (%s), reinitializing seed cases.", e)
        seeds = _get_seed_cases()
        save_cases(seeds)
        return seeds


def save_cases(cases: List[LegalCase]) -> None:
    """Save cases to JSON storage atomically."""
    path = _get_storage_path()
    try:
        data = [c.to_dict() for c in cases]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error("Failed to save cases: %s", e)
        raise


def create_case(
    title: str,
    case_type: str,
    case_number: str,
    court: str,
    client_name: str,
    client_role: str,
    opposite_party: str,
    filing_date: str,
    next_hearing_date: Optional[str] = None,
    status: str = "Active / In Court",
    acts_sections: str = "",
    description: str = "",
) -> LegalCase:
    """Create a new LegalCase and persist it."""
    # Normalize case type to match predefined list if case differs
    matched_type = "Civil"
    for ct in CASE_TYPES:
        if ct.lower() == case_type.strip().lower():
            matched_type = ct
            break

    # Generate readable ID with type prefix
    prefix = matched_type[:3].upper()
    case_id = f"CASE-{prefix}-{str(uuid.uuid4())[:6]}"

    new_case = LegalCase(
        case_id=case_id,
        title=title.strip(),
        case_type=matched_type,
        case_number=case_number.strip(),
        court=court.strip(),
        client_name=client_name.strip(),
        client_role=client_role.strip(),
        opposite_party=opposite_party.strip(),
        filing_date=filing_date,
        next_hearing_date=next_hearing_date if next_hearing_date else None,
        status=status,
        acts_sections=acts_sections.strip(),
        description=description.strip(),
        created_at=datetime.now().isoformat(),
        notes=[],
    )

    cases = load_cases()
    # Insert at top so newest appears first
    cases.insert(0, new_case)
    save_cases(cases)
    return new_case


def get_case(case_id: str) -> Optional[LegalCase]:
    cases = load_cases()
    for c in cases:
        if c.case_id == case_id:
            return c
    return None


def update_case(case_id: str, **kwargs) -> Optional[LegalCase]:
    cases = load_cases()
    for i, c in enumerate(cases):
        if c.case_id == case_id:
            current_dict = c.to_dict()
            for k, v in kwargs.items():
                if k in current_dict:
                    current_dict[k] = v
            updated = LegalCase.from_dict(current_dict)
            cases[i] = updated
            save_cases(cases)
            return updated
    return None


def delete_case(case_id: str) -> bool:
    cases = load_cases()
    initial_len = len(cases)
    cases = [c for c in cases if c.case_id != case_id]
    if len(cases) < initial_len:
        save_cases(cases)
        return True
    return False


def add_note_to_case(case_id: str, content: str, author: str = "Advocate") -> Optional[LegalCase]:
    cases = load_cases()
    for c in cases:
        if c.case_id == case_id:
            note = {
                "note_id": str(uuid.uuid4())[:6],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "author": author,
                "content": content.strip(),
            }
            c.notes.append(note)
            save_cases(cases)
            return c
    return None


def format_case_context_for_llm(case: LegalCase) -> str:
    """Format case facts as a prompt context snippet for research or drafting."""
    return (
        f"--- ACTIVE CASE DOSSIER ---\n"
        f"Case Title: {case.title}\n"
        f"Case Type: {case.case_type}\n"
        f"Case / Filing No: {case.case_number}\n"
        f"Court / Forum: {case.court}\n"
        f"Client: {case.client_name} ({case.client_role})\n"
        f"Opposite Party: {case.opposite_party}\n"
        f"Applicable Sections / Statutes: {case.acts_sections or 'Not specified'}\n"
        f"Key Facts & Background:\n{case.description or 'No facts entered.'}\n"
        f"---------------------------\n"
    )
