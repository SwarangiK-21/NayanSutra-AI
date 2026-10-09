"""Offline tests: no API key, no model downloads."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_core.documents import Document

from ingest import chunk_documents
from retrieval import rrf
from tools import calculate_limitation_deadline, days_between


def test_section_aware_chunking():
    text = ("Preamble text. " * 60) + "\nSection 73. Compensation for loss.\n" + ("Body. " * 60) + \
           "\nSection 74. Penalty stipulated.\n" + ("More. " * 60)
    chunks = chunk_documents([Document(page_content=text, metadata={"source": "x.txt", "doc_type": "statutes"})])
    sections = {c.metadata.get("section") for c in chunks}
    assert "Section 73" in sections and "Section 74" in sections
    assert all("chunk_id" in c.metadata for c in chunks)


def test_rrf_prefers_docs_in_both_lists():
    a, b, c = (Document(page_content=t, metadata={"chunk_id": t}) for t in "abc")
    fused = rrf([[a, b], [c, b]])
    assert fused[0].metadata["chunk_id"] == "b"


def test_limitation_calculator():
    out = calculate_limitation_deadline.invoke({"cause_of_action": "breach of contract", "accrual_date": "2024-03-15"})
    assert "2027-03-15" in out and "Art. 55" in out
    assert "Unknown" in calculate_limitation_deadline.invoke({"cause_of_action": "foo", "accrual_date": "2024-01-01"})
    assert days_between.invoke({"start_date": "2024-01-01", "end_date": "2024-01-31"}) == "30"
