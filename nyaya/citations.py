"""Parse statutory references in a question ("u/s 138 NI Act", "Order 39 Rule 1 CPC",
"Art. 21", "Section 13B HMA") into exact lookup keys used by the retriever."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .chunking import roman_to_int

# alias (lower-case, as typed by lawyers) -> substring of the act title in the library
ACT_ALIASES: dict[str, str] = {
    "bns": "Bharatiya Nyaya Sanhita", "bharatiya nyaya sanhita": "Bharatiya Nyaya Sanhita",
    "bnss": "Bharatiya Nagarik Suraksha Sanhita", "bharatiya nagarik suraksha sanhita": "Bharatiya Nagarik Suraksha Sanhita",
    "bsa": "Bharatiya Sakshya Adhiniyam", "bharatiya sakshya adhiniyam": "Bharatiya Sakshya Adhiniyam",
    "ipc": "Indian Penal Code", "indian penal code": "Indian Penal Code", "penal code": "Indian Penal Code",
    "crpc": "Code of Criminal Procedure", "cr.p.c": "Code of Criminal Procedure", "cr.p.c.": "Code of Criminal Procedure",
    "code of criminal procedure": "Code of Criminal Procedure",
    "cpc": "Code of Civil Procedure", "c.p.c": "Code of Civil Procedure", "code of civil procedure": "Code of Civil Procedure",
    "evidence act": "Evidence Act",
    "contract act": "Indian Contract Act", "ica": "Indian Contract Act", "indian contract act": "Indian Contract Act",
    "specific relief act": "Specific Relief Act", "sra": "Specific Relief Act",
    "limitation act": "Limitation Act",
    "ni act": "Negotiable Instruments Act", "n.i. act": "Negotiable Instruments Act", "n i act": "Negotiable Instruments Act",
    "negotiable instruments act": "Negotiable Instruments Act",
    "arbitration act": "Arbitration and Conciliation Act", "arbitration and conciliation act": "Arbitration and Conciliation Act",
    "tpa": "Transfer of Property Act", "transfer of property act": "Transfer of Property Act",
    "registration act": "Registration Act", "stamp act": "Indian Stamp Act", "indian stamp act": "Indian Stamp Act",
    "commercial courts act": "Commercial Courts Act", "consumer protection act": "Consumer Protection Act",
    "it act": "Information Technology Act", "information technology act": "Information Technology Act",
    "hma": "Hindu Marriage Act", "hindu marriage act": "Hindu Marriage Act",
    "special marriage act": "Special Marriage Act", "sma": "Special Marriage Act",
    "hindu succession act": "Hindu Succession Act", "hsa": "Hindu Succession Act",
    "hama": "Hindu Adoptions and Maintenance Act", "hindu adoptions and maintenance act": "Hindu Adoptions and Maintenance Act",
    "guardians and wards act": "Guardians and Wards Act", "gwa": "Guardians and Wards Act",
    "family courts act": "Family Courts Act",
    "dv act": "Domestic Violence", "pwdva": "Domestic Violence", "domestic violence act": "Domestic Violence",
    "dowry prohibition act": "Dowry Prohibition Act",
    "succession act": "Indian Succession Act", "indian succession act": "Indian Succession Act",
    "pocso": "Sexual Offences", "pocso act": "Sexual Offences",
    "land acquisition act": "Land Acquisition", "rfctlarr": "Land Acquisition", "larr act": "Land Acquisition",
    "mlrc": "Maharashtra Land Revenue Code", "land revenue code": "Maharashtra Land Revenue Code",
    "constitution": "Constitution of India",
}
_ALIAS_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted((re.escape(a) for a in ACT_ALIASES), key=len, reverse=True)) + r")(?![a-z])",
    re.I,
)

_REF = re.compile(
    r"\b(?:(?P<order>order)\s+(?P<ord>[ivxlc]+|\d{1,2})\s*,?\s*(?:rule|r\.?)\s*(?P<rule>\d{1,3}[a-z]?)"
    r"|(?P<kind>sections?|secs?\.?|ss?\.|u/s\.?|under\s+section|articles?|arts?\.)\s*(?P<num>\d{1,3}[a-z]{0,3})"
    r"(?P<more>(?:\s*(?:\(\s*\w{1,4}\s*\))+)?(?:\s*(?:,|and|&|/|to)\s*\d{1,3}[a-z]{0,3}(?:\s*\(\s*\w{1,4}\s*\))*)*))",
    re.I,
)


@dataclass(frozen=True)
class Ref:
    key: str          # S138, A21, O39R1
    act: str | None   # title substring, if the act was named


def mentioned_acts(text: str) -> list[str]:
    seen = []
    for m in _ALIAS_RE.finditer(text):
        a = ACT_ALIASES[m.group(1).lower()]
        if a not in seen:
            seen.append(a)
    return seen


def _nearest_act(text: str, start: int, end: int) -> str | None:
    """Act named closest to a reference: "Section 73 of the Contract Act" (after) or
    "Contract Act ss. 73, 74" (before). Separators like ';' end the search window."""
    before = re.split(r"[;\n?!]", text[max(0, start - 60):start])[-1]
    after = re.split(r"[;\n?!]", text[end:end + 70])[0]
    cand = []
    for m in _ALIAS_RE.finditer(before):
        cand.append((len(before) - m.end(), ACT_ALIASES[m.group(1).lower()]))
    for m in _ALIAS_RE.finditer(after):
        cand.append((m.start(), ACT_ALIASES[m.group(1).lower()]))
    return min(cand)[1] if cand else None


def parse_refs(text: str) -> list[Ref]:
    """Provision references with the nearest act mentioned after them (or anywhere)."""
    refs: list[Ref] = []
    for m in _REF.finditer(text):
        near = _nearest_act(text, m.start(), m.end())
        is_article = bool(m.group("kind")) and m.group("kind").lower().startswith("art")
        if near:
            act = near
        elif is_article:
            act = "Constitution of India"   # a bare "Article N" means the Constitution
        else:
            act = (mentioned_acts(text)[:1] or [None])[0]
        if m.group("order"):
            o = m.group("ord")
            o_num = int(o) if o.isdigit() else roman_to_int(o)
            refs.append(Ref(f"O{o_num}R{m.group('rule').upper()}", act))
            continue
        prefix = "A" if is_article else "S"
        nums = [m.group("num")] + re.findall(r"(?:,|and|&|/|to)\s*(\d{1,3}[a-z]{0,3})", m.group("more") or "", re.I)
        for n in nums[:6]:
            refs.append(Ref(f"{prefix}{n.upper()}", act))
    return list(dict.fromkeys(refs))
