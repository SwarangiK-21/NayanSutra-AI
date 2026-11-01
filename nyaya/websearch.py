"""Live web search for questions the local library cannot answer and for current legal news.

* Provider: Ollama's web-search API, with the OLLAMA_API_KEY the app already uses (no new service or key).
* Only the search query leaves the server — never case documents, the case dossier or the chat history — and it is
  pseudonymised first (Aadhaar, PAN, phone, e-mail… become placeholders).
* Results are ranked by source reliability: official (gov.in / nic.in, courts) and primary legal sources first, then
  legal publishers and established news organisations; social media, Q&A sites and unknown websites are dropped.
* Each result's text is cut to the passages that best match the query, and is later wrapped in the same
  injection-resistant <source> delimiters as library passages: a web page is data, never instructions.
* Bounded: a short timeout, one retry for a server error, and a cache that survives restarts (a day; an hour for news
  and other recent questions), so a question never loops or floods the provider.
* The allowance is protected: "limit reached" (HTTP 429) is never retried and pauses live searches for a while, and a
  daily budget stops them before the allowance runs out. Cached results are still served during both.
  Failure returns ([], reason) — callers must say that the web was not searched, never pretend it was.
* The cache and counters live in a small local file (HOME_DIR/web_search.db), not in Supabase. Queries are stored only
  as a hash; the results are public web pages.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import httpx

from . import clock, config, privacy

log = logging.getLogger(__name__)

# Lower tier = more authoritative. Unlisted domains are tier 4; BLOCKED domains are never used.
_TIERS: list[tuple[int, re.Pattern]] = [
    (0, re.compile(r"(?:^|\.)(?:sci\.gov\.in|main\.sci\.gov\.in|indiacode\.nic\.in|indiacode\.gov\.in|egazette\.gov\.in|"
                   r"ecourts\.gov\.in|nia\.gov\.in|cbi\.gov\.in|mha\.gov\.in|lawmin\.gov\.in|legislative\.gov\.in|"
                   r"pib\.gov\.in|[a-z0-9-]+\.gov\.in|[a-z0-9-]+\.nic\.in)$")),
    (1, re.compile(r"(?:^|\.)(?:indiankanoon\.org|scobserver\.in|prsindia\.org|livelaw\.in|barandbench\.com|"
                   r"verdictum\.in|scconline\.com|lawcommissionofindia\.nic\.in)$")),
    (2, re.compile(r"(?:^|\.)(?:thehindu\.com|indianexpress\.com|hindustantimes\.com|ndtv\.com|timesofindia\.indiatimes\.com|"
                   r"deccanherald\.com|thewire\.in|scroll\.in|theprint\.in|livemint\.com|economictimes\.indiatimes\.com|"
                   r"business-standard\.com|indiatoday\.in|reuters\.com|bbc\.com|bbc\.co\.uk|apnews\.com|"
                   r"britannica\.com|wikipedia\.org)$")),
]
_BLOCKED = re.compile(r"(?:^|\.)(?:facebook\.com|instagram\.com|x\.com|twitter\.com|youtube\.com|tiktok\.com|reddit\.com|"
                      r"quora\.com|pinterest\.com|linkedin\.com|telegram\.me|t\.me|medium\.com|blogspot\.com)$")
TIER_LABEL = {0: "official source", 1: "legal publisher", 2: "news / reference", 4: "other website"}
# Sources below this tier ("other website": unknown blogs, aggregators, test sites) are never handed to the model.
RELIABLE_TIER = 2

_lock = threading.Lock()
NO_RESULT = "no reliable web result was found"
LIMIT_REACHED = "the web search limit has been reached for now"
BUDGET_USED = "today's web search budget has been used"


@dataclass
class WebResult:
    title: str
    url: str
    domain: str
    text: str           # the best-matching passages of the page
    tier: int
    published: str | None = None
    extra: dict = field(default_factory=dict)


class RateLimited(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__("HTTP 429")
        self.retry_after = retry_after


class Blocked(RuntimeError):
    """A live search was not attempted (paused or over budget); the message is the reason shown to the user."""


def enabled() -> bool:
    return bool(config.WEB_SEARCH_ENABLED and config.OLLAMA_API_KEY and config.WEB_SEARCH_URL)


def tier(domain: str) -> int | None:
    if _BLOCKED.search(domain):
        return None
    return next((t for t, p in _TIERS if p.search(domain)), 4)


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "in", "on", "and", "or", "to", "for", "is", "are", "was", "what", "about", "tell",
         "me", "with", "by", "its", "their", "india", "indian"}


def best_passages(text: str, query: str, size: int = 1400, keep: int = 2) -> str:
    """The `keep` windows of `text` that share the most words with the query, in document order."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= size * keep:
        return text
    q = {w for w in _WORD.findall(query.lower()) if w not in _STOP}
    windows = [text[i:i + size] for i in range(0, min(len(text), 400_000), size)]
    scored = sorted(range(len(windows)), key=lambda i: (-len(q & set(_WORD.findall(windows[i].lower()))), i))
    picked = sorted(set([0] + scored[:keep]))[:keep + 1]   # the page's opening usually says what the page is
    return " … ".join(windows[i] for i in picked)


def _call(query: str, max_results: int) -> list[dict]:
    headers = {"Authorization": f"Bearer {config.OLLAMA_API_KEY}", "User-Agent": config.HTTP_USER_AGENT}
    last: Exception | None = None
    for attempt in range(2):   # one retry, never more, and none for "limit reached": it would only waste the allowance
        try:
            r = httpx.post(config.WEB_SEARCH_URL, headers=headers, json={"query": query, "max_results": max_results},
                           timeout=config.WEB_SEARCH_TIMEOUT)
            if r.status_code == 429:
                raise RateLimited(_retry_after(r.headers.get("Retry-After")))
            if r.status_code in (500, 502, 503, 504) and attempt == 0:
                time.sleep(1.0)
                continue
            r.raise_for_status()
            data = r.json()
            return (data.get("results") or []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            if attempt == 0:
                time.sleep(0.5)
    raise RuntimeError(type(last).__name__ if last else "web search failed")


def _retry_after(value: str | None) -> int:
    default = config.WEB_SEARCH_PAUSE_MINUTES * 60
    return min(int(value), 86400) if value and value.isdigit() else default


@contextlib.contextmanager
def _store():
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, contextlib.closing(sqlite3.connect(config.HOME_DIR / "web_search.db", timeout=5)) as c, c:
        c.execute("CREATE TABLE IF NOT EXISTS results(key TEXT PRIMARY KEY, saved_at REAL, items TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS state(name TEXT PRIMARY KEY, value REAL)")
        yield c


def reset() -> None:
    """Forget cached results, the pause and today's count."""
    with _store() as c:
        c.execute("DELETE FROM results")
        c.execute("DELETE FROM state")


def _cached(key: str, fresh: bool) -> list[WebResult] | None:
    long, short = config.WEB_CACHE_HOURS * 3600, config.WEB_CACHE_FRESH_HOURS * 3600
    try:
        with _store() as c:
            row = c.execute("SELECT saved_at, items FROM results WHERE key=?", (key,)).fetchone()
    except sqlite3.Error as exc:
        log.warning("Web search cache unavailable: %s", exc)
        return None
    if not row:
        return None
    items = [WebResult(**i) for i in json.loads(row[1])]
    # "nothing found" is rechecked as soon as recent news is: the web may have caught up
    return items if time.time() - row[0] < (short if fresh or not items else long) else None


def _remember(key: str, items: list[WebResult]) -> None:
    now = time.time()
    try:
        with _store() as c:
            c.execute("INSERT OR REPLACE INTO results(key, saved_at, items) VALUES(?,?,?)",
                      (key, now, json.dumps([asdict(i) for i in items], ensure_ascii=False)))
            c.execute("DELETE FROM results WHERE saved_at < ?", (now - config.WEB_CACHE_HOURS * 3600,))
    except sqlite3.Error as exc:
        log.warning("Web search cache unavailable: %s", exc)


def _paused() -> bool:
    try:
        with _store() as c:
            row = c.execute("SELECT value FROM state WHERE name='paused_until'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] > time.time())


def _pause(seconds: int) -> None:
    log.warning("Web search limit reached: pausing live searches for %d minutes", seconds // 60)
    with contextlib.suppress(sqlite3.Error), _store() as c:
        c.execute("INSERT OR REPLACE INTO state(name, value) VALUES('paused_until', ?)", (time.time() + seconds,))


def _spend() -> bool:
    """Count one live search against today's budget; False when it is used up. 0 = no budget."""
    limit = config.WEB_SEARCH_DAILY_LIMIT
    if limit <= 0:
        return True
    day = f"spent:{clock.today()}"
    try:
        with _store() as c:
            row = c.execute("SELECT value FROM state WHERE name=?", (day,)).fetchone()
            spent = int(row[0]) if row else 0
            if spent >= limit:
                return False
            c.execute("DELETE FROM state WHERE name LIKE 'spent:%' AND name<>?", (day,))
            c.execute("INSERT OR REPLACE INTO state(name, value) VALUES(?,?)", (day, spent + 1))
    except sqlite3.Error as exc:
        log.warning("Web search budget unavailable: %s", exc)
        return True
    if spent + 1 == int(limit * 0.8):
        log.warning("Web search budget: %d of %d used today", spent + 1, limit)
    elif spent + 1 == limit:
        log.warning("Web search budget used up (%d today): live searches stop until tomorrow", limit)
    return True


def _live(query: str, max_results: int) -> list[dict]:
    """One provider call, unless paused or over budget. This is the only way a search reaches the provider."""
    if _paused():
        raise Blocked(LIMIT_REACHED)
    if not _spend():
        raise Blocked(BUDGET_USED)
    try:
        return _call(query, max_results)
    except RateLimited as exc:
        _pause(exc.retry_after)
        raise Blocked(LIMIT_REACHED) from exc


def search(query: str, *, max_results: int = 8, keep: int = 4, max_tier: int = RELIABLE_TIER,
           fresh: bool = False) -> tuple[list[WebResult], str]:
    """(ranked results, '' or a short reason why the web could not be searched). `fresh`: the answer depends on recent
    events, so only a recent cached result will do."""
    if not enabled():
        return [], "web search is not configured"
    q = privacy.Redactor().redact(re.sub(r"\s+", " ", query or "").strip())[:300]
    if not q:
        return [], "empty query"
    key = hashlib.sha256(f"{q.lower()}|{max_results}|{max_tier}|{keep}".encode()).hexdigest()
    hit = _cached(key, fresh)
    if hit is not None:
        return hit, "" if hit else NO_RESULT
    try:
        raw = _live(q, max_results)
    except Blocked as exc:
        return [], str(exc)
    except Exception as exc:
        log.warning("Web search failed: %s", exc)
        return [], "the web search service could not be reached"
    out: list[WebResult] = []
    seen: set[str] = set()
    for item in raw:
        url = str(item.get("url") or "")
        parts = urlsplit(url)
        domain = (parts.hostname or "").lower().removeprefix("www.")
        t = tier(domain) if parts.scheme in ("http", "https") and domain else None
        if t is None or t > max_tier or url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", str(item.get("title") or domain)).strip()[:200]
        body = best_passages(str(item.get("content") or ""), q, keep=3)   # enough of the page to support what it says
        if len(body) < 80:
            continue
        out.append(WebResult(title=title, url=url, domain=domain, text=body, tier=t))
    out.sort(key=lambda r: r.tier)   # stable: provider order within a tier
    out = out[:keep]
    _remember(key, out)
    return out, "" if out else NO_RESULT
