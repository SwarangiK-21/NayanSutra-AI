"""Tiny in-process job runner for long LLM tasks (translation, summaries) so the browser can poll progress
instead of holding a request open for minutes. Jobs live in memory; results are persisted by the task itself.

A job belongs to the user who started it: it runs under that user's identity (so database rules apply to it as
they would to the request) and only that user can read its progress and result."""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import db, llm

log = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
MAX_JOBS = 500   # finished jobs beyond this are forgotten (oldest first)


class Progress:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def __call__(self, done: int, total: int, text: str = "") -> None:
        with _lock:
            j = _jobs.get(self.job_id)
            if j:
                j.update(done=done, total=total, message=text)


def _prune() -> None:
    done = [k for k, j in _jobs.items() if j["status"] != "running"]
    for k in done[: max(0, len(_jobs) - MAX_JOBS)]:
        _jobs.pop(k, None)


def submit(kind: str, fn: Callable[[Progress], dict], *, key: str | None = None, owner: str | None = None) -> dict:
    """Run fn(progress) in the background as the calling user. A running job with the same key and owner is reused."""
    with _lock:
        if key:
            for j in _jobs.values():
                if j["key"] == key and j["owner"] == owner and j["status"] == "running":
                    return {k: v for k, v in j.items() if k not in ("key", "owner")}
        _prune()
        job_id = db.new_id("job_")
        _jobs[job_id] = {"id": job_id, "kind": kind, "key": key, "owner": owner, "status": "running", "done": 0,
                         "total": 0, "message": "", "result": None, "error": None, "started": time.time()}
    ctx = contextvars.copy_context()   # carries the caller's database identity into the worker thread

    def run():
        try:
            res = fn(Progress(job_id))
            with _lock:
                _jobs[job_id].update(status="done", result=res)
        except llm.LLMError as exc:
            with _lock:
                _jobs[job_id].update(status="error", error=str(exc))
        except (ValueError, PermissionError) as exc:  # user-facing validation / authorization problem
            with _lock:
                _jobs[job_id].update(status="error", error=str(exc))
        except Exception:
            log.exception("job %s failed", kind)
            with _lock:
                _jobs[job_id].update(status="error", error="The task failed unexpectedly. Please try again.")

    _pool.submit(ctx.run, run)
    return {k: v for k, v in _jobs[job_id].items() if k not in ("key", "owner")}


def get(job_id: str, owner: str | None = None) -> dict | None:
    """The job, only if it belongs to `owner` (a job id alone never gives access to someone else's results)."""
    with _lock:
        j = _jobs.get(job_id)
        if not j or j.get("owner") != owner:
            return None
        return dict(j)
