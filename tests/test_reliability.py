"""Part 2 — reliability tests (offline, synthetic data): durable jobs and their authorization, retries, dead letter,
cancellation, checkpoints; ingestion attempts, quarantine and quality checks; bulk upload; LLM retries, circuit breaker
and fallback; workflow trace and partial answers; draft review; library versions."""
from __future__ import annotations

import io
import json
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from nyaya import accounts, config, db, docintel, drafting, ingest, jobs, llm, parsing, quality, storage
from nyaya.server import app
from test_security import PW, fake, guest, mock_feed, new_case, person, security_limiter_reset, signed_in, upload  # noqa: F401

REAL_CHAT, REAL_STREAM = llm.chat, llm.stream   # captured before the autouse fixture replaces them
BASE = "http://127.0.0.1:8000"


def wait_job(c, job_id, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] not in ("queued", "processing"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.fixture
def local_db(fresh_db):
    """Local mode, no workers running: jobs are claimed and run synchronously by the test."""
    accounts.ensure_local_operator()
    yield


def _doc(case_id: str, text: str = "Synthetic agreement. Rent is payable monthly.\n" * 20, name: str = "a.txt") -> str:
    import tempfile
    from pathlib import Path
    with db.system():
        d = Path(tempfile.mkdtemp()) / name
        d.write_text(text, encoding="utf-8")
        ref = storage.put_file(d, case_id, db.new_id("doc_"), d.suffix, "text/plain")
        doc, _ = ingest.register(d, collection="case", case_id=case_id, doc_type=None, title=None, filename=name,
                                 stored=ref)
    return doc["id"]


def _local_case() -> str:
    from nyaya import cases
    with db.as_identity(db.Identity(user_id=accounts.LOCAL_USER_ID, org_id=db.DEFAULT_ORG, role="admin")):
        return cases.create_case({"title": "Synthetic v. Sample", "client_name": "Asha Rao", "case_type": "Civil"})["id"]


def _run_next(lane: str = "ingest") -> str | None:
    row = jobs._claim(lane)
    return jobs.run_one(row) if row else None


# ── Durable jobs ─────────────────────────────────────────────────────────────
def test_jobs_are_durable_owned_and_deduplicated(local_db):
    case_id = _local_case()
    doc_id = _doc(case_id)
    j1 = ingest.queue(doc_id, owner=accounts.LOCAL_USER_ID)
    j2 = ingest.queue(doc_id, owner=accounts.LOCAL_USER_ID)
    assert j1["id"] == j2["id"] and j1["status"] == "queued"                    # idempotent: one job per document
    with db.system():
        assert db.conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1   # stored in the database
    assert jobs.get(j1["id"], owner="someone-else") is None                     # an id alone gives no access
    assert jobs.cancel(j1["id"], owner="someone-else") is None
    assert _run_next() == "completed"
    with db.system():
        st = db.conn().execute("SELECT status, n_chunks FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert st["status"] == "ready" and st["n_chunks"] > 0
    # Running the same document again replaces its passages instead of duplicating them (idempotent processing).
    with db.system():
        before = db.conn().execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0]
        ingest.process(doc_id)
        assert db.conn().execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0] == before


def test_poison_file_is_retried_a_bounded_number_of_times_then_dead_lettered(local_db, monkeypatch):
    monkeypatch.setattr(jobs, "BACKOFF_BASE", 0.0)
    case_id = _local_case()
    doc_id = _doc(case_id)
    real_extract = parsing.extract
    monkeypatch.setattr(parsing, "extract", lambda p: (_ for _ in ()).throw(RuntimeError("parser crashed")))
    job = ingest.queue(doc_id, owner=accounts.LOCAL_USER_ID)
    outcomes = [_run_next() for _ in range(config.INGEST_MAX_ATTEMPTS + 2)]
    assert outcomes[:config.INGEST_MAX_ATTEMPTS] == ["queued"] * (config.INGEST_MAX_ATTEMPTS - 1) + ["dead"]
    assert outcomes[-1] is None                                                 # nothing left: no endless loop
    j = jobs.get(job["id"], owner=accounts.LOCAL_USER_ID)
    assert j["status"] == "dead" and j["attempts"] == config.INGEST_MAX_ATTEMPTS
    with db.system():
        d = db.conn().execute("SELECT status, error FROM documents WHERE id=?", (doc_id,)).fetchone()
        notes = [r["title"] for r in db.conn().execute("SELECT title FROM notifications WHERE user_id=?",
                                                       (accounts.LOCAL_USER_ID,))]
    assert d["status"] == "failed" and "failed repeatedly" in d["error"]       # never stuck in 'processing'
    assert any("could not be completed" in n for n in notes)                   # the owner is told
    # Retrying puts it back; with the parser fixed it now succeeds.
    monkeypatch.setattr(parsing, "extract", real_extract)   # (undo() would also drop the offline model fakes)
    jobs.retry(job["id"], accounts.LOCAL_USER_ID)
    assert _run_next() == "completed"


def test_unreadable_file_is_quarantined_not_retried(local_db):
    case_id = _local_case()
    doc_id = _doc(case_id, text="�� " * 400, name="garbled.txt")
    ingest.queue(doc_id, owner=accounts.LOCAL_USER_ID)
    assert _run_next() == "completed"
    with db.system():
        d = db.conn().execute("SELECT status, error FROM documents WHERE id=?", (doc_id,)).fetchone()
        n = db.conn().execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0]
    assert d["status"] == "quarantined" and n == 0 and d["error"]             # kept, not searchable, reason shown


def test_quality_checks_find_blank_garbled_and_missing_pages():
    pages = ["Page text " * 10, "", "�" * 120 + "abc", "More text " * 10]
    q = quality.assess(pages, {"printed_page_numbers": [(1, 1, 8), (2, 2, 8), (3, 5, 8), (4, 6, 8)]})
    assert q["empty_pages"] == [2] and q["garbled_pages"] == [3] and q["verdict"] == "warnings"
    assert q["missing_printed_pages"] == ["3–4", "7–8"]
    assert quality.assess(["", " "])["verdict"] == "quarantine"
    raw = ["Title\nbody\n1", "body\n2", "body\nPage 4 of 4"]
    assert quality.printed_numbers(raw) == [(1, 1, None), (2, 2, None), (3, 4, 4)]
    assert quality.missing_pages([(1, 1, None), (2, 12, None)], 2) == []      # too few numbers to judge


def test_cancel_a_queued_job_and_a_running_one(local_db):
    case_id = _local_case()
    doc_id = _doc(case_id)
    job = ingest.queue(doc_id, owner=accounts.LOCAL_USER_ID)
    assert jobs.cancel(job["id"], accounts.LOCAL_USER_ID)["status"] == "cancelled"
    with db.system():
        assert db.conn().execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()["status"] == "cancelled"
    assert _run_next() is None
    # A running job stops at its next progress report.
    calls = []

    @jobs.handler("test_slow", authorize=None, timeout=60)
    def slow(params, ctx):
        for i in range(5):
            calls.append(i)
            if i == 1:
                jobs.cancel(ctx.job_id, accounts.LOCAL_USER_ID)
            ctx(i, 5, "working")
        return {"done": True}
    j = jobs.submit("test_slow", {}, owner=accounts.LOCAL_USER_ID)
    assert jobs.run_one(jobs._claim("ai")) == "cancelled" and calls == [0, 1]
    assert jobs.get(j["id"], accounts.LOCAL_USER_ID)["status"] == "cancelled"


def test_timeouts_permanent_errors_and_stale_locks(local_db, monkeypatch):
    monkeypatch.setattr(jobs, "BACKOFF_BASE", 0.0)

    @jobs.handler("test_timeout", authorize=None, timeout=0, max_attempts=2)
    def too_slow(params, ctx):
        time.sleep(0.01)
        ctx(1, 2, "still working")
        return {}

    @jobs.handler("test_bad_input", authorize=None)
    def bad(params, ctx):
        raise ValueError("The document is already in English — no translation needed.")

    t = jobs.submit("test_timeout", {}, owner=accounts.LOCAL_USER_ID)
    assert jobs.run_one(jobs._claim("ai")) == "queued"                         # timed out: retried
    assert jobs.run_one(jobs._claim("ai")) == "dead"                           # bounded
    assert jobs.get(t["id"], accounts.LOCAL_USER_ID)["error"] == "The task timed out."
    b = jobs.submit("test_bad_input", {}, owner=accounts.LOCAL_USER_ID)
    assert jobs.run_one(jobs._claim("ai")) == "failed"                         # permanent: not retried
    assert "already in English" in jobs.get(b["id"], accounts.LOCAL_USER_ID)["error"]
    # A worker that died mid-job: its expired lock sends the job back to the queue.
    s = jobs.submit("test_bad_input", {}, owner=accounts.LOCAL_USER_ID)
    row = jobs._claim("ai")
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE jobs SET locked_until='2000-01-01T00:00:00' WHERE id=?", (row["id"],))
    assert jobs.recover_stale() == 1 and jobs.get(s["id"], accounts.LOCAL_USER_ID)["status"] == "queued"
    assert jobs.submit.__module__ and pytest.raises(ValueError, jobs.submit, "no_such_kind", {})


def test_translation_resumes_from_its_checkpoint(local_db, monkeypatch):
    case_id = _local_case()
    doc_id = _doc(case_id, text=("यह एक सिंथेटिक किरायानामा है। किराया हर महीने देय है। " * 80 + "\n\n") * 6, name="hi.txt")
    with db.system():
        ingest.process(doc_id)
    monkeypatch.setattr(docintel, "PART_CHARS", 3000)
    sent, fail_once = [], {"n": 0}

    def flaky_chat(messages, **kw):
        sent.append(1)
        if len(sent) == 2 and not fail_once["n"]:
            fail_once["n"] = 1
            raise llm.LLMError("Service busy", transient=True)
        return "[p. 1]\nSynthetic English text."
    monkeypatch.setattr(llm, "chat", flaky_chat)
    monkeypatch.setattr(jobs, "BACKOFF_BASE", 0.0)
    j = jobs.submit("translate", {"doc_id": doc_id}, owner=accounts.LOCAL_USER_ID, case_id=case_id)
    assert jobs.run_one(jobs._claim("ai")) == "queued"                         # failed on part 2, keeps part 1
    n_parts = len(docintel._parts(docintel.extracted_pages(doc_id)))
    assert jobs.run_one(jobs._claim("ai")) == "completed"
    assert len(sent) == n_parts + 1                                            # part 1 was not translated twice
    assert jobs.get(j["id"], accounts.LOCAL_USER_ID)["result"]["pages"] >= 1


# ── Background jobs re-check authorization when they run ─────────────────────
def test_job_stops_if_access_was_withdrawn_before_it_ran(fake, monkeypatch):
    person(fake, "adv@example.com", "advocate")
    person(fake, "helper@example.com", "advocate")
    a, h = signed_in("adv@example.com"), signed_in("helper@example.com")
    case_id = new_case(a)
    doc = upload(a, case_id)
    hid = h.get("/api/session").json()["user"]["id"]
    assert a.post(f"/api/cases/{case_id}/members", json={"user_id": hid, "access": "advocate"}).status_code == 200
    jobs.stop_workers()                                                         # keep the job queued
    sent = []
    monkeypatch.setattr(llm, "chat", lambda *x, **k: sent.append(1) or "")
    r = h.post(f"/api/documents/{doc['id']}/summarize", json={"consent": True})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert a.delete(f"/api/cases/{case_id}/members/{hid}").status_code == 200  # removed before the job runs
    assert jobs.run_one(jobs._claim("ai")) == "failed"
    j = h.get(f"/api/jobs/{r.json()['id']}").json()
    assert "withdrawn" in j["error"] and not sent                              # no model call with that document
    assert a.get(f"/api/jobs/{r.json()['id']}").status_code == 404             # not even the case owner sees it
    jobs.start_workers()


# ── Bulk upload ──────────────────────────────────────────────────────────────
def test_batch_upload_limits_progress_cancel_and_report(fake, monkeypatch):
    person(fake, "adv@example.com", "advocate")
    person(fake, "other@example.com", "advocate")
    a, o = signed_in("adv@example.com"), signed_in("other@example.com")
    case_id = new_case(a)
    monkeypatch.setattr(config, "BATCH_MAX_FILES", 5)
    assert a.post(f"/api/cases/{case_id}/batches", json={"files": [{"name": f"{i}.txt", "size": 10} for i in range(6)]}).status_code == 413
    assert a.post(f"/api/cases/{case_id}/batches",
                  json={"files": [{"name": "big.pdf", "size": config.BATCH_MAX_TOTAL_MB * 1024 * 1024 + 1}]}).status_code == 413
    files = [{"name": "one.txt", "size": 400}, {"name": "-cmd.exe", "size": 10}, {"name": "two.txt", "size": 400},
             {"name": "three.txt", "size": 400}]
    b = a.post(f"/api/cases/{case_id}/batches", json={"files": files}).json()
    assert [i["status"] for i in b["items"]] == ["waiting", "rejected", "waiting", "waiting"]
    slot = {i["filename"]: i["id"] for i in b["items"]}
    assert o.post(f"/api/cases/{case_id}/batches", json={"files": files}).status_code == 404       # not their case
    assert o.get(f"/api/batches/{b['id']}").status_code == 404
    text = ("Synthetic lease. Deposit refundable. " * 12).encode()
    up = lambda c, name, data, item: c.post(f"/api/cases/{case_id}/documents",
                                            files={"file": (name, data, "text/plain")}, data={"batch_item": item})
    assert up(a, "different.txt", text, slot["one.txt"]).status_code == 409    # slot is for another file
    assert up(a, "one.txt", text, slot["one.txt"]).status_code == 200
    assert up(a, "one.txt", text, slot["one.txt"]).status_code == 409          # a slot is used once
    assert up(a, "two.txt", b"\x00binary", slot["two.txt"]).status_code == 415 # content check fails → recorded
    view = a.post(f"/api/batches/{b['id']}/cancel").json()                     # stop before three.txt
    assert up(a, "three.txt", text, slot["three.txt"]).status_code == 409
    for _ in range(100):
        view = a.get(f"/api/batches/{b['id']}").json()
        if view["status"] not in ("processing", "cancelling", "queued"):
            break
        time.sleep(0.05)
    states = {i["filename"]: i["state"] for i in view["items"]}
    assert states["two.txt"] == "rejected" and states["three.txt"] == "cancelled" and states["-cmd.exe"] == "rejected"
    assert states["one.txt"] in ("ready", "cancelled") and view["status"] in ("cancelled", "partial")
    rep = a.get(f"/api/batches/{b['id']}/report.csv")
    assert rep.status_code == 200 and "attachment" in rep.headers["content-disposition"]
    body = rep.content.decode("utf-8-sig")
    assert "'-cmd.exe" in body and "does not match its extension" in body      # reasons; formula injection escaped
    assert "Synthetic lease" not in body                                        # never document contents
    assert o.get(f"/api/batches/{b['id']}/report.csv").status_code == 404


# ── LLM retries, circuit breaker, fallback ───────────────────────────────────
@pytest.fixture
def llm_server(monkeypatch):
    """Real llm.chat / llm.stream against a scripted Ollama API (no network)."""
    monkeypatch.setattr(llm, "chat", REAL_CHAT)
    monkeypatch.setattr(llm, "stream", REAL_STREAM)
    monkeypatch.setattr(llm, "_sleep", lambda attempt: None)
    monkeypatch.setattr(llm, "_breakers", {})
    monkeypatch.setattr(config, "LLM_BREAKER_FAILURES", 3)
    script: dict = {"calls": [], "handler": None}

    def handle(req):
        body = json.loads(req.content)
        script["calls"].append(body["model"])
        return script["handler"](body)
    monkeypatch.setattr(llm, "_client", httpx.Client(base_url="https://ollama.test", transport=httpx.MockTransport(handle)))
    yield script
    monkeypatch.setattr(llm, "_client", None)


def test_llm_retries_then_succeeds(llm_server):
    seq = iter([503, 502, 200])
    llm_server["handler"] = lambda b: httpx.Response(st := next(seq), json={"message": {"content": "ok"}} if st == 200 else {"error": "busy"})
    info = {}
    assert llm.chat([{"role": "user", "content": "q"}], info=info) == "ok"
    assert llm_server["calls"] == [config.LLM_MODEL] * 3 and info["model"] == config.LLM_MODEL and not info["fallback"]


def test_llm_falls_back_and_opens_the_circuit(llm_server):
    llm_server["handler"] = lambda b: (httpx.Response(503, json={"error": "overloaded"}) if b["model"] == config.LLM_MODEL
                                       else httpx.Response(200, json={"message": {"content": "fallback answer"}}))
    info = {}
    assert llm.chat([{"role": "user", "content": "q"}], info=info) == "fallback answer"
    assert info == {"model": config.LLM_FALLBACK_MODEL, "fallback": True, "ok": True}
    for _ in range(2):
        llm.chat([{"role": "user", "content": "q"}])
    assert llm.breaker_state()[config.LLM_MODEL] == "open"                    # 3 failures in a row
    before = llm_server["calls"].count(config.LLM_MODEL)
    assert llm.chat([{"role": "user", "content": "q"}]) == "fallback answer"
    assert llm_server["calls"].count(config.LLM_MODEL) == before              # main model skipped while open


def test_no_fallback_for_a_rejected_api_key(llm_server):
    llm_server["handler"] = lambda b: httpx.Response(401, json={"error": "unauthorized"})
    with pytest.raises(llm.LLMError) as e:
        llm.chat([{"role": "user", "content": "q"}])
    assert not e.value.transient and llm_server["calls"] == [config.LLM_MODEL]


def test_stream_fallback_only_before_the_first_token(llm_server):
    llm_server["handler"] = lambda b: (httpx.Response(503, json={"error": "busy"}) if b["model"] == config.LLM_MODEL else
                                       httpx.Response(200, content=b'{"message":{"content":"Hello "}}\n{"message":{"content":"there"},"done":true}\n'))
    info = {}
    assert "".join(llm.stream([{"role": "user", "content": "q"}], info=info)) == "Hello there" and info["fallback"]
    llm_server["handler"] = lambda b: httpx.Response(200, content=b'{"message":{"content":"Half an "}}\n{"error":"crashed"}\n')
    out, info = [], {}
    with pytest.raises(llm.LLMError):
        for tok in llm.stream([{"role": "user", "content": "q"}], info=info):
            out.append(tok)
    assert out == ["Half an "] and info["partial"] and llm_server["calls"][-1] == config.LLM_MODEL   # no restart mid-answer


# ── Workflow trace and partial answers ───────────────────────────────────────
def test_answer_carries_a_trace_without_document_text(fake, offline):
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    case_id = new_case(a)
    upload(a, case_id, text="CONFIDENTIAL FACT: the deposit is rupees two lakh and refundable.\n" * 4)
    r = a.post("/api/chat", json={"message": "is the deposit refundable?", "case_id": case_id, "scope": "case"})
    done = [json.loads(x[6:]) for x in r.text.splitlines() if x.startswith("data: ")][-1]
    steps = [s["step"] for s in done["meta"]["trace"]]
    assert steps[:3] == ["authorize", "screen", "classify"]
    assert {"retrieve", "generate", "verify_citations", "output_check"} <= set(steps)
    assert "CONFIDENTIAL" not in json.dumps(done["meta"]["trace"])


def test_cut_off_answer_is_labelled_incomplete(fake, offline, monkeypatch):
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    case_id = new_case(a)
    upload(a, case_id)

    def broken(messages, **kw):
        yield "**Short answer** — The deposit"
        raise llm.LLMError("Lost connection to the language model service.", transient=True)
    monkeypatch.setattr(llm, "stream", broken)
    r = a.post("/api/chat", json={"message": "is the deposit refundable?", "case_id": case_id, "scope": "case"})
    events = [json.loads(x[6:]) for x in r.text.splitlines() if x.startswith("data: ")]
    done = events[-1]
    assert done["meta"]["partial"] is True and "verify" not in done["meta"]   # never presented as verified
    text = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "incomplete" in text


# ── Draft review ─────────────────────────────────────────────────────────────
def test_draft_must_be_approved_per_version(fake, monkeypatch):
    person(fake, "adv@example.com", "advocate")
    person(fake, "client@example.com", "client")
    person(fake, "other@example.com", "advocate")
    a, cl, o = signed_in("adv@example.com"), signed_in("client@example.com"), signed_in("other@example.com")
    case_id = new_case(a)
    monkeypatch.setattr(llm, "chat", lambda *x, **k: "DRAFT — FOR REVIEW BY A QUALIFIED LEGAL PROFESSIONAL\n\nNotice to "
                                                      "[●: name of the opposite party] to pay the deposit.")
    job = a.post("/api/drafts/generate", json={"doc_type": "notice_ni_138", "forum": "none", "case_id": case_id}).json()
    d = wait_job(a, job["id"])["result"]
    assert d["review_status"] == "draft"
    r = a.post(f"/api/drafts/{d['id']}/review", json={"action": "approve"})
    assert r.status_code == 409 and "placeholder" in r.json()["detail"]       # not approvable with gaps
    assert o.post(f"/api/drafts/{d['id']}/review", json={"action": "approve"}).status_code == 404
    clean = d["content"].replace("[●: name of the opposite party]", "Ramesh Kumar")
    assert a.put(f"/api/drafts/{d['id']}", json={"content": clean}).status_code == 200
    ok = a.post(f"/api/drafts/{d['id']}/review", json={"action": "approve", "note": "checked"}).json()
    assert ok["review_status"] == "approved" and ok["approved_version"] == ok["version"]
    docx = a.get(f"/api/drafts/{d['id']}/export?format=docx")
    xml = zipfile.ZipFile(io.BytesIO(docx.content)).read("word/document.xml").decode("utf-8")
    assert "Approved by" in xml and "FOR REVIEW" not in xml
    edited = a.put(f"/api/drafts/{d['id']}", json={"content": clean + "\nOne more line."}).json()
    assert edited["review_status"] == "draft" and edited["approved_version"] is None   # new version, new review
    xml2 = zipfile.ZipFile(io.BytesIO(a.get(f"/api/drafts/{d['id']}/export?format=docx").content)).read("word/document.xml").decode()
    assert "FOR REVIEW" in xml2
    entries = [x["action"] for x in signed_in_admin_items(fake)]
    assert "draft.approve" in entries


def signed_in_admin_items(fake):
    person(fake, "auditor@example.com", "auditor")
    security_limiter_reset()
    return signed_in("auditor@example.com").get("/api/audit?limit=500").json()["items"]


# ── Library versions ─────────────────────────────────────────────────────────
def test_revised_act_becomes_a_new_version_and_old_text_leaves_search(fresh_db, tmp_path, monkeypatch):
    from nyaya import retrieval
    acts = config.BARE_ACTS_DIR
    acts.mkdir(parents=True, exist_ok=True)
    pdf_text = lambda body: _pdf_bytes(body)
    path = acts / "synthetic_act.pdf"
    path.write_bytes(pdf_text("THE SYNTHETIC ACT, 2026\n1. Short title. This Act may be called the Synthetic Act.\n"
                              "2. Penalty. Whoever breaks a rule shall pay one hundred rupees.\n" * 4))
    with db.system():
        ingest.build_library(progress=lambda s: None)
        v1 = db.conn().execute("SELECT id FROM documents WHERE collection='bare_act'").fetchone()["id"]
    path.write_bytes(pdf_text("THE SYNTHETIC ACT, 2026\n1. Short title. This Act may be called the Synthetic Act.\n"
                              "2. Penalty. Whoever breaks a rule shall pay five thousand rupees.\n" * 4))
    with db.system():
        ingest.build_library(progress=lambda s: None)
        rows = [dict(r) for r in db.conn().execute("SELECT id, status, meta FROM documents WHERE collection='bare_act' "
                                                   "ORDER BY created_at")]
        assert [r["status"] for r in rows] == ["superseded", "ready"]
        assert json.loads(rows[1]["meta"])["version"] == 2 and json.loads(rows[1]["meta"])["supersedes"] == [v1]
        hits, _ = retrieval.search("penalty for breaking a rule", scope="library", k=8)
        assert hits and all(h.doc_id != v1 for h in hits)                      # only the current text is searched
        assert [v["version"] for v in ingest.versions(rows[1]["id"])] == [2, 1]


def _pdf_bytes(text: str) -> bytes:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in text.splitlines():
        pdf.multi_cell(0, 6, line or " ", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
