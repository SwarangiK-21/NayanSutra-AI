import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cases import (
    CASE_TYPES,
    create_case,
    delete_case,
    format_case_context_for_llm,
    get_case,
    load_cases,
    update_case,
)


def test_case_creation_and_types():
    assert "Civil" in CASE_TYPES
    assert "Criminal" in CASE_TYPES
    assert "Family" in CASE_TYPES
    assert "Revenue" in CASE_TYPES

    # Test creating a civil case
    c1 = create_case(
        title="Test Landlord vs Tenant",
        case_type="Civil",
        case_number="RCS 101/2024",
        court="Small Causes Court, Pune",
        client_name="Landlord A",
        client_role="Plaintiff",
        opposite_party="Tenant B",
        filing_date="2024-05-01",
        next_hearing_date="2024-12-01",
        description="Eviction suit on grounds of default in rent payment.",
    )
    assert c1.case_id.startswith("CASE-CIV-")
    assert c1.case_type == "Civil"
    assert c1.title == "Test Landlord vs Tenant"

    # Test creating a criminal case
    c2 = create_case(
        title="State vs. Test Accused",
        case_type="Criminal",
        case_number="BAIL 505/2024",
        court="Sessions Court",
        client_name="Test Accused",
        client_role="Accused",
        opposite_party="State",
        filing_date="2024-06-01",
    )
    assert c2.case_id.startswith("CASE-CRI-")
    assert c2.case_type == "Criminal"

    # Test creating a family case
    c3 = create_case(
        title="Wife vs. Husband",
        case_type="Family",
        case_number="FC 99/2024",
        court="Family Court",
        client_name="Wife",
        client_role="Petitioner",
        opposite_party="Husband",
        filing_date="2024-07-01",
    )
    assert c3.case_id.startswith("CASE-FAM-")
    assert c3.case_type == "Family"

    # Test creating a revenue case
    c4 = create_case(
        title="Farmer vs. Collector",
        case_type="Revenue",
        case_number="REV 202/2024",
        court="Revenue Tribunal",
        client_name="Farmer",
        client_role="Applicant",
        opposite_party="Collector",
        filing_date="2024-08-01",
    )
    assert c4.case_id.startswith("CASE-REV-")
    assert c4.case_type == "Revenue"

    # Test retrieve
    fetched = get_case(c1.case_id)
    assert fetched is not None
    assert fetched.case_number == "RCS 101/2024"

    # Test format context
    ctx = format_case_context_for_llm(c1)
    assert "ACTIVE CASE DOSSIER" in ctx
    assert "Civil" in ctx
    assert "Eviction suit" in ctx

    # Clean up test cases
    for cid in [c1.case_id, c2.case_id, c3.case_id, c4.case_id]:
        delete_case(cid)


if __name__ == "__main__":
    print("Running test_case_creation_and_types...")
    test_case_creation_and_types()
    print("[OK] Case management tests passed successfully!")

