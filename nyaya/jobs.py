"""Tiny in-process job runner for long LLM tasks (translation, summaries) so the browser can poll progress
instead of holding a request open for minutes. Jobs live in memory; results are persisted by the task itself."""
from __future__ import annotations

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


class Progress:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def __call__(self, done: int, total: int, text: str = "") -> None:
        with _lock:
            j = _jobs.get(self.job_id)
            if j:
                j.update(done=done, total=total, message=text)


def submit(kind: str, fn: Callable[[Progress], dict], *, key: str | None = None) -> dict:
    """Run fn(progress) in the background. A job with the same key that is still running is reused."""
    with _lock:
        if key:
            for j in _jobs.values():
                if j["key"] == key and j["status"] == "running":
                    return dict(j)
        job_id = db.new_id("job_")
        _jobs[job_id] = {"id": job_id, "kind": kind, "key": key, "status": "running", "done": 0, "total": 0,
                         "message": "", "result": None, "error": None, "started": time.time()}

    def run():
        try:
            res = fn(Progress(job_id))
            with _lock:
                _jobs[job_id].update(status="done", result=res)
        except llm.LLMError as exc:
            with _lock:
                _jobs[job_id].update(status="error", error=str(exc))
        except ValueError as exc:  # user-facing validation problem
            with _lock:
                _jobs[job_id].update(status="error", error=str(exc))
        except Exception:
            log.exception("job %s failed", kind)
            with _lock:
                _jobs[job_id].update(status="error", error="The task failed unexpectedly. Please try again.")

    _pool.submit(run)
    return dict(_jobs[job_id])


def get(job_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None
