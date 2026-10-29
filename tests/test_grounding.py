"""Citation audit rules (major vs minor, honest refusals, disclosed general knowledge) and passage selection
(complete provisions, old/new criminal-code counterparts). Offline."""
import pytest

from nyaya import agent, db, ingest, llm, retrieval
from nyaya.retrieval import Hit


def _hit(n=1, text="Section 138. Dishonour of cheque for insufficiency of funds in the account.", label="Section 138"):
    return Hit(chunk_id=n, doc_id="d1", title="The Negotiable Instruments Act, 1881", collection="bare_act",
               doc_type="statute", header=f"NI Act | {label}", text=text, label=label, section=label.split()[-1],
               page_start=None, page_end=None)


def _audit(monkeypatch, answer, verdict, question="q"):
    monkeypatch.setattr(llm, "chat_json", lambda m, info=None, **k: dict(verdict))
    meta = {}
    list(agent._audit(question, [_hit()], [answer], meta, question))
    return meta["verify"]


def test_minor_wording_issues_do_not_fail_the_audit(monkeypatch):
    v = _audit(monkeypatch, "**Short answer** — Dishonour of a cheque is an offence [1].",
               {"grounded": False, "unsupported": [{"claim": "says 'is an offence' instead of quoting", "severity": "minor"}]})
    assert v["grounded"] is True and v["unsupported"] == [] and v["minor"]


def test_major_issue_fails_the_audit(monkeypatch):
    v = _audit(monkeypatch, "**Short answer** — The punishment is seven years [1].",
               {"grounded": False, "unsupported": [{"claim": "Seven years is not in source 1", "severity": "major"}]})
    assert v["grounded"] is False and v["unsupported"] == ["Seven years is not in source 1"]


def test_old_string_format_counts_as_major(monkeypatch):
    v = _audit(monkeypatch, "Cheques [1].", {"grounded": False, "unsupported": ["unsupported date"]})
    assert v["grounded"] is False and v["unsupported"] == ["unsupported date"]


def test_honest_refusal_is_grounded(monkeypatch):
    v = _audit(monkeypatch, "The provided sources do not contain the text of Section 999 of the Indian Contract Act.",
               {"grounded": True, "unsupported": []},
               question="What does Section 999 of the Indian Contract Act say?")
    assert v["grounded"] is True and "No legal claims" in v["note"]


def test_uncited_assertion_still_fails(monkeypatch):
    v = _audit(monkeypatch, "Cheque bounce is punishable with two years.", {"grounded": True, "unsupported": []})
    assert v["grounded"] is False and v["unsupported"] == ["The answer contains no citations."]


def test_disclosed_general_knowledge_is_not_a_fabrication(monkeypatch):
    answer = ("**Short answer** — Dishonour is an offence [1].\n**Caveats and next steps**\n"
              "- Not in the provided sources: Article 226 writ remedies may also be available.")
    assert _audit(monkeypatch, answer, {"grounded": True, "unsupported": []})["grounded"] is True
    # …but the same provision asserted as law in the analysis is still caught
    v = _audit(monkeypatch, "**Analysis** — Article 226 applies [1].", {"grounded": True, "unsupported": []})
    assert v["grounded"] is False and any("Article 226" in u for u in v["unsupported"])


BNSS_482 = ("The Bharatiya Nagarik Suraksha Sanhita, 2023 Section 482\nBNSS | Section 482\n482. Direction for grant of "
            "bail to person apprehending arrest.—(4) Nothing in this section shall apply to any case involving the "
            "arrest of any person on accusation of having committed an offence under section 65 and sub-section (2) "
            "of section 70 of the\nBharatiya Nyaya Sanhita, 2023.")   # a PDF line break, as in the real text
IPC_420 = "The Indian Penal Code, 1860 Section 420\nIPC | Section 420\n420. Cheating and dishonestly inducing delivery…"
BNS_318 = "The Bharatiya Nyaya Sanhita, 2023 Section 318\nBNS | Section 318\n318. Cheating.—(1) Whoever, by deceiving…"


def _flags(answer, sources):
    from nyaya import guardrails
    return guardrails.verify_citations(answer, sources)


def test_section_named_inside_another_acts_provision_is_supported():
    assert _flags("Anticipatory bail is excluded for section 65 and section 70 of the BNS [1].", [BNSS_482]) == []


def test_section_credited_to_the_act_named_on_the_same_line():
    answer = ("Cheating under IPC Section 420 is now in the Bharatiya Nyaya Sanhita, 2023 as Section 318; "
              "the IPC provision is repealed [1].")
    assert _flags(answer, [IPC_420, BNS_318]) == []
    assert _flags(answer, [BNS_318])   # without the IPC passage, "IPC Section 420" is rightly unsupported


def test_section_inside_a_long_statutory_list_is_supported():
    src = ("The Bharatiya Nagarik Suraksha Sanhita, 2023 Section 173\nBNSS | Section 173\n173. (1) … Provided that if "
           "the information is given by the woman against whom an offence under section 64, section 65, section 66, "
           "section 67, section 68, section 70, section 71, section 74, section 75, section 76, section 77, section 78,"
           " section 79 or section 124 of the\nBharatiya Nyaya Sanhita, 2023 is alleged to have been committed…")
    assert _flags("If the victim is a woman and the offence is under section 64 of the BNS, a woman officer "
                  "records the information [1].", [src]) == []


def test_statute_sources_carry_their_abbreviation():
    h = _hit()
    h.jurisdiction = "Central"
    h.title = "The Bharatiya Nagarik Suraksha Sanhita, 2023"
    assert agent._kind(h).endswith("(abbreviation: BNSS)")


def test_mislabelled_act_is_still_caught():
    flags = _flags("**Key provisions** — BNS Sec. 482: anticipatory bail [1].", [BNSS_482])
    assert flags and "Section 482 of the Bharatiya Nyaya Sanhita" in flags[0]


def test_section_absent_from_every_source_is_still_caught():
    assert _flags("Section 999 of the BNSS applies [1].", [BNSS_482])


def test_abbreviated_party_names_match_the_source():
    src = ["LALITA KUMARI versus GOVERNMENT OF U.P. & ORS. — Supreme Court of India | 2013 INSC 748"]
    assert _flags("See Lalita Kumari v. Govt. of U.P. [1].", src) == []


@pytest.fixture
def statute_parts(fresh_db):
    from conftest import fake_passages
    with db.system():
        for title, sec, texts in [("The Hindu Marriage Act, 1955", "13", ["13. Divorce.—(1) Any marriage may be dissolved on "
                                  "the ground that the other party has had intercourse with another person;",
                                  "(ii) has ceased to be a Hindu; (iii) has been incurably of unsound mind;",
                                  "(v) has been suffering from venereal disease in a communicable form;"])]:
            doc_id = db.new_id("doc_")
            ingest._insert_document(doc_id=doc_id, case_id=None, collection="bare_act", doc_type="statute", title=title,
                                    filename="hma.pdf", path="x", sha=doc_id, meta={})
            vecs = fake_passages(texts)
            with db.tx() as c:
                c.executemany("INSERT INTO chunks(doc_id, ord, header, text, page_start, page_end, label, section, title, "
                              "chapter, emb) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              [(doc_id, i, f"HMA | Section {sec}", t, None, None, f"Section {sec}", sec, title, None,
                                vecs[i].tobytes()) for i, t in enumerate(texts)])
                c.execute("UPDATE documents SET status='ready' WHERE id=?", (doc_id,))
    yield doc_id


def test_split_provision_is_completed(statute_parts):
    first = db.conn().execute("SELECT id FROM chunks WHERE doc_id=? AND ord=0", (statute_parts,)).fetchone()["id"]
    h = retrieval.load_hits([first])[0]
    assert "venereal" not in h.text
    retrieval.complete_sections([h])
    assert "venereal disease" in h.text and h.text.index("Divorce") < h.text.index("venereal")


def test_counterparts_are_not_added_twice():
    h = Hit(chunk_id=1, doc_id="bns", title="The Bharatiya Nyaya Sanhita, 2023", collection="bare_act", doc_type="statute",
            header="BNS | Section 103", text="Punishment for murder.", label="Section 103", section="103",
            page_start=None, page_end=None)
    assert retrieval.add_counterparts([h]) == [h]   # no IPC in this empty library: nothing invented
