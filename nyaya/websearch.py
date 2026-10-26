"""Live web search for questions the local library cannot answer and for current legal news.

* Provider: Ollama's web-search API, with the OLLAMA_API_KEY the app already uses (no new service or key).
* Only the search query leaves the server — never case documents, the case dossier or the chat history — and it is
  pseudonymised first (Aadhaar, PAN, phone, e-mail… become placeholders).
* Results are ranked by source reliability: official (gov.in / nic.in, courts) and primary legal sources first, then
  legal publishers and established news organisations; social media and Q&A sites are dropped.
* Each result's text is cut to the passages that best match the query, and is later wrapped in the same
  injection-resistant <source> delimiters as library passages: a web page is data, never instructions.
* Bounded: one retry, a short timeout and a 10-minute cache, so a question never loops or floods the provider.
  Failure returns ([], reason) — callers must say that the web was not searched, never pretend it was.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from . import config, privacy

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

_cache: dict[tuple, tuple[float, list]] = {}
_lock = threading.Lock()
CACHE_SECONDS = 600
NO_RESULT = "no reliable web result was found"


@dataclass
class WebResult:
    title: str
    url: str
    domain: str
    text: str           # the best-matching passages of the page
    tier: int
    published: str | None = None
    extra: dict = field(default_factory=dict)


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
    for attempt in range(2):   # one retry, never more
        try:
            r = httpx.post(config.WEB_SEARCH_URL, headers=headers, json={"query": query, "max_results": max_results},
                           timeout=config.WEB_SEARCH_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
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


def search(query: str, *, max_results: int = 6, keep: int = 4) -> tuple[list[WebResult], str]:
    """(ranked results, '' or a short reason why the web could not be searched)."""
    if not enabled():
        return [], "web search is not configured"
    q = privacy.Redactor().redact(re.sub(r"\s+", " ", query or "").strip())[:300]
    if not q:
        return [], "empty query"
    key = (q.lower(), max_results)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return list(hit[1]), ""
    try:
        raw = _call(q, max_results)
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
        if t is None or url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", str(item.get("title") or domain)).strip()[:200]
        body = best_passages(str(item.get("content") or ""), q)
        if len(body) < 80:
            continue
        out.append(WebResult(title=title, url=url, domain=domain, text=body, tier=t))
    out.sort(key=lambda r: r.tier)   # stable: provider order within a tier
    out = out[:keep]
    with _lock:
        _cache[key] = (time.time(), out)
        if len(_cache) > 200:
            _cache.pop(next(iter(_cache)))
    return out, "" if out else NO_RESULT
