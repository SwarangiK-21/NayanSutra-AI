"""Upgrade tests (offline, synthetic data): open sign-up as Lawyer, the two personas, all-India jurisdiction
(India Code catalogue / import, research filter), Recent History, calendar CRUD with authorization, and reminder
delivery (in-app + e-mail through a fake SMTP server), including time zones and retries."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from urllib.parse import unquote_plus

import httpx
import pytest

from nyaya import accounts, config, db, events, indiacode, ingest, mailer, retrieval
from test_security import PW, chat, fake, guest, new_case, person, security_limiter_reset, signed_in  # noqa: F401


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _event(c, **kw):
    body = {"title": "File written statement", "kind": "deadline", "event_date": _future(10), "event_time": "10:00",
            "timezone": "Asia/Kolkata", "reminders": ["1d", "7d"], **kw}
    return c.post("/api/calendar/events", json=body)


# ── Personas and public sign-up ──────────────────────────────────────────────
def test_public_signup_is_a_lawyer_and_cannot_become_admin(fake, monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_MODE", "open")
    r = guest().post("/api/auth/signup", json={"email": "new@example.com", "password": PW, "display_name": "New",
                                               "role": "admin"})
    assert r.status_code == 200
    # E-mail verification is still required before the first sign-in…
    g = guest()
    r = g.post("/api/auth/login", json={"email": "new@example.com", "password": PW})
    assert r.status_code == 403 and r.json()["code"] == "email_not_confirmed"
    # …and opening the link confirms it (the fake server's equivalent of clicking it).
    uid_new = next(u for u, x in fake.users.items() if x["email"] == "new@example.com")
    fake.users[uid_new]["email_confirmed_at"] = "2026-01-01T00:00:00Z"
    me = signed_in("new@example.com")
    s = me.get("/api/session").json()
    assert s["role"] == "advocate"
    assert me.get("/api/admin/users").status_code == 403 and me.get("/api/audit").status_code == 403
    uid = s["user"]["id"]
    assert me.patch(f"/api/admin/users/{uid}", json={"role": "admin"}).status_code == 403
    assert me.post("/api/admin/invitations", json={"email": "x@example.com", "role": "admin"}).status_code == 403
    with db.system():
        assert db.conn().execute("SELECT role FROM memberships WHERE user_id=?", (uid,)).fetchone()["role"] == "advocate"
        assert any(r["action"] == "membership.self_registered" for r in db.conn().execute("SELECT action FROM audit_log"))
    # Invite-only remains available as a setting: an uninvited account then has no access.
    monkeypatch.setattr(config, "SIGNUP_MODE", "invite_only")
    fake.add_user("closed@example.com", PW)
    assert signed_in("closed@example.com").get("/api/session").json()["role"] == "guest"


def test_retired_personas_are_migrated_without_gaining_case_access(fresh_db):
    """Migration 4 turns old client/auditor accounts into Lawyers and removes per-case client access."""
    with db.system():
        with db.tx() as c:
            now = db.now()
            c.execute("INSERT INTO profiles(user_id, email, status, created_at, updated_at) VALUES('u1','c@example.com','active',?,?)", (now, now))
            c.execute("INSERT INTO cases(id, title, case_type, org_id, created_at, updated_at) VALUES('C1','T','Civil','org_default',?,?)", (now, now))
            c.execute("INSERT INTO case_members(case_id, user_id, access, created_at) VALUES('C1','u1','client',?)", (now,))
        script = db.MIGRATIONS[3]
        conn = db.conn()
        conn.executescript(script.split("ALTER TABLE documents")[0])   # the persona part of the migration
        assert conn.execute("SELECT COUNT(*) FROM case_members WHERE access='client'").fetchone()[0] == 0


# ── Calendar: CRUD, privacy, validation ──────────────────────────────────────
def test_calendar_crud_privacy_and_case_link(fake):
    person(fake, "a@example.com", "advocate")
    person(fake, "b@example.com", "advocate")
    person(fake, "admin@example.com", "admin")
    a, b, admin = signed_in("a@example.com"), signed_in("b@example.com"), signed_in("admin@example.com")
    case_a = new_case(a)
    case_b = new_case(b, title="Other matter")
    r = _event(a, case_id=case_a, kind="hearing", title="Final arguments", priority="high")
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["case_title"] and ev["priority"] == "high" and {x["rule"] for x in ev["reminder_status"]} == {"1d", "7d"}
    # Times are computed in the event's own zone: 10:00 IST one day before = 04:30 UTC.
    one_day = next(x for x in ev["reminder_status"] if x["rule"] == "1d")
    assert one_day["remind_at_utc"] == f"{_future(9)}T04:30:00" and one_day["status"] == "pending"
    # Private: another lawyer and an administrator can neither see nor change it.
    for other in (b, admin):
        assert other.get(f"/api/calendar/events/{ev['id']}").status_code == 404
        assert other.patch(f"/api/calendar/events/{ev['id']}", json={"title": "x"}).status_code == 404
        assert other.delete(f"/api/calendar/events/{ev['id']}").status_code == 404
        listed = other.get(f"/api/calendar/events?start={_future(0)}&end={_future(30)}").json()["events"]
        assert all(e["id"] != ev["id"] for e in listed)
    # An entry can only be linked to a matter its owner can open.
    assert _event(a, case_id=case_b).status_code == 404
    assert a.patch(f"/api/calendar/events/{ev['id']}", json={"case_id": case_b}).status_code == 404
    # Reschedule: reminders move with the date.
    moved = a.patch(f"/api/calendar/events/{ev['id']}", json={"event_date": _future(20), "event_time": "15:30"}).json()
    assert next(x for x in moved["reminder_status"] if x["rule"] == "1d")["remind_at_utc"] == f"{_future(19)}T10:00:00"
    # Complete → no pending reminders; reopen → scheduled again.
    done = a.patch(f"/api/calendar/events/{ev['id']}", json={"status": "completed"}).json()
    assert done["status"] == "completed" and done["completed_at"] and not done["reminder_status"]
    again = a.patch(f"/api/calendar/events/{ev['id']}", json={"status": "scheduled"}).json()
    assert len(again["reminder_status"]) == 2 and again["completed_at"] is None
    # Month view + upcoming list.
    month = a.get(f"/api/calendar/events?start={_future(0)}&end={_future(40)}").json()["events"]
    assert any(e["id"] == ev["id"] for e in month)
    up = a.get("/api/calendar/upcoming?tz=Asia/Kolkata").json()
    assert any(e["id"] == ev["id"] for e in up["items"])
    assert a.delete(f"/api/calendar/events/{ev['id']}").status_code == 200
    assert a.get(f"/api/calendar/events/{ev['id']}").status_code == 404
    assert guest().get(f"/api/calendar/events?start={_future(0)}&end={_future(5)}").status_code == 401


def test_calendar_validation_and_case_hearings(fake):
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    assert _event(a, event_date="2026-02-30").status_code == 400
    assert _event(a, event_time="25:00").status_code == 400
    assert _event(a, timezone="Mars/Olympus").status_code == 400
    assert _event(a, title="  ").status_code == 400
    assert _event(a, reminders=["2d"]).status_code == 422
    assert _event(a, kind="party").status_code == 422
    assert a.get("/api/calendar/events?start=2026-01-01&end=2026-12-31").status_code == 400   # range too long
    # A matter's next hearing date shows up on the calendar of the people on the case, read-only…
    cid = new_case(a)
    assert a.patch(f"/api/cases/{cid}", json={"next_hearing_date": _future(5)}).status_code == 200
    items = a.get(f"/api/calendar/events?start={_future(0)}&end={_future(10)}").json()["events"]
    hearing = next(e for e in items if e.get("source") == "case")
    assert hearing["readonly"] and hearing["case_id"] == cid and hearing["event_date"] == _future(5)
    # …until the person adds their own hearing entry for that day (no duplicates).
    _event(a, kind="hearing", case_id=cid, event_date=_future(5), title="Hearing")
    items = a.get(f"/api/calendar/events?start={_future(0)}&end={_future(10)}").json()["events"]
    assert [e.get("source") for e in items] == ["own"]


def test_past_reminders_are_skipped_not_sent_late(fake):
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    ev = _event(a, event_date=_future(2), reminders=["7d", "1d", "at_time"]).json()
    st = {x["rule"]: x["status"] for x in ev["reminder_status"]}
    assert st == {"7d": "skipped", "1d": "pending", "at_time": "pending"}


def test_reminder_times_follow_the_event_time_zone():
    t = events.reminder_times("2026-07-01", "10:00", "Europe/London", ["at_time", "1d", "day_of", "1h"])
    assert events.utc_str(t["at_time"]) == "2026-07-01T09:00:00"          # BST = UTC+1
    assert events.utc_str(t["1d"]) == "2026-06-30T09:00:00"
    assert events.utc_str(t["day_of"]) == "2026-07-01T07:00:00"           # 08:00 local
    assert events.utc_str(t["1h"]) == "2026-07-01T08:00:00"
    allday = events.reminder_times("2026-12-01", None, "Asia/Kolkata", ["at_time", "day_of"])
    assert events.utc_str(allday["at_time"]) == "2026-12-01T03:30:00"     # all-day items start at 09:00 local
    assert events.utc_str(allday["day_of"]) == "2026-12-01T02:30:00"


# ── Reminder delivery ────────────────────────────────────────────────────────
def _make_due(event_id: str) -> None:
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE event_reminders SET remind_at_utc=? WHERE event_id=? AND status='pending'",
                      (events.utc_str(events.utcnow() - timedelta(minutes=1)), event_id))


class FakeSMTP:
    sent: list = []
    fail = 0

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail:
            FakeSMTP.fail -= 1
            raise OSError("connection refused")
        FakeSMTP.sent.append(msg)


def test_reminders_are_delivered_once_in_app_and_by_email(fake, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_FROM", "NyaySutra <noreply@example.test>")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent, FakeSMTP.fail = [], 0
    person(fake, "a@example.com", "advocate")
    person(fake, "b@example.com", "advocate")
    a, b = signed_in("a@example.com"), signed_in("b@example.com")
    assert a.get("/api/calendar/config").json()["email_available"] is True
    ev = _event(a, title="Submit vakalatnama", kind="submission", reminders=["1d"], email_reminders=True).json()
    assert {(x["rule"], x["channel"]) for x in ev["reminder_status"]} == {("1d", "in_app"), ("1d", "email")}
    assert events.dispatch_due() == 0                           # nothing is due yet
    _make_due(ev["id"])
    assert events.dispatch_due() == 2
    notes = a.get("/api/notifications").json()["items"]
    rem = [n for n in notes if n["kind"] == "reminder"]
    assert len(rem) == 1 and "Submit vakalatnama" in rem[0]["title"] and rem[0]["link"] == f"#/calendar?event={ev['id']}"
    assert not [n for n in b.get("/api/notifications").json()["items"] if n["kind"] == "reminder"]   # only the owner
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["To"] == "a@example.com"
    assert events.dispatch_due() == 0                           # never twice
    statuses = {x["channel"]: x["status"] for x in a.get(f"/api/calendar/events/{ev['id']}").json()["reminder_status"]}
    assert statuses == {"in_app": "sent", "email": "sent"}


def test_email_failures_retry_then_stop_and_cancelled_entries_stay_quiet(fake, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_FROM", "noreply@example.test")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent, FakeSMTP.fail = [], 99
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    ev = _event(a, reminders=["1d"], email_reminders=True).json()
    for _ in range(events.MAX_ATTEMPTS):
        _make_due(ev["id"])
        events.dispatch_due()
    em = next(x for x in a.get(f"/api/calendar/events/{ev['id']}").json()["reminder_status"] if x["channel"] == "email")
    assert em["status"] == "failed" and "mail server" in em["error"] and not FakeSMTP.sent
    # A cancelled entry's reminders are not sent.
    ev2 = _event(a, title="Client meeting", kind="meeting", reminders=["1d"]).json()
    _make_due(ev2["id"])
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE calendar_events SET status='cancelled' WHERE id=?", (ev2["id"],))
    events.dispatch_due()
    with db.system():
        st = db.conn().execute("SELECT status FROM event_reminders WHERE event_id=?", (ev2["id"],)).fetchone()["status"]
    assert st == "skipped"
    assert not [n for n in a.get("/api/notifications").json()["items"] if "Client meeting" in n["title"]]


def test_email_option_is_honest_when_smtp_is_not_configured(fake):
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    assert a.get("/api/calendar/config").json()["email_available"] is False
    ev = _event(a, reminders=["1d"], email_reminders=True).json()
    em = next(x for x in ev["reminder_status"] if x["channel"] == "email")
    assert em["status"] == "skipped" and "not configured" in em["error"]


# ── Recent History ───────────────────────────────────────────────────────────
def test_history_is_personal_and_reopens_chats_and_searches(fake, offline):
    person(fake, "a@example.com", "advocate")
    person(fake, "b@example.com", "advocate")
    a, b = signed_in("a@example.com"), signed_in("b@example.com")
    cid = new_case(a)
    _, ev = chat(a, message="Is the deposit refundable?", case_id=cid)
    chat_id = next(e["chat_id"] for e in ev if e["type"] == "chat")
    chat(a, message="What is the punishment for cheating?")
    assert a.get("/api/search?q=deposit&types=bare_act").status_code == 200
    h = a.get("/api/history").json()
    assert {c["id"] for c in h["chats"]} >= {chat_id} and len(h["chats"]) == 2
    assert next(c for c in h["chats"] if c["id"] == chat_id)["case_title"]
    assert h["searches"][0]["query"] == "deposit" and h["searches"][0]["filters"] == {"types": "bare_act"}
    hb = b.get("/api/history").json()
    assert hb == {"chats": [], "searches": []}                                  # nobody else's history
    assert b.delete(f"/api/history/searches/{h['searches'][0]['id']}").status_code == 404
    assert a.delete(f"/api/history/searches/{h['searches'][0]['id']}").status_code == 200
    a.get("/api/search?q=bail")
    assert a.delete("/api/history/searches").json()["deleted"] == 1
    assert a.get("/api/history").json()["searches"] == []
    assert "recent_chats" not in a.get("/api/dashboard").json()                  # moved off the dashboard


# ── All-India jurisdiction ───────────────────────────────────────────────────
def _state_act(tmp_path, name: str, jurisdiction: str, text: str) -> str:
    p = tmp_path / f"{name}.txt"
    p.write_text(text, "utf-8")
    with db.system():
        doc, _ = ingest.register(p, collection="bare_act", case_id=None, doc_type="statute", title=name, filename=p.name,
                                 meta={"jurisdiction": jurisdiction, "title": name})
        ingest.process(doc["id"])
    return doc["id"]


def test_research_is_limited_to_central_law_plus_the_chosen_state(fake, offline, tmp_path):
    ka = _state_act(tmp_path, "The Karnataka Rent Act, 1999", "Karnataka",
                    "THE KARNATAKA RENT ACT\n5. Increase of rent. The landlord may increase the rent by five per cent.\n" * 4)
    tn = _state_act(tmp_path, "The Tamil Nadu Rent Act, 2017", "Tamil Nadu",
                    "THE TAMIL NADU RENT ACT\n5. Increase of rent. The landlord may increase the rent by ten per cent.\n" * 4)
    with db.system():
        both, _ = retrieval.search("landlord increase of rent", scope="library", k=8)
        only_ka, diag = retrieval.search("landlord increase of rent", scope="library", k=8, jurisdiction="Karnataka")
    assert {ka, tn} <= {h.doc_id for h in both}
    assert ka in {h.doc_id for h in only_ka} and tn not in {h.doc_id for h in only_ka} and diag["jurisdiction"] == "Karnataka"
    assert next(h for h in only_ka if h.doc_id == ka).jurisdiction == "Karnataka"
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    _, ev = chat(a, message="Can the landlord increase the rent?", jurisdiction="Tamil Nadu")
    src = next(e for e in ev if e["type"] == "sources")["items"]
    assert any(s["jurisdiction"] == "Tamil Nadu" for s in src) and not any(s["jurisdiction"] == "Karnataka" for s in src)
    r, _ = chat(a, message="rent", jurisdiction="Atlantis")
    assert r.status_code == 422
    j = a.get("/api/jurisdictions").json()
    assert len(j["states"]) == 36 and next(s for s in j["states"] if s["name"] == "Karnataka")["in_library"] == 1
    assert next(s for s in j["states"] if s["name"] == "Ladakh")["union_territory"]


def _india_code_mock(pdf: bytes = b"%PDF-1.4 synthetic act text"):
    item = {"uuid": "11111111-2222-3333-4444-555555555555", "withdrawn": False, "metadata": {
        "dc.title": [{"value": "The Synthetic State Rent Act, 2020"}], "dc.identifier.collection": [{"value": "ACT"}],
        "dc.identifier.act_id": [{"value": "AC_PY_1_2_3"}], "dc.identifier.state_name": [{"value": "puducherry"}],
        "dc.date.act_year": [{"value": "2020"}], "dc.identifier.act_number": [{"value": "7"}],
        "dc.identifier.repealed": [{"value": "false"}]}}
    central = {"uuid": "99999999-2222-3333-4444-555555555555", "metadata": {
        "dc.title": [{"value": "The Synthetic Central Act, 2021"}], "dc.identifier.act_id": [{"value": "AC_CEN_1_2"}],
        "dc.identifier.collection": [{"value": "ACT"}], "dc.identifier.repealed": [{"value": "true"}]}}
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        path = req.url.path
        if path.endswith("/discover/search/objects"):
            objs = [{"_embedded": {"indexableObject": x}} for x in (item, central)]
            return httpx.Response(200, json={"_embedded": {"searchResult": {"_embedded": {"objects": objs},
                                                                            "page": {"totalElements": 2, "number": 0, "totalPages": 1}}}})
        if path.endswith(f"/core/items/{item['uuid']}"):
            return httpx.Response(200, json=item)
        if path.endswith("/bundles"):
            return httpx.Response(200, json={"_embedded": {"bundles": [{"name": "ORIGINAL", "_links": {"bitstreams": {
                "href": "https://indiacode.gov.in/server/api/core/bundles/b1/bitstreams"}}}]}})
        if path.endswith("/bitstreams"):
            return httpx.Response(200, json={"_embedded": {"bitstreams": [
                {"uuid": "b-tamil", "name": "7_of_2020_(t).pdf", "sizeBytes": 10},
                {"uuid": "b-eng", "name": "7_of_2020_(e).pdf", "sizeBytes": 20}]}})
        if path.endswith("/bitstreams/b-eng/content"):
            return httpx.Response(200, content=pdf)
        return httpx.Response(404)
    return handler, seen


def test_india_code_catalogue_and_import(fake, monkeypatch, tmp_path):
    handler, seen = _india_code_mock()
    monkeypatch.setattr(indiacode, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    queued = []
    monkeypatch.setattr(ingest, "queue", lambda doc_id, owner, org_id=None: queued.append(doc_id) or {})
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    res = a.get("/api/library/indiacode?state=Puducherry&q=rent").json()
    assert 'dc.identifier.state_name:"Puducherry"' in unquote_plus(seen[0])
    first, second = res["items"]
    assert first["jurisdiction"] == "Puducherry" and first["repealed"] is False and first["in_library"] is None
    assert second["jurisdiction"] == "Central" and second["repealed"] is True
    assert a.get("/api/library/indiacode?state=Atlantis").status_code == 400
    d = a.post("/api/library/indiacode/import", json={"uuid": first["uuid"]}).json()
    assert d["jurisdiction"] == "Puducherry" and not d["duplicate"] and queued == [d["id"]]
    assert any(u.endswith("/bitstreams/b-eng/content") for u in seen)          # the English text, not the Tamil one
    with db.system():
        row = db.conn().execute("SELECT jurisdiction, path, meta FROM documents WHERE id=?", (d["id"],)).fetchone()
    assert row["jurisdiction"] == "Puducherry" and "states/puducherry/" in row["path"].replace("\\", "/")
    assert json.loads(row["meta"])["item_url"].startswith("https://indiacode.gov.in/items/")
    manifest = json.loads((config.BARE_ACTS_DIR / "states" / "manifest.json").read_text("utf-8"))   # runtime imports
    assert any(k.startswith("states/puducherry/") and v["jurisdiction"] == "Puducherry" for k, v in manifest.items())
    assert any(k.startswith("states/puducherry/") for k in indiacode.read_manifest())
    again = a.post("/api/library/indiacode/import", json={"uuid": first["uuid"]}).json()
    assert again["duplicate"] and again["id"] == d["id"]                         # no second copy
    assert a.get("/api/library/indiacode?state=Puducherry").json()["items"][0]["in_library"]["doc_id"] == d["id"]
    assert guest().post("/api/library/indiacode/import", json={"uuid": first["uuid"]}).status_code == 401
    assert a.post("/api/library/indiacode/import", json={"uuid": "x" * 36}).status_code == 502


def test_india_code_import_refuses_non_pdf_content(fake, monkeypatch):
    handler, _ = _india_code_mock(pdf=b"<html>not a pdf</html>")
    monkeypatch.setattr(indiacode, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    person(fake, "a@example.com", "advocate")
    a = signed_in("a@example.com")
    r = a.post("/api/library/indiacode/import", json={"uuid": "11111111-2222-3333-4444-555555555555"})
    assert r.status_code == 502 and "PDF" in r.json()["detail"]
    with db.system():
        assert not db.conn().execute("SELECT 1 FROM documents WHERE jurisdiction='Puducherry'").fetchone()
    with pytest.raises(indiacode.IndiaCodeError):
        with httpx.Client() as c:
            indiacode._get_json(c, "https://evil.example/server/api/x")


def test_bare_act_reader_shows_only_library_acts(fresh_db):
    from fastapi.testclient import TestClient
    from nyaya import db
    from nyaya.server import app
    with db.system():
        with db.tx() as c:
            c.execute("INSERT INTO documents(id, collection, title, filename, path, doc_type, status, created_at, sha256) "
                      "VALUES('act_r','bare_act','Sample Act','a.pdf','a.pdf','statute','ready','2026-01-01','s1')")
            c.execute("INSERT INTO documents(id, collection, title, filename, path, doc_type, status, created_at, sha256) "
                      "VALUES('case_r','case','Private paper','b.pdf','b.pdf','other','ready','2026-01-01','s2')")
            for i, (doc, label, text) in enumerate([("act_r", "Section 1", "First part."), ("act_r", "Section 1", "Second part."),
                                                    ("act_r", "Section 2", "Other."), ("case_r", "Section 9", "Secret.")]):
                c.execute("INSERT INTO chunks(doc_id, ord, header, text, label, title, page_start) VALUES(?,?,?,?,?,?,?)",
                          (doc, i, "h", text, label, "Title", 1))
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    client.get("/api/session")
    toc = client.get("/api/library/act/act_r").json()
    assert [s["label"] for s in toc["sections"]] == ["Section 1", "Section 2"]
    one = client.get("/api/library/act/act_r", params={"label": "Section 1"}).json()
    assert one["text"] == "First part.\nSecond part." and one["page"] == 1
    assert client.get("/api/library/act/act_r", params={"label": "Section 7"}).status_code == 404
    assert client.get("/api/library/act/case_r").status_code == 404      # case documents are never served here

