"""Data retention: how long NyaySutra keeps personal data, and the daily job that deletes what has expired.

The periods are configuration (config.RETENTION_*; defaults are the project owner's choice, "keep data longer") and
are shown to users on the Privacy page (policy()). docs/DATA_RETENTION.md is the written policy.

What the job does, as internal system work, once a day:
  * chats and their messages not used for RETENTION_CHAT_MONTHS                     -> deleted
  * research (search) history older than RETENTION_SEARCH_MONTHS                     -> deleted
  * accounts not used for RETENTION_WARN_MONTHS                                       -> warned (in-app, and e-mail
                                                                                         when e-mail is configured)
  * accounts not used for RETENTION_INACTIVE_MONTHS, warned at least 30 days earlier -> deleted with their personal
    data (chats, history, calendar, preferences, notifications, sessions); cases whose only member they were are
    deleted with their documents; shared cases stay with the other members
  * audit-log entries older than RETENTION_AUDIT_YEARS                                -> deleted
  * operational records: expired sign-in sessions (30 days), finished background jobs (90 days), notifications
    (read: 12 months, any: 24 months), integration sync log (12 months), sign-in throttle entries (30 days)
Deleting a case, document, chat or search in the app removes it immediately (there is no recycle bin).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from . import config, db

log = logging.getLogger("nyaya.retention")
WARNING_NOTICE_DAYS = 30   # a warned account is deleted no sooner than this after the warning
_stop = threading.Event()
_thread: threading.Thread | None = None


def _months(n: int) -> timedelta:
    return timedelta(days=round(n * 30.44))


def _before(delta: timedelta, now: datetime) -> str:
    return (now - delta).isoformat(timespec="seconds")


def policy() -> list[dict]:
    """The retention periods, for the Privacy page."""
    def period(months: int) -> str:
        return "kept until you delete it" if months <= 0 else f"{months} months"
    return [
        {"data": "Chats with the research assistant (questions and answers)", "kept": period(config.RETENTION_CHAT_MONTHS),
         "from": "the last time the chat was used"},
        {"data": "Research (search) history", "kept": period(config.RETENTION_SEARCH_MONTHS), "from": "the search"},
        {"data": "Your account and personal workspace", "kept": period(config.RETENTION_INACTIVE_MONTHS),
         "from": f"your last sign-in (you are warned after {config.RETENTION_WARN_MONTHS} months)"},
        {"data": "Cases, case documents and drafts", "kept": "until you or your administrator delete them",
         "from": "deleted immediately when you delete them; deleted with an inactive account that is their only member"},
        {"data": "Security audit log (who did what, never document contents)",
         "kept": f"{config.RETENTION_AUDIT_YEARS} years" if config.RETENTION_AUDIT_YEARS > 0 else "kept",
         "from": "the event"},
        {"data": "Sign-in sessions", "kept": "30 days", "from": "the session's expiry"},
        {"data": "Notifications", "kept": "12 months once read, 24 months otherwise", "from": "the notification"},
        {"data": "Background job records", "kept": "90 days", "from": "the job's end"},
    ]


def _delete(sql: str, args: tuple, dry_run: bool) -> int:
    if dry_run:
        count_sql = "SELECT COUNT(*) AS n FROM " + sql.split(" FROM ", 1)[1]
        return db.conn().execute(count_sql, args).fetchone()["n"]
    with db.tx() as c:
        return c.execute(sql, args).rowcount or 0


def delete_account(user_id: str) -> dict:
    """Delete one account and its personal data. Cases whose only member it was go with it (documents included)."""
    from . import cases as cases_mod
    from . import supabase_auth
    out = {"cases": 0}
    with db.system():
        sole = [r["case_id"] for r in db.conn().execute(
            "SELECT case_id FROM case_members WHERE user_id=? AND case_id NOT IN "
            "(SELECT case_id FROM case_members WHERE user_id<>?)", (user_id, user_id)).fetchall()]
        for cid in sole:
            out["cases"] += bool(cases_mod.delete_case(cid))
        with db.tx() as c:
            c.execute("DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE user_id=?)", (user_id,))
            for sql in ("DELETE FROM chats WHERE user_id=?", "DELETE FROM search_history WHERE user_id=?",
                        "DELETE FROM event_reminders WHERE owner_id=?", "DELETE FROM calendar_events WHERE owner_id=?",
                        "DELETE FROM user_prefs WHERE user_id=?", "DELETE FROM notifications WHERE user_id=?",
                        "DELETE FROM app_sessions WHERE user_id=?", "DELETE FROM recovery_codes WHERE user_id=?",
                        "DELETE FROM case_members WHERE user_id=?", "DELETE FROM memberships WHERE user_id=?",
                        "DELETE FROM profiles WHERE user_id=?"):
                c.execute(sql, (user_id,))
    if config.auth_mode() == "supabase" and config.SUPABASE_SECRET_KEY:
        try:
            supabase_auth.admin_delete_user(user_id)
        except Exception as exc:   # the app data is gone; the sign-in record is retried on the next run
            log.warning("Could not delete the sign-in record of an inactive account: %s", type(exc).__name__)
            out["auth_pending"] = True
    db.audit("account.deleted_inactive", "user", user_id, cases_deleted=out["cases"])
    return out


def _warn(user_id: str, email: str, deadline: datetime) -> None:
    from . import mailer
    when = deadline.strftime("%d %B %Y")
    title = "Your NyaySutra account will be deleted for inactivity"
    body = (f"You have not used NyaySutra for {config.RETENTION_WARN_MONTHS} months. Under our data-retention policy "
            f"your account and personal data will be deleted on or after {when}. Sign in before then to keep it.")
    db.notify("system", title, body, "#/privacy", recipients=[user_id], dedupe_key=f"retention-warning:{user_id}")
    if mailer.configured() and email:
        try:
            mailer.send(email, title, body)
        except Exception as exc:
            log.warning("Retention warning e-mail failed: %s", type(exc).__name__)


def run(now: datetime | None = None, dry_run: bool = False) -> dict:
    """Apply the retention periods once. Returns what was (or, with dry_run, would be) deleted or warned."""
    now = now or datetime.now()
    res: dict = {}
    with db.system():
        if config.RETENTION_CHAT_MONTHS > 0:
            cut = _before(_months(config.RETENTION_CHAT_MONTHS), now)
            res["messages"] = _delete("DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE updated_at < ?)",
                                      (cut,), dry_run)
            res["chats"] = _delete("DELETE FROM chats WHERE updated_at < ?", (cut,), dry_run)
        if config.RETENTION_SEARCH_MONTHS > 0:
            res["search_history"] = _delete("DELETE FROM search_history WHERE created_at < ?",
                                            (_before(_months(config.RETENTION_SEARCH_MONTHS), now),), dry_run)
        res["sessions"] = _delete("DELETE FROM app_sessions WHERE expires_at < ? OR revoked_at < ?",
                                  (_before(timedelta(days=30), now),) * 2, dry_run)
        res["jobs"] = _delete("DELETE FROM jobs WHERE status IN ('completed','partial','failed','cancelled','dead') AND "
                              "updated_at < ?", (_before(timedelta(days=90), now),), dry_run)
        res["notifications"] = _delete("DELETE FROM notifications WHERE (read_at IS NOT NULL AND created_at < ?) OR "
                                       "created_at < ?", (_before(_months(12), now), _before(_months(24), now)), dry_run)
        res["sync_log"] = _delete("DELETE FROM sync_log WHERE started_at < ?", (_before(_months(12), now),), dry_run)
        res["auth_throttle"] = _delete("DELETE FROM auth_throttle WHERE window_start < ?",
                                       (_before(timedelta(days=30), now),), dry_run)
        if config.RETENTION_AUDIT_YEARS > 0:
            cut = _before(timedelta(days=round(365.25 * config.RETENTION_AUDIT_YEARS)), now)
            if dry_run or db.backend() != "postgres":
                res["audit_log"] = _delete("DELETE FROM audit_log WHERE at < ?", (cut,), dry_run)
            else:   # the app role cannot delete audit entries; this narrow function can, for system work only
                res["audit_log"] = db.conn().execute("SELECT private.purge_audit_log(?) AS n", (cut,)).fetchone()["n"]
        res["accounts_warned"] = res["accounts_deleted"] = 0
        if config.RETENTION_INACTIVE_MONTHS > 0:
            warn_cut = _before(_months(config.RETENTION_WARN_MONTHS), now)
            del_cut = _before(_months(config.RETENTION_INACTIVE_MONTHS), now)
            notice_cut = _before(timedelta(days=WARNING_NOTICE_DAYS), now)
            for r in db.conn().execute("SELECT user_id, email FROM profiles WHERE last_active_at < ? AND "
                                       "retention_warned_at IS NULL", (warn_cut,)).fetchall():
                res["accounts_warned"] += 1
                if not dry_run:
                    deadline = max(now + timedelta(days=WARNING_NOTICE_DAYS),
                                   now + (_months(config.RETENTION_INACTIVE_MONTHS) - _months(config.RETENTION_WARN_MONTHS)))
                    _warn(r["user_id"], r["email"], deadline)
                    with db.tx() as c:
                        c.execute("UPDATE profiles SET retention_warned_at=? WHERE user_id=?",
                                  (now.isoformat(timespec="seconds"), r["user_id"]))
            for r in db.conn().execute("SELECT user_id FROM profiles WHERE last_active_at < ? AND "
                                       "retention_warned_at IS NOT NULL AND retention_warned_at < ?",
                                       (del_cut, notice_cut)).fetchall():
                res["accounts_deleted"] += 1
                if not dry_run:
                    delete_account(r["user_id"])
    if not dry_run and any(res.values()):
        db.audit("retention.run", "system", None, **{k: v for k, v in res.items() if v})
    log.info("Retention %s: %s", "dry run" if dry_run else "run", {k: v for k, v in res.items() if v} or "nothing due")
    return res


def mark_active(user_id: str, when: datetime | None = None) -> None:
    """Record that a user used the app today (at most one write per user per day) and clear any inactivity warning."""
    today = (when or datetime.now()).date().isoformat()
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE profiles SET last_active_at=?, retention_warned_at=NULL WHERE user_id=? AND "
                      "(last_active_at IS NULL OR last_active_at < ?)", (today + "T00:00:00", user_id, today))


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def loop() -> None:
        first = True
        while not _stop.wait(300 if first else config.RETENTION_TICK_HOURS * 3600):   # first run 5 min after start
            first = False
            try:
                run()
            except Exception:
                log.exception("Retention run failed")
    _thread = threading.Thread(target=loop, name="retention", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
