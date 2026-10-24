"""Durable background jobs (database-backed queue with worker threads).

Every long task — document ingestion, translation, summaries, drafting, provision suggestions — is a row in `jobs`
with an explicit state:

    queued → processing → completed | partial | failed | cancelled | dead
               └─ transient error → queued again later (exponential back-off with jitter), at most max_attempts

* Survives restarts: a job left "processing" by a crash is picked up again when its lock expires.
* Safe with several app instances: workers claim rows with FOR UPDATE SKIP LOCKED (Postgres).
* Owned: only the user who started a job can read, cancel or retry it. Before running, the worker re-checks that the
  owner still has access to everything the job touches (permissions can change between queueing and running).
* Bounded: a time limit per job, a limit on attempts, and a dead-letter state with a notification instead of
  endless retries. Cancellation and time limits are checked whenever the task reports progress.
* Resumable: a task may save a checkpoint (e.g. translated parts) and continue from it on the next attempt.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import httpx

from . import config, db, llm

log = logging.getLogger(__name__)

STATES = ("queued", "processing", "partial", "completed", "failed", "cancelled", "dead")
ACTIVE = ("queued", "processing")
POLL_SECONDS = 2.0
BACKOFF_BASE = 5.0      # seconds; doubles per attempt, randomised ("full jitter"), capped
BACKOFF_CAP = 300.0
_WORKER_ID = f"{socket.gethostname()}:{id(object())}"


class JobCancelled(Exception):
    """The owner cancelled the job."""


class JobTimeout(Exception):
    """The job ran longer than its time limit."""


class Transient(Exception):
    """Raised by a task for a temporary problem worth retrying (network, provider overload)."""


class Rejected(Exception):
    """Raised by a task (or the worker) for a permanent problem; the message is shown to the owner."""


@dataclass
class Handler:
    fn: Callable[[dict, "Ctx"], dict]
    lane: str = "ai"
    authorize: Callable | None = None          # (principal, params) -> None, raises authz.Denied/NotFound
    max_attempts: int = 3
    timeout: int = 900
    on_finish: Callable | None = None          # (params, state, error) -> None, e.g. mark a document failed
    on_retry: Callable | None = None           # (params) -> None, e.g. put a document back into 'processing'


HANDLERS: dict[str, Handler] = {}


def handler(kind: str, **kw):
    def deco(fn):
        HANDLERS[kind] = Handler(fn=fn, **kw)
        return fn
    return deco


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def backoff(attempt: int) -> float:
    """Exponential back-off with full jitter: uniform(0, min(cap, base * 2^(attempt-1)))."""
    return random.uniform(0, min(BACKOFF_CAP, BACKOFF_BASE * 2 ** max(0, attempt - 1)))


def is_transient(exc: BaseException) -> bool:
    from . import storage
    if isinstance(exc, (Transient, JobTimeout, TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    if isinstance(exc, llm.LLMError):
        return exc.transient
    if isinstance(exc, storage.StorageError):
        return "unreachable" in str(exc).lower()
    try:
        import psycopg
        if isinstance(exc, psycopg.OperationalError):
            return True
    except ImportError:
        pass
    return False


# ── Rows ─────────────────────────────────────────────────────────────────────
_PUBLIC = ("id", "kind", "case_id", "status", "progress_done", "progress_total", "message", "result", "error",
           "attempts", "max_attempts", "created_at", "started_at", "finished_at", "next_run_at", "cancel_requested")


def public(row: dict | None) -> dict | None:
    """What the owner sees (no parameters, no lock details)."""
    if not row:
        return None
    out = {k: row.get(k) for k in _PUBLIC}
    for k in ("result",):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    out["done"], out["total"] = out.pop("progress_done"), out.pop("progress_total")
    out["cancel_requested"] = bool(out.get("cancel_requested"))
    return out


def _row(job_id: str) -> dict | None:
    with db.system():
        r = db.conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return db.row_to_dict(r, ("params", "checkpoint")) if r else None


# ── Submitting and managing jobs ─────────────────────────────────────────────
_wake: dict[str, threading.Event] = {"ai": threading.Event(), "ingest": threading.Event()}


def submit(kind: str, params: dict, *, key: str | None = None, owner: str | None = None, org_id: str | None = None,
           case_id: str | None = None) -> dict:
    """Queue a job (or return the owner's identical job that is still queued/running)."""
    h = HANDLERS.get(kind)
    if not h:
        raise ValueError(f"Unknown job kind: {kind}")
    now = db.now()
    with db.system():
        if key:
            existing = db.conn().execute(
                "SELECT * FROM jobs WHERE dedupe_key=? AND COALESCE(owner_id,'')=? AND status IN ('queued','processing') "
                "ORDER BY created_at DESC LIMIT 1", (key, owner or "")).fetchone()
            if existing:
                return public(db.row_to_dict(existing, ("params", "checkpoint")))
        job_id = db.new_id("job_")
        with db.tx() as c:
            c.execute("INSERT INTO jobs(id, kind, lane, owner_id, org_id, case_id, dedupe_key, status, params, "
                      "max_attempts, next_run_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (job_id, kind, h.lane, owner, org_id, case_id, key, "queued",
                       json.dumps(params, ensure_ascii=False, default=str), h.max_attempts, now, now, now))
    _wake[h.lane].set()
    return public(_row(job_id))


def get(job_id: str, owner: str | None = None) -> dict | None:
    """The job, only if it belongs to `owner` — a job id alone never gives access to someone else's results."""
    row = _row(job_id)
    if not row or row.get("owner_id") != owner:
        return None
    return public(row)


def list_for(owner: str, limit: int = 50, kind: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM jobs WHERE owner_id=?", [owner]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    with db.system():
        rows = db.conn().execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, max(1, min(limit, 200))))
        return [public(db.row_to_dict(r, ("params", "checkpoint"))) for r in rows]


def _finish(job_id: str, status: str, *, result: dict | None = None, error: str | None = None,
            message: str | None = None) -> None:
    now = db.now()
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE jobs SET status=?, result=?, error=?, message=COALESCE(?, message), finished_at=?, "
                      "updated_at=?, locked_by=NULL, locked_until=NULL WHERE id=?",
                      (status, json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                       error, message, now, now, job_id))


def cancel(job_id: str, owner: str | None) -> dict | None:
    """Queued jobs stop at once; a running job stops at its next progress report."""
    row = _row(job_id)
    if not row or row.get("owner_id") != owner:
        return None
    if row["status"] == "queued":
        _finish(job_id, "cancelled", message="Cancelled before it started.")
        _after(row, "cancelled", None)
    elif row["status"] == "processing":
        with db.system():
            with db.tx() as c:
                c.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (db.now(), job_id))
    return public(_row(job_id))


def retry(job_id: str, owner: str | None) -> dict | None:
    """Run a failed, dead or cancelled job again (the owner's permissions are checked again when it runs)."""
    row = _row(job_id)
    if not row or row.get("owner_id") != owner:
        return None
    if row["status"] not in ("failed", "dead", "cancelled"):
        raise ValueError("Only failed, stopped or cancelled jobs can be run again.")
    h = HANDLERS.get(row["kind"])
    if h and h.on_retry:
        with db.system():
            h.on_retry(row.get("params") or {})
    now = db.now()
    with db.system():
        with db.tx() as c:
            c.execute("UPDATE jobs SET status='queued', attempts=0, error=NULL, result=NULL, cancel_requested=0, "
                      "next_run_at=?, finished_at=NULL, message='Queued again', updated_at=? WHERE id=?", (now, now, job_id))
    _wake[row["lane"]].set()
    return public(_row(job_id))


def _after(row: dict, state: str, error: str | None) -> None:
    h = HANDLERS.get(row["kind"])
    if h and h.on_finish:
        try:
            with db.system():
                h.on_finish(row.get("params") or {}, state, error)
        except Exception:
            log.exception("job %s: finish hook failed", row["id"])


# ── Context given to a running task ──────────────────────────────────────────
@dataclass
class Ctx:
    job_id: str
    deadline: float
    checkpoint: dict = field(default_factory=dict)
    _last_write: float = 0.0
    _lock_seconds: int = 900

    def check(self) -> None:
        with db.system():
            r = db.conn().execute("SELECT cancel_requested FROM jobs WHERE id=?", (self.job_id,)).fetchone()
        if r and r["cancel_requested"]:
            raise JobCancelled()
        if time.monotonic() > self.deadline:
            raise JobTimeout()

    def __call__(self, done: int, total: int, text: str = "") -> None:
        """Progress report — also the point where cancellation and the time limit take effect."""
        now = time.monotonic()
        if now - self._last_write >= 0.5 or done >= total:
            self._last_write = now
            with db.system():
                with db.tx() as c:
                    c.execute("UPDATE jobs SET progress_done=?, progress_total=?, message=?, updated_at=?, locked_until=? "
                              "WHERE id=?", (int(done), int(total), str(text)[:300], db.now(),
                                             _ts(datetime.now() + timedelta(seconds=self._lock_seconds)), self.job_id))
        self.check()

    def save_checkpoint(self, data: dict) -> None:
        self.checkpoint = dict(data)
        with db.system():
            with db.tx() as c:
                c.execute("UPDATE jobs SET checkpoint=?, updated_at=? WHERE id=?",
                          (json.dumps(self.checkpoint, ensure_ascii=False), db.now(), self.job_id))


# ── Worker ───────────────────────────────────────────────────────────────────
def _claim(lane: str) -> dict | None:
    now = datetime.now()
    lock = _ts(now + timedelta(seconds=max(h.timeout for h in HANDLERS.values()) + 120 if HANDLERS else 1020))
    with db.system():
        if db.backend() == "postgres":
            r = db.conn().execute(
                "UPDATE jobs SET status='processing', attempts=attempts+1, locked_by=?, locked_until=?, "
                "started_at=COALESCE(started_at, ?), updated_at=? WHERE id = (SELECT id FROM jobs WHERE lane=? AND "
                "status='queued' AND next_run_at <= ? ORDER BY next_run_at, created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING *", (_WORKER_ID, lock, _ts(now), _ts(now), lane, _ts(now))).fetchone()
            return db.row_to_dict(r, ("params", "checkpoint")) if r else None
        with db.tx() as c:
            r = c.execute("SELECT id FROM jobs WHERE lane=? AND status='queued' AND next_run_at <= ? "
                          "ORDER BY next_run_at, created_at LIMIT 1", (lane, _ts(now))).fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status='processing', attempts=attempts+1, locked_by=?, locked_until=?, "
                      "started_at=COALESCE(started_at, ?), updated_at=? WHERE id=? AND status='queued'",
                      (_WORKER_ID, lock, _ts(now), _ts(now), r["id"]))
            row = c.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone()
        return db.row_to_dict(row, ("params", "checkpoint"))


def recover_stale() -> int:
    """Jobs whose worker died (lock expired) go back to the queue — or to 'dead' if out of attempts."""
    now = db.now()
    with db.system():
        rows = [db.row_to_dict(r, ("params", "checkpoint")) for r in db.conn().execute(
            "SELECT * FROM jobs WHERE status='processing' AND (locked_until IS NULL OR locked_until < ?)", (now,))]
    for row in rows:
        if row["attempts"] >= row["max_attempts"]:
            _finish(row["id"], "dead", error="The job stopped unexpectedly too many times.")
            _after(row, "dead", "stopped unexpectedly")
        else:
            with db.system():
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='queued', locked_by=NULL, locked_until=NULL, next_run_at=?, "
                              "message='Resuming after an interruption', updated_at=? WHERE id=?", (now, now, row["id"]))
    return len(rows)


def _identity_for(row: dict, h: Handler) -> db.Identity:
    """Run as the owner, with permissions re-checked now; internal jobs run in the system context."""
    if h.authorize is None:
        return db.Identity(system=True, actor=row.get("owner_id"), org_id=row.get("org_id"))
    from . import accounts, authz
    p = accounts.principal_for_user(row.get("owner_id"))
    if p is None:
        raise Rejected("The account that started this job is no longer active.")
    try:
        h.authorize(p, row.get("params") or {})
    except (authz.Denied, authz.NotFound) as exc:
        raise Rejected(f"Access to the item was withdrawn: {exc}") from None
    return p.identity()


def run_one(row: dict) -> str:
    """Execute a claimed job and record its outcome. Returns the final (or re-queued) state."""
    h = HANDLERS.get(row["kind"])
    if h is None:
        _finish(row["id"], "failed", error="Unknown job type.")
        return "failed"
    if row.get("cancel_requested"):
        _finish(row["id"], "cancelled", message="Cancelled.")
        _after(row, "cancelled", None)
        return "cancelled"
    ctx = Ctx(job_id=row["id"], deadline=time.monotonic() + h.timeout, checkpoint=row.get("checkpoint") or {},
              _lock_seconds=h.timeout + 120)
    try:
        ident = _identity_for(row, h)
        with db.as_identity(ident):
            with llm.track():
                result = h.fn(row.get("params") or {}, ctx) or {}
                used = llm.calls()
        if any(c.get("fallback") for c in used):
            result = {**result, "fallback_model_used": True}
        state = "partial" if result.get("partial") else "completed"
        _finish(row["id"], state, result=result, message="Finished with some parts missing" if state == "partial" else "Done")
        _after(row, state, None)
        return state
    except JobCancelled:
        _finish(row["id"], "cancelled", message="Cancelled.")
        _after(row, "cancelled", None)
        return "cancelled"
    except Exception as exc:  # classify: retry later, give up, or permanent failure
        from . import authz
        permanent = (Rejected, ValueError, PermissionError, authz.Denied, authz.NotFound)
        known = (*permanent, llm.LLMError, Transient)
        # Unknown errors are retried a few times (they may be temporary) and then stopped — never looped forever.
        transient = is_transient(exc) or not isinstance(exc, (*permanent, llm.LLMError))
        msg = str(exc) if isinstance(exc, known) else \
            "The task timed out." if isinstance(exc, JobTimeout) else "The task failed unexpectedly."
        if not isinstance(exc, (*known, JobTimeout)):
            log.exception("job %s (%s) failed", row["id"], row["kind"])
        if transient and row["attempts"] < row["max_attempts"]:
            wait = backoff(row["attempts"])
            with db.system():
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='queued', error=?, message=?, next_run_at=?, locked_by=NULL, "
                              "locked_until=NULL, updated_at=? WHERE id=?",
                              (msg, f"Temporary problem — retrying in {int(wait) + 1} s (attempt {row['attempts'] + 1} "
                                    f"of {row['max_attempts']})", _ts(datetime.now() + timedelta(seconds=wait)), db.now(),
                               row["id"]))
            return "queued"
        state = "dead" if transient else "failed"
        _finish(row["id"], state, error=msg)
        _after(row, state, msg)
        if state == "dead" and row.get("owner_id"):
            db.notify("system", "A background task could not be completed",
                      f"{row['kind'].capitalize()} failed {row['attempts']} times and was stopped: {msg} "
                      "You can run it again from the Jobs page.", "#/jobs", recipients=[row["owner_id"]],
                      dedupe_key=f"dead:{row['id']}")
        return state


class Worker(threading.Thread):
    def __init__(self, lane: str, stop: threading.Event, n: int):
        super().__init__(daemon=True, name=f"job-{lane}-{n}")
        self.lane, self.stop_event = lane, stop

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                row = _claim(self.lane)
            except Exception:
                log.exception("job queue unavailable")
                row = None
                self.stop_event.wait(POLL_SECONDS * 5)
            if row is None:
                _wake[self.lane].wait(POLL_SECONDS)
                _wake[self.lane].clear()
                continue
            run_one(row)


_stop = threading.Event()
_workers: list[Worker] = []


def start_workers() -> None:
    """Start the worker threads (concurrency per lane is bounded by configuration)."""
    global _stop
    stop_workers()
    _stop = threading.Event()
    try:
        recover_stale()
    except Exception:
        log.exception("could not recover interrupted jobs")
    for lane, n in (("ingest", config.INGEST_WORKERS), ("ai", config.JOB_WORKERS)):
        for i in range(max(1, n)):
            w = Worker(lane, _stop, i)
            w.start()
            _workers.append(w)


def stop_workers(timeout: float = 5.0) -> None:
    _stop.set()
    for ev in _wake.values():
        ev.set()
    for w in _workers:
        w.join(timeout)
    _workers.clear()
    for ev in _wake.values():
        ev.clear()


def stats() -> dict:
    """Queue health for the status page: backlog per state and lane."""
    with db.system():
        rows = db.conn().execute("SELECT lane, status, COUNT(*) AS n FROM jobs GROUP BY lane, status").fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["lane"], {})[r["status"]] = r["n"]
    return out
