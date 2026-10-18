"""Deterministic legal date tools. The LLM never does date arithmetic.

The limitation schedule below is taken from the Schedule to the Limitation Act,
1963 (and the special statutes named). It is a starting point: always confirm
the article against the bare Act (it is indexed in the library) and consider
ss. 4–24 (court holidays, condonation u/s 5, exclusion of time u/s 12–15,
acknowledgment u/s 18, part-payment u/s 19, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from dateutil.relativedelta import relativedelta


@dataclass(frozen=True)
class Period:
    label: str
    delta: relativedelta
    basis: str
    starts: str
    area: str


LIMITATION: dict[str, Period] = {
    "money_lent": Period("Suit for money lent", relativedelta(years=3),
                         "Limitation Act 1963, Sch. Art. 19", "When the loan is made", "Civil"),
    "breach_of_contract": Period("Compensation for breach of contract", relativedelta(years=3),
                                 "Limitation Act 1963, Sch. Art. 55", "When the contract is broken", "Civil"),
    "specific_performance": Period("Specific performance of a contract", relativedelta(years=3),
                                   "Limitation Act 1963, Sch. Art. 54",
                                   "Date fixed for performance, or when plaintiff has notice of refusal", "Civil"),
    "price_of_goods": Period("Price of goods sold and delivered (no fixed credit period)", relativedelta(years=3),
                             "Limitation Act 1963, Sch. Art. 14", "Date of delivery of the goods", "Civil"),
    "money_payable_bill": Period("Suit on a bill of exchange / promissory note payable at fixed time",
                                 relativedelta(years=3), "Limitation Act 1963, Sch. Art. 35",
                                 "When the bill or note falls due", "Civil"),
    "possession_on_title": Period("Possession of immovable property based on title", relativedelta(years=12),
                                  "Limitation Act 1963, Sch. Art. 65",
                                  "When possession of the defendant becomes adverse", "Civil"),
    "possession_dispossessed": Period("Possession based on previous possession (dispossessed)",
                                      relativedelta(years=12), "Limitation Act 1963, Sch. Art. 64",
                                      "Date of dispossession", "Civil"),
    "declaration": Period("Suit for a declaration (residuary)", relativedelta(years=3),
                          "Limitation Act 1963, Sch. Art. 58", "When the right to sue first accrues", "Civil"),
    "cancellation_of_instrument": Period("Cancellation or setting aside of an instrument or decree",
                                         relativedelta(years=3), "Limitation Act 1963, Sch. Art. 59",
                                         "When facts entitling cancellation first become known", "Civil"),
    "residuary_suit": Period("Any other suit (residuary)", relativedelta(years=3),
                                "Limitation Act 1963, Sch. Art. 113", "When the right to sue accrues", "Civil"),
    "appeal_district_court": Period("Appeal under CPC to a District Court", relativedelta(days=30),
                                    "Limitation Act 1963, Sch. Art. 116(b)", "Date of the decree or order", "Civil"),
    "first_appeal_high_court": Period("Appeal under CPC to a High Court", relativedelta(days=90),
                                      "Limitation Act 1963, Sch. Art. 116(a)", "Date of the decree or order", "Civil"),
    "review_of_judgment": Period("Review of judgment by a court other than the Supreme Court", relativedelta(days=30),
                                 "Limitation Act 1963, Sch. Art. 124", "Date of the decree or order", "Civil"),
    "execution_of_decree": Period("Execution of a decree (other than mandatory injunction)", relativedelta(years=12),
                                  "Limitation Act 1963, Sch. Art. 136", "When the decree becomes enforceable", "Civil"),
    "residuary_application": Period("Any other application (residuary)", relativedelta(years=3),
                                    "Limitation Act 1963, Sch. Art. 137", "When the right to apply accrues", "Civil"),
    "set_aside_award": Period("Application to set aside an arbitral award", relativedelta(months=3),
                              "Arbitration and Conciliation Act 1996, s.34(3) (+30 days on sufficient cause)",
                              "Date of receipt of the arbitral award", "Civil"),
    "criminal_appeal_high_court": Period("Criminal appeal from conviction to the High Court", relativedelta(days=60),
                                         "Limitation Act 1963, Sch. Art. 115(b)(i)",
                                         "Date of the sentence or order", "Criminal"),
    "criminal_appeal_sessions": Period("Criminal appeal from conviction to any other court", relativedelta(days=30),
                                       "Limitation Act 1963, Sch. Art. 115(b)(ii)",
                                       "Date of the sentence or order", "Criminal"),
    "cheque_dishonour_notice": Period("Demand notice after cheque dishonour", relativedelta(days=30),
                                      "Negotiable Instruments Act 1881, s.138 proviso (b)",
                                      "Date of receiving information of dishonour from the bank", "Criminal"),
    "cheque_dishonour_complaint": Period("Complaint for cheque dishonour", relativedelta(months=1),
                                         "Negotiable Instruments Act 1881, s.142(1)(b)",
                                         "Date the cause of action arises (expiry of 15 days after notice is received)",
                                         "Criminal"),
    "consumer_complaint": Period("Consumer complaint", relativedelta(years=2),
                                 "Consumer Protection Act 2019, s.69(1)",
                                 "Date on which the cause of action arose", "Civil"),
    "hma_appeal": Period("Appeal from a decree/order under the Hindu Marriage Act", relativedelta(days=90),
                         "Hindu Marriage Act 1955, s.28(4)", "Date of the decree or order", "Family"),
    "mlrc_appeal": Period("First appeal under the Maharashtra Land Revenue Code", relativedelta(days=60),
                          "Maharashtra Land Revenue Code 1966, s.248(1)(a)",
                          "Date of the order appealed against", "Revenue"),
    "mlrc_second_appeal": Period("Second appeal under the Maharashtra Land Revenue Code", relativedelta(days=90),
                                 "Maharashtra Land Revenue Code 1966, s.248(1)(b)",
                                 "Date of the order appealed against", "Revenue"),
}

CAVEAT = ("Computed mechanically from the start date. Check the governing article in the bare Act and any "
          "exclusions/extensions: s.4 (court closed on last day), s.5 (condonation for appeals/applications), "
          "s.12 (time to obtain certified copies), s.14 (bona fide proceedings in wrong court), s.18–19 "
          "(acknowledgment / part-payment), and special-statute rules.")


def _parse(d: str) -> date:
    return datetime.strptime(d.strip()[:10], "%Y-%m-%d").date()


def categories() -> list[dict]:
    return [{"key": k, "label": p.label, "basis": p.basis, "starts": p.starts, "area": p.area,
             "period": describe(p.delta)} for k, p in LIMITATION.items()]


def describe(d: relativedelta) -> str:
    parts = []
    for unit in ("years", "months", "days"):
        v = getattr(d, unit)
        if v:
            parts.append(f"{v} {unit[:-1] if v == 1 else unit}")
    return " ".join(parts)


def limitation(cause: str, start: str, today: date | None = None) -> dict:
    key = cause.strip().lower().replace(" ", "_").replace("-", "_")
    if key not in LIMITATION:
        raise KeyError(cause)
    p = LIMITATION[key]
    s = _parse(start)
    deadline = s + p.delta
    left = (deadline - (today or date.today())).days
    return {"key": key, "label": p.label, "period": describe(p.delta), "basis": p.basis, "starts": p.starts,
            "start_date": s.isoformat(), "deadline": deadline.isoformat(), "days_left": left,
            "status": f"{left} days remaining" if left >= 0 else f"EXPIRED {-left} days ago", "caveat": CAVEAT}


def days_between(a: str, b: str) -> int:
    return (_parse(b) - _parse(a)).days
