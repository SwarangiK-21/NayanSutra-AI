# These are deterministic Python tools (limitation deadline calculator, date math) used by the ReAct agent

"""Deterministic tools for the calculation agent. Never let an LLM do date arithmetic."""
from datetime import date, datetime

from dateutil.relativedelta import relativedelta
from langchain_core.tools import tool

# SEED DATA: a starting point only. Verify each entry against the bare Act before production use.
LIMITATION_TABLE = {
    "money_lent": (relativedelta(years=3), "Limitation Act 1963, Art. 19 (money payable for money lent)"),
    "breach_of_contract": (relativedelta(years=3), "Limitation Act 1963, Art. 55 (compensation for breach of contract)"),
    "recovery_of_immovable_property": (relativedelta(years=12), "Limitation Act 1963, Art. 65 (possession based on title)"),
    "first_appeal_to_high_court": (relativedelta(days=90), "Limitation Act 1963, Art. 116(a)"),
    "cheque_dishonour_complaint": (relativedelta(months=1), "NI Act 1881, s.142(1)(b) (from the date cause of action arises under s.138 proviso (c))"),
}

CAVEAT = (
    "Excludes Sec. 4 (court holidays), Sec. 5 (condonation), Sec. 12 (time-excluded), "
    "Sec. 14 and Sec. 17-18 (fraud, acknowledgment) adjustments. Verify against the bare Act."
)


def _parse(d: str) -> date:
    return datetime.strptime(d.strip(), "%Y-%m-%d").date()


@tool
def list_limitation_categories() -> str:
    """List the causes of action the limitation calculator supports."""
    return "\n".join(f"- {k}: {ref}" for k, (_, ref) in LIMITATION_TABLE.items())


@tool
def calculate_limitation_deadline(cause_of_action: str, accrual_date: str) -> str:
    """Compute the limitation deadline. cause_of_action must be a key from
    list_limitation_categories; accrual_date is YYYY-MM-DD."""
    key = cause_of_action.strip().lower().replace(" ", "_")
    if key not in LIMITATION_TABLE:
        return f"Unknown cause_of_action '{cause_of_action}'. Supported: {', '.join(LIMITATION_TABLE)}"
    period, ref = LIMITATION_TABLE[key]
    start = _parse(accrual_date)
    deadline = start + period
    left = (deadline - date.today()).days
    status = f"{left} days remaining" if left >= 0 else f"EXPIRED {-left} days ago"
    return f"Deadline: {deadline.isoformat()} ({status}). Basis: {ref}. Caveat: {CAVEAT}"


@tool
def days_between(start_date: str, end_date: str) -> str:
    """Number of days between two YYYY-MM-DD dates (end minus start)."""
    return str((_parse(end_date) - _parse(start_date)).days)


TOOLS = [list_limitation_categories, calculate_limitation_deadline, days_between]
