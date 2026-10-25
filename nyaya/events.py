"""Calendar: each lawyer's hearings, deadlines, follow-ups, submissions, meetings and tasks, with reminders.

Events are private to their owner (API checks here + row-level security in Postgres). An event may be linked to a
matter the owner can access. Case hearing dates (cases.next_hearing_date) appear on the calendar of everyone on the
case as read-only items, unless the person already has their own hearing event for that case and day.

Reminders are computed when an event is saved, in UTC, from the event's own time zone (IANA name), so "one day
before" means one day before in the place the hearing happens — whatever the server's clock is set to. A background
dispatcher (start_dispatcher) delivers due reminders every few seconds whether or not anyone has the app open: an
in-app notification (bell), and an e-mail when the person asked for one and SMTP is configured. Each reminder is
claimed atomically (FOR UPDATE SKIP LOCKED on Postgres), so two app instances never send it twice; a reminder whose
time had already passed when the event was saved is marked 'skipped' rather than sent late.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import authz, config, db, mailer

log = logging.getLogger("nyaya.events")

KINDS = ("hearing", "deadline", "follow_up", "submission", "meeting", "task")
PRIORITIES = ("low", "normal", "high")
STATUSES = ("scheduled", "completed", "cancelled")
# Reminder rules: offset from the event's start, or a fixed time on the day. An all-day item "starts" at 09:00.
RULES = ("at_time", "1h", "day_of", "1d", "7d")
ALL_DAY_START = time(9, 0)
DAY_OF_TIME = time(8, 0)
MAX_ATTEMPTS = 3
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class EventError(ValueError):
    """Invalid input (400); the message is safe to show."""


# ── Time ─────────────────────────────────────────────────────────────────────
def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or config.DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        raise EventError("Unknown time zone.") from None


def utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def start_of(event_date: str, event_time: str | None, tz: str) -> datetime:
    d = date.fromisoformat(event_date)
    t = time.fromisoformat(event_time) if event_time else ALL_DAY_START
    return datetime.combine(d, t, tzinfo=zone(tz))


def reminder_times(event_date: str, event_time: str | None, tz: str, rules: list[str]) -> dict[str, datetime]:
    start = start_of(event_date, event_time, tz)
    out: dict[str, datetime] = {}
    for r in rules:
        if r == "at_time":
            out[r] = start
        elif r == "1h":
            out[r] = start - timedelta(hours=1)
        elif r == "day_of":
            out[r] = min(start, datetime.combine(start.date(), DAY_OF_TIME, tzinfo=start.tzinfo))
        elif r == "1d":
            out[r] = start - timedelta(days=1)
        elif r == "7d":
            out[r] = start - timedelta(days=7)
    return out


# ── Validation ───────────────────────────────────────────────────────────────
def clean(data: dict, partial: bool = False) -> dict:
    out: dict = {}

    def has(k: str) -> bool:
        return k in data and (data[k] is not None or k in ("event_time", "case_id"))

    if not partial or has("title"):
        title = re.sub(r"[\x00-\x1f]", " ", str(data.get("title") or "")).strip()
        if not 1 <= len(title) <= 200:
            raise EventError("Give the task a title (up to 200 characters).")
        out["title"] = title
    if not partial or has("kind"):
        if (data.get("kind") or "task") not in KINDS:
            raise EventError("Unknown type of entry.")
        out["kind"] = data.get("kind") or "task"
    if not partial or has("event_date"):
        d = str(data.get("event_date") or "")
        try:
            if not _DATE.match(d):
                raise ValueError
            date.fromisoformat(d)
        except ValueError:
            raise EventError("Choose a valid date.") from None
        out["event_date"] = d
    if not partial or has("event_time"):
        t = data.get("event_time") or None
        if t is not None and not _TIME.match(str(t)):
            raise EventError("Time must be HH:MM (24-hour), or leave it empty for an all-day item.")
        out["event_time"] = t
    if not partial or has("timezone"):
        tz = str(data.get("timezone") or config.DEFAULT_TIMEZONE)
        zone(tz)
        out["timezone"] = tz
    if not partial or has("priority"):
        if (data.get("priority") or "normal") not in PRIORITIES:
            raise EventError("Priority must be low, normal or high.")
        out["priority"] = data.get("priority") or "normal"
    if not partial or has("description"):
        desc = str(data.get("description") or "").strip()
        if len(desc) > 4000:
            raise EventError("The description is too long (4000 characters at most).")
        out["description"] = desc
    if not partial or has("reminders"):
        rules = list(dict.fromkeys(data.get("reminders") or []))
        if any(r not in RULES for r in rules):
            raise EventError("Unknown reminder option.")
        out["reminders"] = rules
    if not partial or has("email_reminders"):
        out["email_reminders"] = bool(data.get("email_reminders"))
    if has("case_id"):
        out["case_id"] = data.get("case_id") or None
    if has("status"):
        if data["status"] not in STATUSES:
            raise EventError("Unknown status.")
        out["status"] = data["status"]
    return out


# ── Records ──────────────────────────────────────────────────────────────────
def _row(r) -> dict:
    d = dict(r)
    try:
        d["reminders"] = json.loads(d.get("reminders") or "[]")
    except ValueError:
        d["reminders"] = []
    d["email_reminders"] = bool(d.get("email_reminders"))
    return d


def _with_details(events: list[dict]) -> list[dict]:
    if not events:
        return events
    ids = [e["id"] for e in events]
    ph = ",".join("?" * len(ids))
    rem: dict[str, list] = {}
    for r in db.conn().execute(f"SELECT event_id, rule, channel, status, remind_at_utc, sent_at, error FROM event_reminders "
                               f"WHERE event_id IN ({ph}) ORDER BY remind_at_utc", ids):
        rem.setdefault(r["event_id"], []).append({k: r[k] for k in ("rule", "channel", "status", "remind_at_utc", "sent_at", "error")})
    case_ids = sorted({e["case_id"] for e in events if e.get("case_id")})
    titles: dict[str, str] = {}
    if case_ids:
        titles = {r["id"]: r["title"] for r in db.conn().execute(
            f"SELECT id, title FROM cases WHERE id IN ({','.join('?' * len(case_ids))})", case_ids)}
    for e in events:
        e["reminder_status"] = rem.get(e["id"], [])
        e["case_title"] = titles.get(e.get("case_id") or "", "")
        e["source"] = "own"
    return events


def get_own(p: authz.Principal, event_id: str) -> dict:
    r = db.conn().execute("SELECT * FROM calendar_events WHERE id=? AND owner_id=?", (event_id, p.user_id)).fetchone()
    if not r:
        raise authz.NotFound("Event not found")
    return _with_details([_row(r)])[0]


def _check_case(p: authz.Principal, case_id: str | None) -> None:
    if case_id:
        try:
            authz.require_case(p, case_id, "case.read")
        except authz.NotFound:
            raise authz.NotFound("Case not found") from None


def _schedule(c, ev: dict) -> None:
    """(Re)create the pending reminders of an event. Sent reminders stay as a record; pending ones are replaced."""
    c.execute("DELETE FROM event_reminders WHERE event_id=? AND status IN ('pending', 'skipped', 'failed')", (ev["id"],))
    if ev["status"] != "scheduled":
        return
    channels = ["in_app"] + (["email"] if ev["email_reminders"] else [])
    now = utcnow()
    done = {(r["rule"], r["channel"]) for r in c.execute(
        "SELECT rule, channel FROM event_reminders WHERE event_id=?", (ev["id"],)).fetchall()}
    for rule, at in reminder_times(ev["event_date"], ev["event_time"], ev["timezone"], ev["reminders"]).items():
        for ch in channels:
            if (rule, ch) in done:
                continue
            past = at <= now
            reason = "The reminder time had already passed when this was saved." if past else \
                "E-mail is not configured on this server." if ch == "email" and not mailer.configured() else None
            c.execute("INSERT INTO event_reminders(id, event_id, owner_id, rule, channel, remind_at_utc, status, error) "
                      "VALUES(?,?,?,?,?,?,?,?)", (db.new_id("rem_"), ev["id"], ev["owner_id"], rule, ch, utc_str(at),
                                                  "skipped" if reason else "pending", reason))


def create(p: authz.Principal, data: dict) -> dict:
    ev = clean(data)
    case_id = data.get("case_id") or None
    _check_case(p, case_id)
    now = db.now()
    ev.update(id=db.new_id("evt_"), org_id=p.org_id, owner_id=p.user_id, case_id=case_id, status="scheduled",
              created_at=now, updated_at=now,
              starts_at_utc=utc_str(start_of(ev["event_date"], ev["event_time"], ev["timezone"])))
    with db.tx() as c:
        c.execute("INSERT INTO calendar_events(id, org_id, owner_id, case_id, kind, title, description, event_date, "
                  "event_time, timezone, starts_at_utc, priority, status, reminders, email_reminders, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ev["id"], ev["org_id"], ev["owner_id"], case_id, ev["kind"], ev["title"], ev["description"],
                   ev["event_date"], ev["event_time"], ev["timezone"], ev["starts_at_utc"], ev["priority"], "scheduled",
                   json.dumps(ev["reminders"]), int(ev["email_reminders"]), now, now))
        _schedule(c, ev)
    db.audit("calendar.create", "event", ev["id"], kind=ev["kind"], case_id=case_id)
    return get_own(p, ev["id"])


def update(p: authz.Principal, event_id: str, data: dict) -> dict:
    cur = get_own(p, event_id)
    changes = clean(data, partial=True)
    if "case_id" in changes and changes["case_id"] != cur.get("case_id"):
        _check_case(p, changes["case_id"])
    ev = {**cur, **changes}
    ev["starts_at_utc"] = utc_str(start_of(ev["event_date"], ev["event_time"], ev["timezone"]))
    if changes.get("status") == "completed" and cur["status"] != "completed":
        ev["completed_at"] = db.now()
    elif changes.get("status") == "scheduled":
        ev["completed_at"] = None
    ev["updated_at"] = db.now()
    with db.tx() as c:
        c.execute("UPDATE calendar_events SET case_id=?, kind=?, title=?, description=?, event_date=?, event_time=?, "
                  "timezone=?, starts_at_utc=?, priority=?, status=?, reminders=?, email_reminders=?, completed_at=?, "
                  "updated_at=? WHERE id=? AND owner_id=?",
                  (ev.get("case_id"), ev["kind"], ev["title"], ev["description"], ev["event_date"], ev["event_time"],
                   ev["timezone"], ev["starts_at_utc"], ev["priority"], ev["status"], json.dumps(ev["reminders"]),
                   int(ev["email_reminders"]), ev.get("completed_at"), ev["updated_at"], event_id, p.user_id))
        rescheduled = (ev["event_date"], ev["event_time"], ev["timezone"]) != \
            (cur["event_date"], cur["event_time"], cur["timezone"])
        if rescheduled:   # a new date means every reminder is due again
            c.execute("DELETE FROM event_reminders WHERE event_id=?", (event_id,))
        _schedule(c, ev)
    db.audit("calendar.update", "event", event_id, fields=sorted(changes), status=ev["status"])
    return get_own(p, event_id)


def delete(p: authz.Principal, event_id: str) -> None:
    get_own(p, event_id)
    with db.tx() as c:
        c.execute("DELETE FROM event_reminders WHERE event_id=?", (event_id,))
        c.execute("DELETE FROM calendar_events WHERE id=? AND owner_id=?", (event_id, p.user_id))
    db.audit("calendar.delete", "event", event_id)


def _case_hearings(p: authz.Principal, start: str, end: str, own: list[dict]) -> list[dict]:
    """Next hearing dates of the caller's matters, as read-only items (unless they made their own entry)."""
    ids = authz.accessible_case_ids(p)
    if not ids:
        return []
    covered = {(e["case_id"], e["event_date"]) for e in own if e["kind"] == "hearing" and e.get("case_id")}
    rows = db.conn().execute(
        f"SELECT id, title, court, next_hearing_date FROM cases WHERE id IN ({','.join('?' * len(ids))}) "
        "AND next_hearing_date >= ? AND next_hearing_date <= ?", (*ids, start, end))
    return [{"id": f"case:{r['id']}", "source": "case", "kind": "hearing", "title": r["title"], "case_id": r["id"],
             "case_title": r["title"], "court": r["court"] or "", "event_date": r["next_hearing_date"], "event_time": None,
             "priority": "high", "status": "scheduled", "readonly": True}
            for r in rows if (r["id"], r["next_hearing_date"]) not in covered]


def list_range(p: authz.Principal, start: str, end: str) -> list[dict]:
    for d in (start, end):
        if not _DATE.match(d or ""):
            raise EventError("Dates must be YYYY-MM-DD.")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 62:
        raise EventError("Ask for at most two months at a time.")
    own = _with_details([_row(r) for r in db.conn().execute(
        "SELECT * FROM calendar_events WHERE owner_id=? AND event_date >= ? AND event_date <= ? "
        "ORDER BY event_date, COALESCE(event_time, '00:00')", (p.user_id, start, end))])
    items = own + _case_hearings(p, start, end, own)
    return sorted(items, key=lambda e: (e["event_date"], e.get("event_time") or "", e["title"]))


def upcoming(p: authz.Principal, tz: str | None = None, days: int = 30, limit: int = 12) -> dict:
    today = datetime.now(zone(tz)).date()
    end = (today + timedelta(days=max(1, min(days, 366)))).isoformat()
    own = _with_details([_row(r) for r in db.conn().execute(
        "SELECT * FROM calendar_events WHERE owner_id=? AND status='scheduled' AND event_date >= ? AND event_date <= ? "
        "ORDER BY event_date, COALESCE(event_time, '00:00') LIMIT ?", (p.user_id, today.isoformat(), end, limit))])
    items = own + _case_hearings(p, today.isoformat(), end, own)
    items.sort(key=lambda e: (e["event_date"], e.get("event_time") or "", e["title"]))
    overdue = db.conn().execute(
        "SELECT COUNT(*) AS n FROM calendar_events WHERE owner_id=? AND status='scheduled' AND event_date < ?",
        (p.user_id, today.isoformat())).fetchone()["n"]
    return {"items": items[:limit], "overdue": overdue, "today": today.isoformat()}


# ── Reminder delivery ────────────────────────────────────────────────────────
KIND_LABEL = {"hearing": "Court hearing", "deadline": "Deadline", "follow_up": "Follow-up",
              "submission": "Document submission", "meeting": "Meeting", "task": "Task"}


def _claim(limit: int) -> list[dict]:
    now = utcnow()
    now_s, lock = utc_str(now), utc_str(now + timedelta(minutes=5))
    due = "(status='pending' OR (status='sending' AND locked_until < ?)) AND remind_at_utc <= ?"
    if db.backend() == "postgres":
        rows = db.conn().execute(
            "UPDATE event_reminders SET status='sending', attempts=attempts+1, locked_until=? WHERE id IN "
            f"(SELECT id FROM event_reminders WHERE {due} ORDER BY remind_at_utc LIMIT ? FOR UPDATE SKIP LOCKED) "
            "RETURNING *", (lock, now_s, now_s, limit)).fetchall()
        return [dict(r) for r in rows]
    with db.tx() as c:
        ids = [r["id"] for r in c.execute(f"SELECT id FROM event_reminders WHERE {due} ORDER BY remind_at_utc LIMIT ?",
                                          (now_s, now_s, limit)).fetchall()]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        c.execute(f"UPDATE event_reminders SET status='sending', attempts=attempts+1, locked_until=? WHERE id IN ({ph})",
                  (lock, *ids))
        return [dict(r) for r in c.execute(f"SELECT * FROM event_reminders WHERE id IN ({ph})", ids).fetchall()]


def _finish(rid: str, status: str, error: str | None = None, retry_at: datetime | None = None) -> None:
    with db.tx() as c:
        if retry_at:
            c.execute("UPDATE event_reminders SET status='pending', remind_at_utc=?, error=?, locked_until=NULL WHERE id=?",
                      (utc_str(retry_at), error, rid))
        else:
            c.execute("UPDATE event_reminders SET status=?, error=?, sent_at=?, locked_until=NULL WHERE id=?",
                      (status, error, utc_str(utcnow()) if status == "sent" else None, rid))


def _when(ev: dict) -> str:
    d = date.fromisoformat(ev["event_date"]).strftime("%a %d %b %Y")
    return f"{d}, {ev['event_time']} ({ev['timezone']})" if ev.get("event_time") else f"{d} (all day)"


def _deliver(rem: dict) -> None:
    ev = db.conn().execute("SELECT * FROM calendar_events WHERE id=?", (rem["event_id"],)).fetchone()
    if not ev or ev["status"] != "scheduled":
        _finish(rem["id"], "skipped", "The entry was completed, cancelled or deleted.")
        return
    ev = dict(ev)
    case = db.conn().execute("SELECT title FROM cases WHERE id=?", (ev["case_id"],)).fetchone() if ev["case_id"] else None
    title = f"{KIND_LABEL.get(ev['kind'], 'Task')}: {ev['title']}"
    body = f"{_when(ev)}" + (f" · {case['title']}" if case else "") + (f"\n{ev['description'][:300]}" if ev["description"] else "")
    if rem["channel"] == "in_app":
        db.notify("reminder", title, body, f"#/calendar?event={ev['id']}", dedupe_key=f"reminder:{rem['id']}",
                  recipients=[ev["owner_id"]])
        _finish(rem["id"], "sent")
        return
    prof = db.conn().execute("SELECT email, status FROM profiles WHERE user_id=?", (ev["owner_id"],)).fetchone()
    if not prof or prof["status"] != "active" or not prof["email"]:
        _finish(rem["id"], "skipped", "The account has no active e-mail address.")
        return
    try:
        mailer.send(prof["email"], f"Reminder — {title}",
                    f"{title}\n{body}\n\nOpen your NyaySutra calendar to see or change this entry.\n"
                    f"(You asked for e-mail reminders for this entry.)")
        _finish(rem["id"], "sent")
    except mailer.MailError as exc:
        if rem["attempts"] < MAX_ATTEMPTS:
            _finish(rem["id"], "pending", str(exc), retry_at=utcnow() + timedelta(minutes=5 * rem["attempts"]))
        else:
            _finish(rem["id"], "failed", str(exc))


def dispatch_due(limit: int = 50) -> int:
    """Deliver every reminder that is due. Returns how many were handled."""
    n = 0
    with db.system():
        for rem in _claim(limit):
            try:
                _deliver(rem)
            except Exception:   # one bad reminder must not stop the others
                log.exception("reminder %s failed", rem["id"])
                _finish(rem["id"], "pending" if rem["attempts"] < MAX_ATTEMPTS else "failed", "Internal error",
                        retry_at=utcnow() + timedelta(minutes=5) if rem["attempts"] < MAX_ATTEMPTS else None)
            n += 1
    return n


_stop = threading.Event()
_thread: threading.Thread | None = None


def start_dispatcher() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def loop() -> None:
        while not _stop.wait(config.REMINDER_TICK_SECONDS):
            try:
                dispatch_due()
            except Exception:
                log.exception("reminder dispatch failed")
    _thread = threading.Thread(target=loop, name="reminders", daemon=True)
    _thread.start()


def stop_dispatcher() -> None:
    _stop.set()
