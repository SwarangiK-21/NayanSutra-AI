"""Unit tests: chunking, citation parsing, limitation tool, security helpers."""
from datetime import date
from pathlib import Path

from nyaya import chunking, citations, security, tools

ACT = """THE SAMPLE CONTRACT ACT, 1872
ARRANGEMENT OF SECTIONS
SECTIONS
1. Short title.
2. Interpretation-clause.
CHAPTER VI
OF THE CONSEQUENCES OF BREACH OF CONTRACT
73. Compensation for loss or damage caused by breach of contract.
74. Compensation for breach of contract where penalty stipulated for.
THE SAMPLE CONTRACT ACT, 1872
ACT NO. 9 OF 1872
An Act to define and amend certain parts of the law relating to contracts.
PRELIMINARY
1. Short title.—This Act may be called the Sample Contract Act, 1872. It extends to the whole of India.
2. Interpretation-clause.—In this Act the following words are used in the following senses, unless a contrary
intention appears from the context.
CHAPTER VI
OF THE CONSEQUENCES OF BREACH OF CONTRACT
73. Compensation for loss or damage caused by breach of
contract.—When a contract has been broken, the party who suffers by such breach is entitled to receive
compensation for any loss or damage caused to him thereby.
[74. Compensation for breach of contract where penalty stipulated for.—When a contract has been broken, if a
sum is named in the contract as the amount to be paid in case of such breach, the party complaining is entitled
to reasonable compensation not exceeding the amount so named.
THE FIRST SCHEDULE
ORDER XXXIX
TEMPORARY INJUNCTIONS
1. Cases in which temporary injunction may be granted.—Where in any suit it is proved by affidavit that any
property in dispute is in danger of being wasted, the Court may grant a temporary injunction.
2. Injunction to restrain repetition.—In any suit for restraining the defendant from committing a breach of
contract the plaintiff may apply for a temporary injunction.
"""


def test_statute_chunking_by_provision():
    chunks = chunking.chunk_statute([ACT], "The Sample Contract Act, 1872", "Contract Act")
    keys = {c.section for c in chunks}
    assert {"S1", "S2", "S73", "S74", "O39R1", "O39R2"} <= keys
    s73 = next(c for c in chunks if c.section == "S73")
    assert s73.title.startswith("Compensation for loss or damage caused by breach")  # wrapped heading joined
    assert s73.text.startswith("When a contract has been broken")
    assert "Chapter VI" in (s73.chapter or "")
    assert "Section 73" in s73.header and "Sample Contract Act" in s73.header
    s1 = next(c for c in chunks if c.section == "S1")
    assert not s1.chapter  # table-of-contents chapter must not leak into the body
    r1 = next(c for c in chunks if c.section == "O39R1")
    assert r1.label == "Order XXXIX Rule 1"


def test_judgment_paragraph_chunking():
    paras = "\n".join(f"{i}. " + ("The appellant contends that the award is contrary to public policy. " * 6)
                      for i in range(1, 13))
    opening = ("IN THE SUPREME COURT OF INDIA\nCIVIL APPELLATE JURISDICTION\nCivil Appeal No. 1 of 2003\n"
               "Oil & Natural Gas Corporation Ltd. ... Appellant\nVersus\nSaw Pipes Ltd. ... Respondent\n")
    chunks = chunking.chunk_blocks([opening + paras],
                                   "ONGC v. Saw Pipes", "judgment")
    assert any(c.label and c.label.startswith("para") for c in chunks)
    assert all(len(c.text) <= chunking.MAX_CHARS + 50 for c in chunks)
    assert chunks[0].label == "opening"


def test_generic_chunks_have_pages_and_overlap():
    pages = [("Sentence number %d is here. " % i) * 1 for i in range(400)]
    joined = [" ".join(pages[:200]), " ".join(pages[200:])]
    chunks = chunking.chunk_generic(joined, "Doc")
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 2
    assert len(chunks) > 3


def test_citation_parsing():
    refs = citations.parse_refs("Is bail available u/s 138 NI Act and Order 39 Rule 1 CPC? Also Art. 21")
    keys = {(r.key, r.act) for r in refs}
    assert ("S138", "Negotiable Instruments Act") in keys
    assert ("O39R1", "Code of Civil Procedure") in keys
    assert ("A21", "Constitution of India") in keys
    multi = citations.parse_refs("Sections 73 and 74 of the Contract Act")
    assert {r.key for r in multi} == {"S73", "S74"}
    assert all(r.act == "Indian Contract Act" for r in multi)
    assert citations.mentioned_acts("difference between IPC and BNS") == ["Indian Penal Code", "Bharatiya Nyaya Sanhita"]
    # act named *before* the numbers, followed by another act after a separator
    before = citations.parse_refs("Indian Contract Act ss. 73, 74; Specific Relief Act; CPC Order VII")
    assert {(r.key, r.act) for r in before} == {("S73", "Indian Contract Act"), ("S74", "Indian Contract Act")}
    mixed = citations.parse_refs("BNSS Sec. 483 (CrPC Sec. 439), BNS Sec. 318 (IPC Sec. 420)")
    assert ("S483", "Bharatiya Nagarik Suraksha Sanhita") in {(r.key, r.act) for r in mixed}
    assert ("S439", "Code of Criminal Procedure") in {(r.key, r.act) for r in mixed}
    assert ("S420", "Indian Penal Code") in {(r.key, r.act) for r in mixed}
    assert citations.parse_refs("what does section 12 say")[0].act is None  # not trusted for exact lookup


def test_alternative_citation_format_is_normalised():
    from nyaya.agent import normalise_citations
    assert normalise_citations("held so【5†L1-L4】 and 【12】") == "held so[5] and [12]"


def test_limitation_tool():
    r = tools.limitation("breach of contract", "2024-03-15", today=date(2025, 1, 1))
    assert r["deadline"] == "2027-03-15" and "Art. 55" in r["basis"] and r["days_left"] > 0
    r2 = tools.limitation("cheque_dishonour_notice", "2024-01-31", today=date(2024, 3, 15))
    assert r2["deadline"] == "2024-03-01" and r2["days_left"] < 0
    assert tools.days_between("2024-01-01", "2024-01-31") == 30


def test_security_helpers(tmp_path: Path):
    assert security.safe_filename("../../etc/passwd") == "passwd"
    assert security.safe_filename("C:\\x\\Bail <script>.PDF") == "Bail _script_.pdf"
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7\n...")
    exe = tmp_path / "b.pdf"
    exe.write_bytes(b"MZ\x90\x00binary")
    assert security.sniff_ok(pdf, ".pdf") and not security.sniff_ok(exe, ".pdf")
    txt = tmp_path / "c.txt"
    txt.write_bytes(b"plain text")
    assert security.sniff_ok(txt, ".txt") and not security.sniff_ok(exe, ".txt")
    from nyaya import sessions
    csrf = sessions.csrf_for("token-a")
    assert sessions.csrf_ok("token-a", csrf) and not sessions.csrf_ok("token-b", csrf) and not sessions.csrf_ok(None, csrf)
    assert not sessions.csrf_ok("token-a", None) and not sessions.csrf_ok("token-a", csrf[:-1] + ("1" if csrf[-1] == "0" else "0"))
    assert security.host_allowed("127.0.0.1:8000") and security.host_allowed("localhost")
    assert not security.host_allowed("evil.example.com") and not security.host_allowed(None)
    assert security.origin_allowed("http://127.0.0.1:8000", "127.0.0.1:8000")
    assert not security.origin_allowed("https://evil.example", "127.0.0.1:8000")
