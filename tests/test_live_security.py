"""Live security tests against the real Supabase project (opt-in: NYAYA_LIVE_SUPABASE=1; credentials from .env).

Synthetic data only: accounts are created as qa-…@example.com (reserved domain, no e-mail is sent: they are created
confirmed through the admin API) and every row and account is removed at the end, whatever the outcome.

* test_live_rls_isolation      — Postgres row-level security with real per-request claims: another user, an anonymous
                                 caller and a user trying to escalate are all refused by the database itself.
* test_live_auth_mfa_and_vault — Supabase Auth through the app's HTTP API: password sign-in, TOTP enrolment and
                                 challenge (aal2), session tokens kept in Supabase Vault, sign-out.
"""
from __future__ import annotations

import os
import secrets
import time
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from fake_auth import totp
from nyaya import accounts, config, db, sessions
from nyaya import supabase_auth as sb

LIVE = os.getenv("NYAYA_LIVE_SUPABASE") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set NYAYA_LIVE_SUPABASE=1 to run against the real Supabase project")
BASE = "http://127.0.0.1:8000"


@pytest.fixture
def live(monkeypatch):
    from dotenv import dotenv_values
    env = dotenv_values(config.ROOT / ".env")
    for k in ("SUPABASE_DB_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_PUBLISHABLE_KEY"):
        monkeypatch.setattr(config, k, os.getenv(k) or env.get(k, ""))
    monkeypatch.setattr(config, "AUTH_MODE", "supabase")
    monkeypatch.setattr(sb, "transport", None)
    sb.reset_client()
    db.close_pool()
    assert db.backend() == "postgres" and sb.enabled()
    yield
    sb.reset_client()
    db.close_pool()


def _as(user_id: str | None, org: str = db.DEFAULT_ORG, role: str = "advocate"):
    return db.as_identity(db.Identity(user_id=user_id, org_id=org, role=role) if user_id else db.ANONYMOUS)


def _cleanup(user_ids: list[str], case_ids: list[str]) -> None:
    with db.system():
        with db.tx() as c:
            for cid in case_ids:
                c.execute("DELETE FROM cases WHERE id=?", (cid,))
            for uid in user_ids:
                for r in c.execute("SELECT id, token_ref FROM app_sessions WHERE user_id=?", (uid,)).fetchall():
                    if r["token_ref"]:
                        c.execute("SELECT private.token_delete(CAST(? AS uuid))", (r["token_ref"],))
                c.execute("DELETE FROM app_sessions WHERE user_id=?", (uid,))
                c.execute("DELETE FROM user_prefs WHERE user_id=?", (uid,))
                c.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
                c.execute("DELETE FROM profiles WHERE user_id=?", (uid,))   # memberships, codes cascade
            c.execute("DELETE FROM invitations WHERE email LIKE 'qa-%@example.com'")


def test_live_rls_isolation(live):
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
    tag = secrets.token_hex(3)
    case_id = f"CASE-QA-{tag}"
    doc_id = f"doc_qa{tag}"
    try:
        with db.system():
            with db.tx() as c:
                for uid, n in ((u1, 1), (u2, 2)):
                    c.execute("INSERT INTO profiles(user_id, email, display_name, created_at, updated_at) VALUES(?,?,?,?,?)",
                              (uid, f"qa-rls{n}-{tag}@example.com", f"QA {n}", db.now(), db.now()))
                    c.execute("INSERT INTO memberships(org_id, user_id, role, created_at) VALUES(?,?,?,?)",
                              (db.DEFAULT_ORG, uid, "advocate", db.now()))
        # User 1 creates a case: allowed by the insert policy; the trigger makes them its owner.
        with _as(u1):
            with db.tx() as c:
                c.execute("INSERT INTO cases(id, title, case_type, org_id, created_by, created_at, updated_at) "
                          "VALUES(?,?,?,?,?,?,?)", (case_id, "QA synthetic matter", "Civil", db.DEFAULT_ORG, u1, db.now(), db.now()))
                c.execute("INSERT INTO documents(id, case_id, collection, doc_type, title, filename, path, sha256, status, "
                          "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (doc_id, case_id, "case", "other", "QA doc", "qa.txt", "sb://case-files/qa", tag, "ready", db.now()))
            assert db.conn().execute("SELECT COUNT(*) FROM cases WHERE id=?", (case_id,)).fetchone()[0] == 1
            assert db.conn().execute("SELECT access FROM case_members WHERE case_id=? AND user_id=?",
                                     (case_id, u1)).fetchone()["access"] == "owner"
            assert db.conn().execute("SELECT COUNT(*) FROM documents WHERE id=?", (doc_id,)).fetchone()[0] == 1
            # Sessions, recovery codes and vault tokens are never readable in a user context.
            assert db.conn().execute("SELECT COUNT(*) FROM app_sessions").fetchone()[0] == 0
            with pytest.raises(psycopg.Error):
                db.conn().execute("SELECT private.token_get(CAST(? AS uuid))", (str(uuid.uuid4()),))
            # Audit entries cannot be written in someone else's name.
            with pytest.raises(psycopg.Error):
                with db.tx() as c:
                    c.execute("INSERT INTO audit_log(at, action, entity, actor_id) VALUES(?,?,?,?)",
                              (db.now(), "qa.forged", "qa", u2))
        # User 2 (same organisation, not on the case): the database shows nothing and refuses changes.
        with _as(u2):
            assert db.conn().execute("SELECT COUNT(*) FROM cases WHERE id=?", (case_id,)).fetchone()[0] == 0
            assert db.conn().execute("SELECT COUNT(*) FROM documents WHERE id=?", (doc_id,)).fetchone()[0] == 0
            assert db.conn().execute("SELECT COUNT(*) FROM case_members WHERE case_id=?", (case_id,)).fetchone()[0] == 0
            with db.tx() as c:
                assert c.execute("UPDATE cases SET title='hijacked' WHERE id=?", (case_id,)).rowcount == 0
                assert c.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount == 0
            with pytest.raises(psycopg.Error):   # cannot add themselves to the case
                with db.tx() as c:
                    c.execute("INSERT INTO case_members(case_id, user_id, access, created_at) VALUES(?,?,?,?)",
                              (case_id, u2, "owner", db.now()))
            with db.tx() as c:   # cannot promote themselves
                assert c.execute("UPDATE memberships SET role='admin' WHERE user_id=?", (u2,)).rowcount == 0
        # Anonymous callers see the public library only.
        with _as(None):
            assert db.conn().execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
            assert db.conn().execute("SELECT COUNT(*) FROM documents WHERE collection='case'").fetchone()[0] == 0
            assert db.conn().execute("SELECT COUNT(*) FROM documents WHERE collection='bare_act'").fetchone()[0] > 0
            assert db.conn().execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
        # The audit log is append-only even for the backend's internal context.
        with db.system():
            with pytest.raises(psycopg.Error):
                with db.tx() as c:
                    c.execute("UPDATE audit_log SET action='x' WHERE id = (SELECT MIN(id) FROM audit_log)")
        # After the owner adds user 2 as a client, user 2 sees the case but not internal notes.
        with _as(u1):
            with db.tx() as c:
                c.execute("INSERT INTO case_members(case_id, user_id, access, added_by, created_at) VALUES(?,?,?,?,?)",
                          (case_id, u2, "client", u1, db.now()))
                c.execute("INSERT INTO case_notes(id, case_id, author, content, created_at) VALUES(?,?,?,?,?)",
                          (f"note_qa{tag}", case_id, "QA", "internal strategy", db.now()))
        with _as(u2):
            assert db.conn().execute("SELECT COUNT(*) FROM cases WHERE id=?", (case_id,)).fetchone()[0] == 1
            assert db.conn().execute("SELECT COUNT(*) FROM documents WHERE id=?", (doc_id,)).fetchone()[0] == 1
            assert db.conn().execute("SELECT COUNT(*) FROM case_notes WHERE case_id=?", (case_id,)).fetchone()[0] == 0
            with db.tx() as c:
                assert c.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount == 0   # clients cannot delete
    finally:
        _cleanup([u1, u2], [case_id])
    with db.system():
        assert db.conn().execute("SELECT COUNT(*) FROM profiles WHERE email LIKE 'qa-rls%'").fetchone()[0] == 0


def test_live_auth_mfa_and_vault(live):
    email = f"qa-auth-{secrets.token_hex(4)}@example.com"
    password = f"qa {secrets.token_urlsafe(18)} pass"   # generated here, used only by this test
    user = sb.admin_create_user(email, password, "QA Auth", email_confirm=True)
    uid = user["id"]
    try:
        with db.system():
            accounts.create_invitation(db.DEFAULT_ORG, email, "advocate", invited_by="qa")
        c = TestClient(app_module().app, base_url=BASE)
        s = c.get("/api/session").json()
        c.headers["X-Nyaya-CSRF"] = s["csrf"]
        assert c.post("/api/auth/login", json={"email": email, "password": "definitely wrong 123"}).status_code == 401
        r = c.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "mfa_enroll" and r.json()["role"] == "guest"    # advocates must set up MFA first
        c.headers["X-Nyaya-CSRF"] = r.json()["csrf"]
        assert c.get("/api/cases").status_code == 401
        f = c.post("/api/auth/mfa/enroll").json()
        assert f["qr_code"].startswith("data:image/svg") and f["secret"]
        r = c.post("/api/auth/mfa/enroll/verify", json={"factor_id": f["factor_id"], "code": totp(f["secret"])})
        assert r.status_code == 200, r.text
        assert len(r.json()["recovery_codes"]) == 10 and r.json()["state"] == "active" and r.json()["role"] == "advocate"
        c.headers["X-Nyaya-CSRF"] = r.json()["csrf"]
        assert c.get("/api/cases").status_code == 200
        # The Supabase tokens are in Vault (encrypted by Supabase), referenced from the session row only.
        with db.system():
            row = db.conn().execute("SELECT token_ref, aal FROM app_sessions WHERE user_id=?", (uid,)).fetchone()
            assert row["token_ref"] and row["aal"] == "aal2"
            blob = db.conn().execute("SELECT private.token_get(CAST(? AS uuid)) AS v", (row["token_ref"],)).fetchone()["v"]
            assert blob and "refresh_token" in blob
        assert c.post("/api/auth/logout").status_code == 200
        # Second sign-in asks for the TOTP code (aal2 via Supabase's challenge/verify).
        c2 = TestClient(app_module().app, base_url=BASE)
        c2.headers["X-Nyaya-CSRF"] = c2.get("/api/session").json()["csrf"]
        r = c2.post("/api/auth/login", json={"email": email, "password": password})
        assert r.json()["state"] == "mfa_pending"
        c2.headers["X-Nyaya-CSRF"] = r.json()["csrf"]
        assert c2.post("/api/auth/mfa/verify", json={"code": "000000"}).status_code == 401
        time.sleep(31 - time.time() % 30)   # a fresh 30-second window, so the code differs from the one used above
        r = c2.post("/api/auth/mfa/verify", json={"code": totp(f["secret"])})
        assert r.status_code == 200 and r.json()["state"] == "active", r.text
        c2.headers["X-Nyaya-CSRF"] = r.json()["csrf"]
        assert c2.get("/api/account").json()["mfa"]["enabled"] is True
        assert c2.post("/api/auth/logout").status_code == 200
        with db.system():
            assert db.conn().execute("SELECT COUNT(*) FROM app_sessions WHERE user_id=?", (uid,)).fetchone()[0] == 0
    finally:
        try:
            sb.admin_delete_user(uid)
        finally:
            _cleanup([uid], [])


def test_live_job_queue_claims_once_and_is_private(live):
    """Postgres path of the durable queue: FOR UPDATE SKIP LOCKED claim, owner-only visibility under RLS."""
    from nyaya import jobs
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    tag = secrets.token_hex(3)
    ran = []

    @jobs.handler(f"qa_live_{tag}", authorize=None, timeout=60)
    def qa(params, ctx):
        ctx(1, 1, "done")
        ran.append(params["n"])
        return {"ok": True}
    with db.system():
        if db.conn().execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]:
            pytest.skip("real jobs are waiting in the queue; this test must not claim them")
    try:
        with db.system():
            with db.tx() as c:
                for uid, n in ((owner, 1), (other, 2)):
                    c.execute("INSERT INTO profiles(user_id, email, display_name, created_at, updated_at) VALUES(?,?,?,?,?)",
                              (uid, f"qa-job{n}-{tag}@example.com", f"QA {n}", db.now(), db.now()))
        j = jobs.submit(f"qa_live_{tag}", {"n": 7}, key=f"qa:{tag}", owner=owner)
        assert jobs.submit(f"qa_live_{tag}", {"n": 7}, key=f"qa:{tag}", owner=owner)["id"] == j["id"]   # deduplicated
        with _as(other):
            assert db.conn().execute("SELECT COUNT(*) FROM jobs WHERE id=?", (j["id"],)).fetchone()[0] == 0
        with _as(owner):
            assert db.conn().execute("SELECT COUNT(*) FROM jobs WHERE id=?", (j["id"],)).fetchone()[0] == 1
        row = jobs._claim("ai")
        assert row and row["id"] == j["id"] and row["status"] == "processing" and row["attempts"] == 1
        assert jobs._claim("ai") is None                               # already claimed: never handed out twice
        assert jobs.run_one(row) == "completed" and ran == [7]
        assert jobs.get(j["id"], owner)["status"] == "completed" and jobs.get(j["id"], other) is None
    finally:
        with db.system():
            with db.tx() as c:
                c.execute("DELETE FROM jobs WHERE dedupe_key=?", (f"qa:{tag}",))
                c.execute("DELETE FROM profiles WHERE user_id IN (?, ?)", (owner, other))
        jobs.HANDLERS.pop(f"qa_live_{tag}", None)


def app_module():
    from nyaya import server
    return server
