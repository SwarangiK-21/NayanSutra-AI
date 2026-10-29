"""Data retention: expired data is deleted, recent data kept; inactive accounts are warned, then deleted with their
personal data (sole-member cases too, shared cases kept); activity cancels a warning. Offline (SQLite)."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nyaya import config, db, retention
from nyaya.server import app

NOW = datetime(2030, 1, 15, 12, 0, 0)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


def _user(c, uid, last_active, warned=None):
    c.execute("INSERT INTO profiles(user_id, email, display_name, status, mfa_enabled, created_at, updated_at, "
              "last_active_at, retention_warned_at) VALUES(?,?,?,?,?,?,?,?,?)",
              (uid, f"{uid}@example.com", uid, "active", 0, ago(days=2000), ago(days=2000), last_active, warned))
    c.execute("INSERT INTO memberships(org_id, user_id, role, created_at) VALUES(?,?,?,?)",
              (db.DEFAULT_ORG, uid, "advocate", ago(days=2000)))


def _chat(c, cid, uid, updated):
    c.execute("INSERT INTO chats(id, case_id, title, created_at, updated_at, user_id, org_id) VALUES(?,?,?,?,?,?,?)",
              (cid, None, "t", updated, updated, uid, db.DEFAULT_ORG))
    c.execute("INSERT INTO messages(chat_id, role, content, sources, meta, created_at) VALUES(?,?,?,?,?,?)",
              (cid, "user", "q", "[]", "{}", updated))


def _case(c, case_id, members):
    c.execute("INSERT INTO cases(id, title, case_type, created_at, updated_at, org_id) VALUES(?,?,?,?,?,?)",
              (case_id, case_id, "Civil", ago(days=2000), ago(days=2000), db.DEFAULT_ORG))
    for m in members:
        c.execute("INSERT INTO case_members(case_id, user_id, access, added_by, created_at) VALUES(?,?,?,?,?)",
                  (case_id, m, "owner", m, ago(days=2000)))


@pytest.fixture
def data(fresh_db):
    with db.system():
        with db.tx() as c:
            _user(c, "active_user", ago(days=10))
            _user(c, "idle_33m", ago(days=34 * 30))
            _user(c, "idle_37m_warned", ago(days=37 * 31), warned=ago(days=60))
            _user(c, "idle_37m_warned_recently", ago(days=37 * 31), warned=ago(days=5))
            _chat(c, "chat_old", "active_user", ago(days=26 * 31))
            _chat(c, "chat_new", "active_user", ago(days=30))
            _chat(c, "chat_of_idle", "idle_37m_warned", ago(days=30))
            c.execute("INSERT INTO search_history(id, user_id, org_id, query, filters, results, created_at) "
                      "VALUES('s_old','active_user',?, 'q', '{}', 1, ?)", (db.DEFAULT_ORG, ago(days=25 * 31)))
            c.execute("INSERT INTO search_history(id, user_id, org_id, query, filters, results, created_at) "
                      "VALUES('s_new','active_user',?, 'q', '{}', 1, ?)", (db.DEFAULT_ORG, ago(days=10)))
            c.execute("INSERT INTO audit_log(at, action, entity) VALUES(?, 'x', 'y')", (ago(days=6 * 366),))
            c.execute("INSERT INTO audit_log(at, action, entity) VALUES(?, 'x', 'y')", (ago(days=100),))
            _case(c, "case_solo", ["idle_37m_warned"])
            _case(c, "case_shared", ["idle_37m_warned", "active_user"])
    yield


def _count(sql, *args):
    with db.system():
        return db.conn().execute(sql, args).fetchone()[0]


def test_dry_run_reports_without_deleting(data):
    res = retention.run(now=NOW, dry_run=True)
    assert res["chats"] == 1 and res["search_history"] == 1 and res["audit_log"] == 1
    assert res["accounts_warned"] == 1 and res["accounts_deleted"] == 1
    assert _count("SELECT COUNT(*) FROM chats") == 3 and _count("SELECT COUNT(*) FROM profiles") == 4


def test_expired_data_is_deleted_and_recent_kept(data):
    res = retention.run(now=NOW)
    assert _count("SELECT COUNT(*) FROM chats WHERE id='chat_old'") == 0
    assert _count("SELECT COUNT(*) FROM messages WHERE chat_id='chat_old'") == 0
    assert _count("SELECT COUNT(*) FROM chats WHERE id='chat_new'") == 1
    assert _count("SELECT COUNT(*) FROM search_history") == 1
    assert _count("SELECT COUNT(*) FROM audit_log WHERE at < ?", ago(days=5 * 366)) == 0
    assert res["audit_log"] == 1


def test_inactive_account_is_warned_then_deleted(data):
    retention.run(now=NOW)
    warned = _count("SELECT COUNT(*) FROM profiles WHERE user_id='idle_33m' AND retention_warned_at IS NOT NULL")
    assert warned == 1
    assert _count("SELECT COUNT(*) FROM notifications WHERE user_id='idle_33m'") == 1
    # warned 60 days ago and inactive 37 months: deleted with personal data and the case only they were on
    assert _count("SELECT COUNT(*) FROM profiles WHERE user_id='idle_37m_warned'") == 0
    assert _count("SELECT COUNT(*) FROM chats WHERE user_id='idle_37m_warned'") == 0
    assert _count("SELECT COUNT(*) FROM cases WHERE id='case_solo'") == 0
    assert _count("SELECT COUNT(*) FROM cases WHERE id='case_shared'") == 1           # another member still uses it
    assert _count("SELECT COUNT(*) FROM case_members WHERE case_id='case_shared'") == 1
    # warned only 5 days ago: not yet deleted (30-day notice)
    assert _count("SELECT COUNT(*) FROM profiles WHERE user_id='idle_37m_warned_recently'") == 1
    assert _count("SELECT COUNT(*) FROM profiles WHERE user_id='active_user'") == 1


def test_using_the_app_cancels_the_warning(data):
    retention.run(now=NOW)
    retention.mark_active("idle_33m", NOW + timedelta(days=1))
    row = None
    with db.system():
        row = db.conn().execute("SELECT last_active_at, retention_warned_at FROM profiles WHERE user_id='idle_33m'"
                                ).fetchone()
    assert row["retention_warned_at"] is None and row["last_active_at"].startswith("2030-01-16")


def test_zero_means_keep(data, monkeypatch):
    monkeypatch.setattr(config, "RETENTION_CHAT_MONTHS", 0)
    retention.run(now=NOW)
    assert _count("SELECT COUNT(*) FROM chats WHERE id='chat_old'") == 1


def test_health_check_needs_no_sign_in_and_returns_no_data(fresh_db):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:   # no session cookie at all
        r = c.get("/api/health")
        assert r.status_code == 200 and set(r.json()) == {"ok", "database", "index_loaded"} and r.json()["ok"]


def test_privacy_page_api_is_public(fresh_db):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        c.get("/api/session")
        r = c.get("/api/privacy")
        assert r.status_code == 200
        rows = r.json()["periods"]
        assert any(p["kept"] == "24 months" for p in rows) and any("5 years" in p["kept"] for p in rows)
