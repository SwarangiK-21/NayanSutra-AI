"""Current date and time — from the clock, never from the language model.

The server clock is read in the platform's time zone (DEFAULT_TIMEZONE, Asia/Kolkata = IST), independent of the time
zone the server machine itself is set to. It is cross-checked against the Date header of a trusted HTTPS server
(TIME_CHECK_URL); if the server clock is off by more than CLOCK_TOLERANCE the network time is used instead, and if no
check was possible the answer says that the time could not be verified.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

from . import config

log = logging.getLogger(__name__)

CLOCK_TOLERANCE = 120          # seconds of server-clock drift tolerated before the network time is used
CHECK_EVERY = 15 * 60          # the offset is re-measured at most this often
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "offset": None, "source": ""}


@dataclass
class Now:
    dt: datetime            # aware, in the platform time zone
    verified: bool          # cross-checked against a network time source
    source: str             # where the time came from (for the answer and the trace)
    tz_name: str


def _measure() -> tuple[float | None, str]:
    """(network time − server time) in seconds, from the Date header of TIME_CHECK_URL; (None, '') if unavailable."""
    if not config.TIME_CHECK_URL:
        return None, ""
    try:
        t0 = time.time()
        r = httpx.head(config.TIME_CHECK_URL, timeout=5, follow_redirects=False,
                       headers={"User-Agent": config.HTTP_USER_AGENT})
        t1 = time.time()
        header = r.headers.get("date")
        if not header:
            return None, ""
        net = parsedate_to_datetime(header).timestamp()
        return net - (t0 + t1) / 2, httpx.URL(config.TIME_CHECK_URL).host
    except Exception as exc:  # network down, bad header — the answer says the time is unverified
        log.info("Time check failed: %s", type(exc).__name__)
        return None, ""


def _offset() -> tuple[float | None, str]:
    with _lock:
        if time.time() - _cache["at"] > CHECK_EVERY or _cache["offset"] is None:
            off, src = _measure()
            # Failures are retried on the next question, successes are cached.
            _cache.update(at=time.time() if off is not None else 0.0, offset=off, source=src)
        return _cache["offset"], _cache["source"]


def now() -> Now:
    tz = ZoneInfo(config.DEFAULT_TIMEZONE)
    off, src = _offset()
    utc = datetime.now(timezone.utc)
    if off is None:
        return Now(utc.astimezone(tz), False, "server clock (not verified)", config.DEFAULT_TIMEZONE)
    if abs(off) > CLOCK_TOLERANCE:   # the server clock is wrong — trust the network time
        return Now((utc + timedelta(seconds=off)).astimezone(tz), True, f"network time from {src}",
                   config.DEFAULT_TIMEZONE)
    return Now(utc.astimezone(tz), True, f"server clock, checked against {src}", config.DEFAULT_TIMEZONE)


def today() -> str:
    """ISO date in the platform time zone (no network call)."""
    return datetime.now(ZoneInfo(config.DEFAULT_TIMEZONE)).date().isoformat()


def stamp() -> str:
    """One line for prompts: the model is told the date and time instead of guessing them."""
    d = datetime.now(ZoneInfo(config.DEFAULT_TIMEZONE))
    return f"{d:%A, %d %B %Y}, {d:%I:%M %p} {_abbr(d)}"


def _abbr(d: datetime) -> str:
    return "IST" if config.DEFAULT_TIMEZONE == "Asia/Kolkata" else (d.tzname() or config.DEFAULT_TIMEZONE)


_DAYS = {"hi": ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"],
         "mr": ["सोमवार", "मंगळवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]}


def answer(kind: str = "time", lang: str = "en") -> tuple[str, Now]:
    """Fixed-format reply to "what time / what date is it?" (kind = 'time' | 'date')."""
    n = now()
    d = n.dt
    tz = _abbr(d)
    utc = d.astimezone(timezone.utc)
    clock = f"{d:%I:%M %p}".lstrip("0")
    date_s = f"{d:%A}, {d.day} {d:%B %Y}"
    if lang in _DAYS:
        day = _DAYS[lang][d.weekday()]
        date_s = f"{day}, {d:%d-%m-%Y}"
        main = (f"आत्ता **{clock} {tz}** आहे — **{date_s}**." if lang == "mr" else
                f"अभी **{clock} {tz}** है — **{date_s}**.") if kind == "time" else \
               (f"आजची तारीख **{date_s}** आहे ({tz})." if lang == "mr" else f"आज की तारीख **{date_s}** है ({tz}).")
    else:
        main = (f"It is **{clock} {tz}** on **{date_s}**." if kind == "time"
                else f"Today is **{date_s}** ({tz}); the time is {clock}.")
    note = (f"\n\n*Time zone: {n.tz_name} ({tz}, UTC+05:30) · UTC {utc:%H:%M} · source: {n.source}.*"
            if config.DEFAULT_TIMEZONE == "Asia/Kolkata"
            else f"\n\n*Time zone: {n.tz_name} · UTC {utc:%H:%M} · source: {n.source}.*")
    if not n.verified:
        note += ("\n\n⚠️ I could not verify this against an internet time source, so it is the server's own clock "
                 "and may be slightly off.")
    return main + note, n
