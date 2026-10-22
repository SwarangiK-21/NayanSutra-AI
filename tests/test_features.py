"""Offline tests for the feature expansion (Modules 1–16).

All records here are SYNTHETIC test data (fictional parties, numbers and FIRs) — not real court or police
records. External services are replaced by clearly identified mocks: httpx.MockTransport for feeds/APIs and
the fake LLM from conftest. No test claims that a real integration was exercised.
"""
import io
import json
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from nyaya import (cases, casesearch, config, db, docintel, drafting, export, ingest, integrations, langdetect, library,
                   llm, ocr, provisions, reference)
from nyaya.server import app

SYNTH = "[SYNTHETIC TEST RECORD]"


@pytest.fixture
def client(fresh_db):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        s = c.get("/api/session").json()
        c.headers.update({"X-Nyaya-CSRF": s["csrf"]})
        yield c


def _wait(client, doc_id, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        d = client.get(f"/api/documents/{doc_id}").json()
        if d["status"] != "processing":
            return d
        time.sleep(0.1)
    raise AssertionError("ingestion timed out")


def _case(client, **kw):
    body = {"title": f"{SYNTH} Matter", "case_type": "Civil", "client_name": "Asha Rao", **kw}
    r = client.post("/api/cases", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _upload(client, case_id, name, data: bytes, mime="text/plain"):
    r = client.post(f"/api/cases/{case_id}/documents", files={"file": (name, data, mime)}, data={"doc_type": "auto"})
    assert r.status_code == 200, r.text
    return _wait(client, r.json()["id"])


# ── Module 7: case form, roles, conditional FIR fields, drafts ───────────────
def test_party_roles_depend_on_category():
    civil, crim, cons = (reference.roles_for(c) for c in ("Civil", "Criminal", "Consumer"))
    assert civil["client"][0] == "Plaintiff" and "Defendant" in civil["opposite"]["Plaintiff"]
    assert "Accused" in crim["opposite"]["Complainant"] and "Plaintiff" not in crim["client"]
    assert cons["opposite"]["Complainant"] == ["Opposite Party"]
    assert reference.needs_police_station("Criminal") and not reference.needs_police_station("Civil")
    assert reference.needs_police_station("Civil", fir_number="12/2024")


def test_case_validation_and_draft(client):
    r = client.post("/api/cases", json={"case_type": "Criminal", "client_name": "X"})
    assert r.status_code == 400 and r.json()["field"] == "client_name" and r.json()["code"] == "invalid_name"
    r = client.post("/api/cases", json={"title": "t", "client_name": "Ravi Kumar", "cnr_number": "MH12"})
    assert r.json()["code"] == "invalid_cnr"
    r = client.post("/api/cases", json={"title": "t", "client_name": "Ravi Kumar", "state": "Maharashtra", "district": "Atlantis"})
    assert r.json()["code"] == "invalid_district"
    # incomplete matters can be saved as drafts and completed later
    d = client.post("/api/cases", json={"opposite_party": "Sunil Patil", "is_draft": True}).json()
    assert d["is_draft"] == 1 and d["status"] == "Draft / Pre-filing"
    r = client.patch(f"/api/cases/{d['id']}", json={"is_draft": False})
    assert r.status_code == 400 and r.json()["field"] == "client_name"           # completing requires the client
    ok = client.patch(f"/api/cases/{d['id']}", json={"is_draft": False, "client_name": "Meera Joshi",
                                                     "cnr_number": "mhpu-0100-1234-2023", "filing_year": "2023",
                                                     "fir_number": "45/2023", "police_station": "Kothrud"}).json()
    assert ok["is_draft"] == 0 and ok["cnr_number"] == "MHPU010012342023" and ok["title"] == "Meera Joshi v. Sunil Patil"
    ref = client.get("/api/reference").json()
    assert "Maharashtra" in ref["districts"] and "Consumer" in ref["categories"]


# ── Module 4: language preference ────────────────────────────────────────────
def test_language_preference_persists(client):
    assert client.get("/api/prefs").json()["lang"] == "en"
    assert client.put("/api/prefs", json={"values": {"lang": "hi"}}).json()["lang"] == "hi"
    assert client.get("/api/prefs").json()["lang"] == "hi"
    assert client.put("/api/prefs", json={"values": {"lang": "fr"}}).status_code == 400
    assert client.put("/api/prefs", json={"values": {"evil": "x"}}).status_code == 400


def test_chat_language_rule_keeps_legal_text(client, offline):
    from nyaya import prompts
    rule = prompts.language_rule("hi")
    assert "Hindi" in rule and "Do NOT translate statutory text" in rule
    assert prompts.language_rule("en") == ""
    seen = {}
    real_stream = offline.stream

    def spy(messages, **kw):
        seen["system"] = messages[0]["content"]
        yield from real_stream(messages, **kw)
    llm.stream = spy
    c = _case(client)
    _upload(client, c["id"], "lease.txt", b"1. The security deposit is refundable within thirty days.\n" * 3)
    client.post("/api/chat", json={"message": "Is the deposit refundable?", "case_id": c["id"], "scope": "case", "lang": "hi"})
    assert "LANGUAGE: Write your reply in Hindi" in seen["system"]


# ── Module 1: case search ────────────────────────────────────────────────────
def test_case_search_identifiers(client):
    c = _case(client, cnr_number="MHPU010012342023", case_number="RCS 12/2023", filing_year=2023)
    r = client.post("/api/casesearch", json={"cnr_number": "mhpu010012342023"}).json()
    assert r["ok"] and r["jurisdiction"]["state"] == "Maharashtra" and r["jurisdiction"]["inferred"]
    assert [x["id"] for x in r["local_cases"]] == [c["id"]]
    assert r["official_sources"][0]["source"] == "ecourts" and r["official_sources"][0]["access"] == "manual"
    assert r["automated_retrieval"] is False
    # party-name search finds the saved case but asks for court/year to search officially
    r = client.post("/api/casesearch", json={"petitioner": "Asha"}).json()
    assert r["ok"] and r["local_cases"] and any(n["for"] == "party_name" for n in r["needs"])
    # Supreme Court case number routes to the SCI portal
    r = client.post("/api/casesearch", json={"case_number": "1234", "court": "Supreme Court of India", "filing_year": "2024"}).json()
    assert r["official_sources"][0]["source"] == "sci"


def test_case_search_invalid_and_incomplete(client):
    r = client.post("/api/casesearch", json={}).json()
    assert not r["ok"] and r["errors"][0]["code"] == "need_identifier"
    r = client.post("/api/casesearch", json={"cnr_number": "ABC"}).json()
    assert r["errors"][0]["code"] == "invalid_cnr"
    # An FIR number alone identifies nothing: state, district, police station and year are requested
    r = client.post("/api/casesearch", json={"fir_number": "101/2024"}).json()
    need = next(n for n in r["needs"] if n["for"] == "fir_number")
    assert set(need["fields"]) == {"state", "district", "police_station", "filing_year"}
    assert r["local_cases"] == [] and r["imported_records"] == []      # no record is claimed to exist
    # a different year is a different FIR
    _case(client, case_type="Criminal", fir_number="101/2024", police_station="Kothrud", filing_year=2023)
    r = client.post("/api/casesearch", json={"fir_number": "101/2024", "filing_year": "2024", "state": "Maharashtra",
                                             "district": "Pune", "police_station": "Kothrud"}).json()
    assert r["local_cases"] == []
    assert any(s["source"] == "mh_police_fir" for s in r["official_sources"])


def test_record_import_duplicates_and_conflicts(client):
    base = {"source": "ecourts", "cnr_number": "MHPU010099992022", "petitioner": "Asha Rao", "respondent": "Synthetic Builders",
            "court": "Civil Judge (Senior Division)", "status": "Pending", "next_hearing_date": "2026-11-20"}
    r1 = client.post("/api/records/import", json=base).json()
    assert r1["result"] == "new" and r1["record"]["provenance"] == "user_import"
    assert "next_hearing_date" in r1["record"]["data"]["unverified_fields"]
    r2 = client.post("/api/records/import", json=base).json()
    assert r2["result"] == "unchanged" and r2["changes"] == []                      # duplicate prevention
    case = client.post(f"/api/records/{r1['record']['id']}/save-case").json()
    assert case["cnr_number"] == "MHPU010099992022" and case["is_draft"] == 1
    r3 = client.post("/api/records/import", json={**base, "next_hearing_date": "2026-12-05", "status": "Evidence"}).json()
    fields = {c["field"] for c in r3["changes"]}
    assert r3["result"] == "updated" and fields == {"next_hearing_date", "status"}  # conflicting values detected
    assert client.get(f"/api/cases/{case['id']}").json()["next_hearing_date"] == "2026-12-05"
    notes = client.get("/api/notifications").json()["items"]
    assert any(n["kind"] == "case_update" for n in notes)
    assert client.post("/api/records/import", json={**base, "source": "livelaw"}).status_code == 400


# ── Modules 2 & 12: integrations, sync, retries (MOCKED HTTP) ────────────────
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Synthetic headline one</title><link>https://www.livelaw.in/a-1</link><guid>g1</guid><pubDate>Thu, 22 Oct 2026 06:31:05 GMT</pubDate></item>
<item><title>Synthetic headline two</title><link>https://www.livelaw.in/a-2</link><guid>g2</guid></item>
<item><title>Off-site link is dropped</title><link>https://evil.example.com/x</link><guid>g3</guid></item>
</channel></rss>"""


@pytest.fixture
def mock_http(monkeypatch):
    monkeypatch.setattr(integrations, "HOST_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(integrations.time, "sleep", lambda s: None)
    calls = []

    def install(handler):
        def factory():
            return httpx.Client(transport=httpx.MockTransport(lambda req: (calls.append(req), handler(req))[1]))
        monkeypatch.setattr(integrations, "client_factory", factory)
        return calls
    return install


def test_feed_sync_dedupe_and_conditional_get(fresh_db, mock_http):
    calls = mock_http(lambda req: httpx.Response(304) if req.headers.get("if-none-match") == '"v1"'
                      else httpx.Response(200, content=RSS, headers={"etag": '"v1"'}))
    r = integrations.run_sync("livelaw", "manual")
    assert r["ok"] and r["new"] == 2                                   # off-site item rejected
    assert integrations.run_sync("livelaw", "scheduled")["outcome"] == "not_modified"
    assert calls[-1].headers["if-none-match"] == '"v1"'
    st = next(s for s in integrations.status() if s["id"] == "livelaw")
    assert st["status"] == "ok" and st["records"] == 2 and st["mode"] == "periodic" and st["last_success_at"]
    assert len(integrations.news()) == 2


def test_sync_retry_then_success(fresh_db, mock_http):
    seq = iter([httpx.Response(503), httpx.Response(200, content=RSS)])
    mock_http(lambda req: next(seq))
    assert integrations.run_sync("livelaw")["ok"]


def test_failed_integration_backoff_and_log(fresh_db, mock_http):
    def boom(req):
        raise httpx.ConnectError("offline")
    mock_http(boom)
    r = integrations.run_sync("livelaw")
    assert not r["ok"] and r["retry_in_minutes"] == 5
    st = next(s for s in integrations.status() if s["id"] == "livelaw")
    assert st["status"] == "error" and "ConnectError" in st["last_error"] and st["log"][0]["outcome"] == "error"
    assert integrations.run_sync("livelaw")["retry_in_minutes"] == 10      # exponential back-off
    integrations.run_sync("livelaw")
    assert any("failing" in n["title"] for n in [dict(x) for x in db.conn().execute("SELECT * FROM notifications")])
    with pytest.raises(ValueError):
        integrations.run_sync("ecourts")                                   # manual sources are never "synced"
    assert next(s for s in integrations.status() if s["id"] == "ecourts")["mode"] == "manual"


def test_manual_refresh_cooldown(fresh_db, mock_http):
    mock_http(lambda req: httpx.Response(200, content=RSS))
    integrations.run_sync("livelaw", "manual")
    with pytest.raises(ValueError):
        integrations.run_sync("livelaw", "manual")


def test_hostile_feed_is_rejected(fresh_db, mock_http):
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    mock_http(lambda req: httpx.Response(200, content=bomb))
    r = integrations.run_sync("livelaw")
    assert not r["ok"] and "not valid RSS" in r["error"]


# ── Module 5: multilingual documents, OCR ────────────────────────────────────
HI = "यह एक किरायानामा है। किरायेदार को हर महीने किराया देना होगा और जमा राशि वापस की जाएगी। यह करार पुणे में किया गया है और दोनों पक्षों ने इसे पढ़ा है।"
MR = "हा भाडेकरार आहे. भाडेकरूने दरमहा भाडे द्यावे आणि ठेव रक्कम परत केली जाईल. हा करार पुणे येथे करण्यात आला आहे आणि दोन्ही पक्षांनी तो वाचला आहे. मालमत्तेचे क्षेत्रफळ नाही."


def test_language_detection():
    assert langdetect.detect("The tenant shall pay rent every month.")["lang"] == "en"
    assert langdetect.detect(HI)["lang"] == "hi"
    assert langdetect.detect(MR)["lang"] == "mr"
    assert langdetect.detect("आहे")["confidence"] == "low"                # too little evidence is flagged


def test_hindi_marathi_english_uploads(client):
    c = _case(client)
    for name, text, lang in (("hi.txt", HI * 3, "hi"), ("mr.txt", MR * 3, "mr"), ("en.txt", "The tenant shall pay rent. " * 20, "en")):
        d = _upload(client, c["id"], name, text.encode("utf-8"))
        assert d["status"] == "ready" and d["meta"]["lang"] == lang, (name, d)
        intel = client.get(f"/api/documents/{d['id']}/intel").json()
        assert intel["pages"] and intel["language"]["lang"] == lang       # original extracted text preserved


def test_image_upload_without_ocr_fails_clearly(client, monkeypatch):
    monkeypatch.setattr(ocr, "engine", lambda: None)
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, "PNG")
    c = _case(client)
    d = _upload(client, c["id"], "scan.png", buf.getvalue(), "image/png")
    assert d["status"] == "failed" and "OCR" in d["error"]
    bad = client.post(f"/api/cases/{c['id']}/documents", files={"file": ("x.png", b"not a png", "image/png")})
    assert bad.status_code == 415


def test_ocr_low_confidence_words_are_marked():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t95\tकिराया\n5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t31\tरक्कम\n")
    text, conf, words, low = ocr._tsv_to_text(tsv)
    assert text == "किराया [?रक्कम]" and words == 2 and low == 1


def test_translation_marks_uncertainty_and_indexes_english(client, monkeypatch):
    c = _case(client)
    d = _upload(client, c["id"], "hi.txt", (HI * 3).encode("utf-8"))
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "[p. 1]\nThis is a rent agreement [uncertain translation]. The tenant pays rent.")
    res = docintel.translate(d["id"])
    assert res["uncertain_marks"] == 1
    intel = docintel.overview(d["id"])
    assert intel["derivatives"]["translation_en"]["content"][0].startswith("This is a rent agreement")
    derived = db.conn().execute("SELECT id FROM documents WHERE json_extract(meta,'$.derived_from')=?", (d["id"],)).fetchall()
    assert len(derived) == 1                                             # English translation is searchable
    listed = client.get(f"/api/documents?case_id={c['id']}").json()["documents"]
    assert [x["id"] for x in listed] == [d["id"]]                        # but not listed as a separate upload
    assert client.post(f"/api/documents/{d['id']}/translate", json={}).status_code == 400   # consent required


def test_summary_verifies_values_against_text(client, monkeypatch):
    c = _case(client)
    d = _upload(client, c["id"], "notice.txt", b"LEGAL NOTICE\nCase No. RCS 77/2024 between Asha Rao and Synthetic Builders under Section 138.\n" * 3)
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {
        "document_type": "legal notice", "summary": "s", "parties": [{"name": "Asha Rao", "role": "sender", "page": 1},
                                                                    {"name": "Invented Person", "role": "x", "page": 1}],
        "case_numbers": [{"value": "RCS 77/2024", "page": 1}], "legal_provisions": [{"reference": "Section 420", "page": 1}],
        "dates": [{"date": "1 Jan", "event": "e", "page": 9}]})
    facts = docintel.summarize(d["id"], "en")["facts"]
    names = {p["name"]: p["verified"] for p in facts["parties"]}
    assert names == {"Asha Rao": True, "Invented Person": False}
    assert facts["case_numbers"][0]["verified"] and not facts["legal_provisions"][0]["verified"]
    assert facts["dates"][0]["page_valid"] is False                       # page 9 does not exist


# ── Library fixture for provisions / drafting ───────────────────────────────
NI = ("THE NEGOTIABLE INSTRUMENTS ACT, 1881\nARRANGEMENT OF SECTIONS\n"
      "138. Dishonour of cheque for insufficiency, etc., of funds in the account.—Where any cheque drawn by a person on an "
      "account maintained by him with a banker for payment of any amount of money is returned by the bank unpaid, such person "
      "shall be deemed to have committed an offence. Provided that nothing contained in this section shall apply unless the payee "
      "makes a demand for the payment of the said amount of money by giving a notice in writing, to the drawer of the cheque, "
      "within thirty days of the receipt of information by him from the bank regarding the return of the cheque as unpaid.\n"
      "139. Presumption in favour of holder.—It shall be presumed, unless the contrary is proved, that the holder of a cheque "
      "received the cheque for the discharge of any debt or other liability.\n")


@pytest.fixture
def ni_library(fresh_db, tmp_path):
    config.BARE_ACTS_DIR.mkdir(parents=True, exist_ok=True)
    p = config.BARE_ACTS_DIR / "ni_test.txt"
    p.write_text(NI * 2, "utf-8")
    (config.BARE_ACTS_DIR / "manifest.json").write_text(json.dumps({"ni_test.txt": {
        "title": "The Negotiable Instruments Act, 1881", "short": "NI Act", "enforcement_date": "1-3-1882",
        "item_url": "https://indiacode.gov.in/items/synthetic"}}), "utf-8")
    library._manifest.cache_clear()
    doc, _ = ingest.register(p, collection="bare_act", case_id=None, doc_type="statute",
                             title="The Negotiable Instruments Act, 1881", filename=p.name, meta={"short": "NI Act"})
    assert ingest.process(doc["id"])["status"] == "ready"
    yield doc
    library._manifest.cache_clear()


def test_library_lookup_and_temporal_flags(ni_library):
    p = library.lookup_provision("NI Act", "Section 138")
    assert p["found"] and p["label"] == "Section 138" and p["pdf_url"].endswith(f"#page={p['page'] or 1}")
    assert not library.lookup_provision("NI Act", "Section 999")["found"]
    from datetime import date
    flags = provisions.temporal_flags("The Bharatiya Nyaya Sanhita, 2023", "criminal", date(2023, 5, 1),
                                      {"enforcement_date": "1-7-2024"})
    assert any("before this Act came into force" in f and "Indian Penal Code" in f for f in flags)
    assert provisions.temporal_flags("The Bharatiya Nyaya Sanhita, 2023", "criminal", None, {})       # date unknown → flagged
    assert any("repealed" in f for f in provisions.temporal_flags("The Indian Penal Code, 1860", "criminal", date(2025, 1, 1),
                                                                     {"repealed": True, "note": "Repealed w.e.f. 1 July 2024"}))


def test_provision_suggestions_only_from_library(client, ni_library, monkeypatch):
    c = _case(client, case_type="Criminal", description=f"{SYNTH} A cheque of Rs 2 lakh given by the accused was returned unpaid.")
    answers = iter([
        {"category": "criminal", "conduct_date": "2024-02-10", "search_queries": ["dishonour of cheque returned unpaid"]},
        {"provisions": [{"cid": "C1", "why": "cheque returned unpaid", "relevance": "high"},
                        {"cid": "C99", "why": "hallucinated candidate"}]},
    ])
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: next(answers))
    res = provisions.suggest(c["id"])
    got = res["provisions"]
    assert len(got) == 1 and got[0]["verification"] == "verified_in_library" and got[0]["doc_id"] == ni_library["id"]
    pid = got[0]["id"]
    r = client.patch(f"/api/provisions/{pid}", json={"status": "accepted"}).json()
    assert r["status"] == "accepted"
    assert "NI Act" in client.get(f"/api/cases/{c['id']}").json()["acts_sections"]
    r = client.patch(f"/api/provisions/{pid}", json={"ref": "Section 999"}).json()
    assert r["verification"] == "not_found" and r["status"] == "edited"
    assert client.post(f"/api/cases/{c['id']}/provisions/suggest", json={}).status_code == 400   # consent required


# ── Modules 6, 9, 11: drafting, placeholders, export ─────────────────────────
DRAFT = ("**DRAFT — FOR REVIEW BY A QUALIFIED LEGAL PROFESSIONAL**\n\n---\n\n# LEGAL NOTICE\n\n"
         "1. My client Asha Rao ⟦F1⟧ issued cheque no. [●: cheque number] which was dishonoured ⟦F2⟧ [1].\n"
         "2. You are called upon to pay within the period stated in the proviso [1] [7] ⟦F9⟧.\n\n"
         "| Item | Detail |\n|---|---|\n| Amount | **Rs 2,00,000** |\n\nकानूनी नोटिस — हिंदी पाठ\n\n**Drafting notes**\n- Fill [●: address]")


def test_draft_generation_placeholders_and_checks(client, ni_library, monkeypatch):
    c = _case(client, case_type="Criminal")
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {
        "facts": [{"text": "The payee lent money", "origin": "user", "ref": "user input"},
                  {"text": "The cheque was returned unpaid", "origin": "case", "ref": "case dossier"}],
        "missing": [{"key": "cheque", "question": "Cheque number?", "essential": True}],
        "inconsistencies": [], "unsupported": ["claim of fraud"]})
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: DRAFT)
    d = drafting.generate(doc_type_id="notice_ni_138", forum_id="none", case_id=c["id"],
                          known={"sender_name": "Asha Rao"}, instructions="demand payment")
    ck = d["inputs"]["checks"]
    assert d["kind"] == "notice" and d["version"] == 1
    assert "[●: cheque number]" in ck["placeholders"] and ck["unknown_fact_tags"] == ["F9"]
    assert ck["invalid_citations"] and 7 in ck["invalid_citations"] and ck["labelled_for_review"]
    assert d["content"].count("FOR REVIEW") == 1                     # a Markdown-formatted label is not duplicated
    assert any(f["origin"] == "user" and "Asha Rao" in f["text"] for f in d["facts"])   # typed details become facts
    assert d["missing"][0]["essential"] and d["inputs"]["unsupported"] == ["claim of fraud"]
    assert any(s["title"].startswith("The Negotiable") for s in d["sources"])   # the s.138 text was supplied as law
    # editing creates a new version; export works in both formats, including Devanagari
    d2 = client.put(f"/api/drafts/{d['id']}", json={"content": d["content"] + "\nEdited."}).json()
    assert d2["version"] == 2 and len(d2["versions"]) == 2
    docx = client.get(f"/api/drafts/{d['id']}/export?format=docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    xml = zipfile.ZipFile(io.BytesIO(docx.content)).read("word/document.xml").decode("utf-8")
    assert "FOR REVIEW" in xml and "[●: cheque number]" in xml and "⟦F" not in xml and "कानूनी नोटिस" in xml
    pdf = client.get(f"/api/drafts/{d['id']}/export?format=pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    acts = [a["action"] for a in client.get("/api/audit").json()["items"]]
    assert "draft.export" in acts and "notice.generated" in acts


def test_draft_rejects_other_cases_documents_and_needs_consent(client, ni_library, monkeypatch):
    a, b = _case(client), _case(client)
    doc = _upload(client, a["id"], "a.txt", b"Confidential facts of case A. " * 10)
    monkeypatch.setattr(llm, "chat_json", lambda m, **k: {"facts": [], "missing": []})
    monkeypatch.setattr(llm, "chat", lambda m, **k: DRAFT)
    with pytest.raises(ValueError):                    # case B cannot pull case A's document into its draft
        drafting.generate(doc_type_id="legal_notice", forum_id="none", case_id=b["id"], doc_ids=[doc["id"]])
    r = client.post("/api/drafts/generate", json={"doc_type": "legal_notice", "forum": "none", "case_id": a["id"], "doc_ids": [doc["id"]]})
    assert r.status_code == 400                        # document text leaves the machine only with consent
    r = client.post("/api/drafts/generate", json={"doc_type": "legal_notice", "forum": "none", "case_id": a["id"],
                                                  "doc_ids": [doc["id"]], "consent": True})
    job = r.json()
    for _ in range(100):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["inputs"]["files"][0]["doc_id"] == doc["id"]


def test_forum_requirements_are_sourced(client, ni_library):
    f = client.get("/api/drafting/forums/bombay_hc").json()
    assert all(o["url"].startswith("https://") and o["verified_on"] for o in f["official"])
    assert f["confirm"]                                   # formatting rules are never assumed
    bad = client.post("/api/drafting/forums/bombay_hc/requirements", json={"text": "A4 paper", "url": "http://x", "verified_on": "2026-10-22"})
    assert bad.status_code == 422                        # requirements need an https source and a date
    ok = client.post("/api/drafting/forums/bombay_hc/requirements",
                     json={"text": "A4 paper", "url": "https://bombayhighcourt.gov.in/bhc/PracticeNotes", "verified_on": "2026-10-22"}).json()
    assert ok["requirements"][-1]["status"] == "user_supplied"


def test_export_functions_direct():
    assert export.to_docx("t", DRAFT, [])[:2] == b"PK"
    pdf = export.to_pdf("t", DRAFT, [{"n": 1, "citation": "NI Act s.138"}])
    assert pdf[:5] == b"%PDF-"
    import pypdfium2
    doc = pypdfium2.PdfDocument(pdf)
    text = "".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    assert "None" not in text and text.count("FOR REVIEW") == 1 and "LEGAL NOTICE" in text


# ── Module 10 & 13: awareness, unified search ────────────────────────────────
def test_awareness_items_have_jurisdiction_links_and_dates(client):
    res = client.get("/api/awareness?state=MH").json()
    assert res["items"] and res["disclaimer"]
    for i in res["items"]:
        assert i["jurisdiction"] and i["last_verified"]
        assert all(l["url"].startswith("https://") and l["verified_on"] for l in i["links"])
    assert {i["state"] for i in res["items"]} == {"MH", "central"}


def test_unified_search_separates_sources(client, ni_library):
    c = _case(client, description="cheque dishonour matter")
    res = client.get("/api/search?q=cheque").json()
    types = {r["type"] for r in res["results"]}
    assert {"case", "bare_act"} <= types
    assert all(r["source"] and r["url"] for r in res["results"])
    only_cases = client.get("/api/search?q=cheque&types=case").json()["results"]
    assert {r["type"] for r in only_cases} == {"case"} and only_cases[0]["url"] == f"#/cases/{c['id']}"


# ── Module 14: security & audit ──────────────────────────────────────────────
def test_permissions_headers_and_audit(client):
    r = client.get("/")
    assert "microphone=(self)" in r.headers["permissions-policy"] and "camera=()" in r.headers["permissions-policy"]
    c = _case(client)
    acts = [a["action"] for a in client.get("/api/audit").json()["items"]]
    assert "case.create" in acts
    client.delete(f"/api/cases/{c['id']}")
    assert client.get("/api/audit").json()["items"][0]["action"] == "case.delete"


def test_retention_purges_old_chats(fresh_db):
    from nyaya import server
    db.set_pref("chat_retention_days", "30")
    with db.tx() as cx:
        cx.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES('old','x','2020-01-01','2020-01-01')")
        cx.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES('new','y',?,?)", (db.now(), db.now()))
    server._apply_retention()
    ids = [r[0] for r in db.conn().execute("SELECT id FROM chats")]
    assert ids == ["new"]


def test_static_ui_has_toggle_and_accessibility_hooks(client):
    html = client.get("/").text
    assert 'id="langToggle"' in html and 'class="skip"' in html and 'type="module"' in html
    hi = client.get("/static/i18n/hi.json").json()
    en = client.get("/static/i18n/en.json").json()
    assert set(en) <= set(hi)                       # every English UI string has a Hindi translation
