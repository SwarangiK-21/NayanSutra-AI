"""Supreme Court judgments: catalogue parsing and search, on-demand full text, the cap, and safety checks. Offline."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from nyaya import agent, config, db, ingest, jobs, judgments, llm, retrieval
from nyaya.server import app

RAW = ("<select id='language7'><option>English</option></select><br><div class='modal fade'><div><div><div>"
       "disclaimer text</div></div></div></div><button type='button' aria-label=' A versus B pdf'><font size='4'> "
       "<strong>MOHD. AJMAL AMIR KASAB @ ABU MUJAHID<span class='fst-italic'> versus </span>STATE OF MAHARASHTRA "
       "</strong>- <span class='escrText'>[2012] 9 S.C.R. 1</span><span class='ncDisplay'>2012 INSC 341</span>"
       "<input type='hidden' id='cnr' value=ESCR010001232012></button></font><br><strong>Coram : AFTAB ALAM"
       "<sup class='tooltip-sup' data-tooltip='Author'>*</sup>, CHANDRAMAULI KR. PRASAD</strong><br> HEADNOTES "
       "Terrorist attack on Mumbai – waging war against the Government of India – death sentence confirmed."
       "<br><strong class='caseDetailsTD'><span> Decision Date :</span><font color='green'> 29-08-2012</font>"
       "<span> | Case No :</span><font color='green'> CRIMINAL APPEAL No. 1899/2011</font><span> | Disposal "
       "Nature :</span><font color='green'> Appeal(s) dismissed</font> <span> | Bench :</span><font color='green'>"
       " 2 Judges</font></strong>")
REC = {"raw_html": RAW, "path": "2012_9_1_120", "nc_display": "2012INSC341"}


def test_parse_record_extracts_all_fields():
    j = judgments.parse_record(REC, 2012)
    assert j["title"] == "MOHD. AJMAL AMIR KASAB @ ABU MUJAHID versus STATE OF MAHARASHTRA"
    assert j["petitioner"].startswith("MOHD. AJMAL") and j["respondent"] == "STATE OF MAHARASHTRA"
    assert (j["scr"], j["neutral"], j["cnr"]) == ("[2012] 9 S.C.R. 1", "2012 INSC 341", "ESCR010001232012")
    assert j["author"] == "AFTAB ALAM" and "CHANDRAMAULI" in j["coram"]
    assert (j["decided"], j["disposal"], j["bench"]) == ("2012-08-29", "Appeal(s) dismissed", "2 Judges")
    assert "waging war" in j["headnote"] and "disclaimer" not in j["headnote"]
    header, text, label = judgments.passage(j)
    assert "2012 INSC 341" in header and "decided 2012-08-29" in header and label == "2012 INSC 341"
    assert "Headnote summary" in text and "CNR: ESCR010001232012" in text


@pytest.mark.parametrize("rec", [{"raw_html": "", "path": "x_1"}, {"raw_html": RAW, "path": "../etc/passwd"},
                                 {"raw_html": "<p>nothing</p>", "path": "2012_1"}])
def test_unusable_records_are_skipped(rec):
    assert judgments.parse_record(rec, 2012) is None


def test_pdf_url_and_host_allowlist():
    assert judgments.pdf_url("2012_9_1_120", 2012) == (
        "https://indian-supreme-court-judgments.s3.ap-south-1.amazonaws.com/data/pdf/year=2012/english/2012_9_1_120_EN.pdf")
    with pytest.raises(judgments.JudgmentError):
        judgments._get(httpx.Client(), "https://evil.example.com/x.pdf")


@pytest.fixture
def catalogue(fresh_db):
    judgments._cat_id.clear()
    with db.system():
        items = [judgments.parse_record(REC, 2012)]
        other = dict(judgments.parse_record(REC, 2012), path="1985_3_985_990", title="GOVIND SARAN versus COMMISSIONER OF SALES TAX",
                     neutral="1985 INSC 107", headnote="Central Sales Tax Act – goods of special importance.")
        maneka = dict(judgments.parse_record(REC, 1978), path="1978_2_621_734", title="MANEKA GANDHI versus UNION OF INDIA AND ANR.",
                      neutral="1978 INSC 16", decided="1978-01-25", headnote="Passports Act – personal liberty – Article 21.")
        assert judgments.add_to_catalogue(items + [other, maneka]) == 3
        assert judgments.add_to_catalogue(items) == 0          # resumable: never added twice
    yield
    judgments._cat_id.clear()


def test_catalogue_is_searchable_as_a_judgment(catalogue):
    hits, _ = retrieval.search("Kasab terrorist attack Mumbai death sentence", scope="library")
    top = hits[0]
    assert top.collection == "judgment" and top.doc_type == "catalogue" and top.section == "2012_9_1_120"
    assert top.title.startswith("MOHD. AJMAL AMIR KASAB")   # the case, not "…judgments catalogue"
    assert "JUDGMENT SUMMARY" in agent._kind(top)
    item = agent._source_items([top])[0]
    assert item["catalogue"] and item["dataset_path"] == "2012_9_1_120" and item["full_text"] is None
    assert item["url"].endswith("/year=2012/english/2012_9_1_120_EN.pdf")


def test_catalogue_allows_several_judgments_per_answer(catalogue):
    hits, _ = retrieval.search("Supreme Court judgment", scope="library")
    assert sum(h.doc_type == "catalogue" for h in hits) == 3   # all entries, not capped at one document's share


def _cases_check(answer):
    from nyaya import guardrails
    return guardrails.verify_citations(answer, ["Section 103 of the Bharatiya Nyaya Sanhita"],
                                       case_lookup=judgments.describe_case)


def test_real_case_not_in_sources_is_identified(catalogue):
    issues = _cases_check("See Kasab v. State of Maharashtra (2012) on the death sentence.")
    assert any("real Supreme Court judgment (2012 INSC 341, decided 2012-08-29)" in i for i in issues)


def test_wrong_year_is_flagged(catalogue):
    issues = _cases_check("As held in Kasab v. State of Maharashtra (2015), the sentence stands.")
    assert any("but not from 2015" in i for i in issues)


def test_invented_case_is_flagged(catalogue):
    issues = _cases_check("In Ramesh Kumarswamy v. Union of Atlantis (2012) the court held otherwise.")
    assert any("not found among the Supreme Court judgments" in i for i in issues)


def test_neutral_and_scr_citations_are_checked(catalogue):
    issues = _cases_check("The rule was stated in 2012 INSC 341 and in [2012] 9 S.C.R. 1; contrast 2012 INSC 999.")
    assert sum("real Supreme Court judgment (MOHD. AJMAL" in i for i in issues) == 2
    assert any("2012 INSC 999" in i and "has this citation" in i for i in issues)


def test_union_of_india_cases_are_looked_up(catalogue):
    issues = _cases_check("Maneka Gandhi v. Union of India (1978) widened personal liberty.")
    assert any("real Supreme Court judgment (1978 INSC 16, decided 1978-01-25)" in i for i in issues)
    issues = _cases_check("Maneka Gandhi v. Union of India (1985) widened personal liberty.")
    assert any("but not from 1985" in i for i in issues)


def test_generic_names_and_other_reporters_are_not_guessed(catalogue):
    issues = _cases_check("State v. Union of India and (2010) 5 SCC 1 are discussed.")
    assert issues and not any("register" in i or "real Supreme Court" in i for i in issues)


def test_case_in_sources_is_not_flagged(catalogue):
    from nyaya import guardrails
    issues = guardrails.verify_citations("Kasab v. State of Maharashtra (2012) [1]",
                                         ["MOHD. AJMAL AMIR KASAB versus STATE OF MAHARASHTRA"],
                                         case_lookup=judgments.describe_case)
    assert issues == []


def _fake_pdf_client(monkeypatch, body=b"%PDF-1.4 fake"):
    def handler(request):
        assert request.url.host == judgments.HOST
        return httpx.Response(200, content=body)
    monkeypatch.setattr(judgments, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


processed: list[str] = []


def _fake_process(doc_id, **kw):
    """Stands in for parsing + indexing the PDF: marks the document ready."""
    processed.append(doc_id)
    with db.tx() as c:
        c.execute("UPDATE documents SET status='ready' WHERE id=?", (doc_id,))
    return dict(db.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def test_fetch_full_text_registers_and_indexes(catalogue, monkeypatch, tmp_path):
    _fake_pdf_client(monkeypatch)
    monkeypatch.setattr(config, "JUDGMENTS_DIR", tmp_path)
    monkeypatch.setattr(ingest, "process", _fake_process)
    doc = judgments.fetch_full_text("2012_9_1_120")
    assert doc["status"] == "ready" and doc["collection"] == "judgment" and processed
    assert judgments.full_text_doc("2012_9_1_120")["status"] == "ready"
    meta = json.loads(db.conn().execute("SELECT meta FROM documents WHERE id=?", (doc["id"],)).fetchone()["meta"])
    assert meta["source"] == "sc_dataset" and "CC-BY-4.0" in meta["attribution"]
    assert judgments.fetch_full_text("2012_9_1_120")["id"] == doc["id"]   # second request reuses it


def test_non_pdf_download_is_refused(catalogue, monkeypatch, tmp_path):
    _fake_pdf_client(monkeypatch, body=b"<html>not a pdf</html>")
    monkeypatch.setattr(config, "JUDGMENTS_DIR", tmp_path)
    with pytest.raises(judgments.JudgmentError):
        judgments.fetch_full_text("2012_9_1_120")


def test_cap_removes_least_recently_used(catalogue, monkeypatch, tmp_path):
    _fake_pdf_client(monkeypatch)
    monkeypatch.setattr(config, "JUDGMENTS_DIR", tmp_path)
    monkeypatch.setattr(ingest, "process", _fake_process)
    monkeypatch.setattr(config, "SC_FULLTEXT_MAX", 5)
    judgments.fetch_full_text("2012_9_1_120")
    judgments.fetch_full_text("1985_3_985_990")
    monkeypatch.setattr(config, "SC_FULLTEXT_MAX", 1)
    old = judgments.full_text_doc("2012_9_1_120")["id"]
    judgments.touch(judgments.full_text_doc("1985_3_985_990")["id"])   # used more recently
    with db.system():
        assert judgments.enforce_cap() == 1
    assert judgments.full_text_doc("2012_9_1_120") is None and judgments.full_text_doc("1985_3_985_990")
    assert db.conn().execute("SELECT COUNT(*) AS n FROM documents WHERE id=?", (old,)).fetchone()["n"] == 0


def test_queue_fetch_validates_and_dedupes(catalogue, monkeypatch):
    sent = []
    monkeypatch.setattr(jobs, "submit", lambda kind, params, **kw: sent.append((kind, params, kw)) or {})
    assert judgments.queue_fetch(["../x", "not_in_catalogue", "2012_9_1_120"]) == ["2012_9_1_120"]
    assert sent[0][0] == "judgment_fetch" and sent[0][2]["key"] == "judgment:2012_9_1_120" and sent[0][2]["owner"] is None


def test_answer_citing_a_catalogue_entry_queues_its_full_text(catalogue, monkeypatch):
    sent = []
    monkeypatch.setattr(jobs, "submit", lambda kind, params, **kw: sent.append(params["path"]) or {})
    monkeypatch.setattr(llm, "chat_json", lambda m, info=None, **k: (info or {}).update(ok=True) or (
        {"grounded": True, "unsupported": []} if "audit" in m[-1]["content"].lower()
        else {"intent": "research", "query": "Kasab death sentence terrorist attack"}))
    monkeypatch.setattr(llm, "stream", lambda m, **k: iter(["Kasab's death sentence was confirmed [1]."]))
    agent.ensure_chat = agent.ensure_chat
    with db.system():
        ev = list(agent.run(message="Kasab death sentence terrorist attack", scope="library"))
    meta = next(e for e in ev if e["type"] == "done")["meta"]
    assert sent == ["2012_9_1_120"] and meta["judgments_queued"] == ["2012_9_1_120"]


def test_fetch_endpoint(catalogue, monkeypatch):
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: {})
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        c.headers.update({"X-Nyaya-CSRF": c.get("/api/session").json()["csrf"]})
        assert c.post("/api/judgments/fetch", json={"path": "9999_1_1_1"}).status_code == 404
        assert c.post("/api/judgments/fetch", json={"path": "../../x"}).status_code == 422
        assert c.post("/api/judgments/fetch", json={"path": "2012_9_1_120"}).json()["status"] == "queued"
