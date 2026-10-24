"""Minimal Ollama client (cloud via API key, or a local server) with streaming,
JSON mode, retries and user-friendly errors. One pooled HTTP connection is
reused for every call, which keeps per-request latency low.

This is the single point where text leaves the server for the language model, so personal identifiers are
pseudonymised here (privacy.redact_messages) and restored in the reply.

Resilience, all bounded:
* retries on overload / server errors / network errors with exponential back-off and full jitter;
* a circuit breaker per model: after repeated failures the model is skipped for a short time instead of making every
  request wait for timeouts;
* an approved fallback model (LLM_FALLBACK_MODEL, same provider and the same already-authorised prompt) used when the
  main model fails or its circuit is open — every call records which model answered, so answers can say so.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

import httpx

from . import config, privacy

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Message is safe to show to the user. `transient` = worth retrying later; `fallback_ok` = another model may work."""

    def __init__(self, message: str, *, transient: bool = False, fallback_ok: bool | None = None):
        super().__init__(message)
        self.transient = transient
        self.fallback_ok = transient if fallback_ok is None else fallback_ok


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            headers = {"User-Agent": "NyaySutra-AI/1.0"}
            if config.OLLAMA_API_KEY:
                headers["Authorization"] = f"Bearer {config.OLLAMA_API_KEY}"
            _client = httpx.Client(base_url=config.OLLAMA_HOST, headers=headers,
                                   timeout=httpx.Timeout(config.LLM_TIMEOUT, connect=15),
                                   limits=httpx.Limits(max_keepalive_connections=8, max_connections=16))
        return _client


# ── Which models answered (per request / job) ────────────────────────────────
_calls: ContextVar[list | None] = ContextVar("nyaya_llm_calls", default=None)


@contextmanager
def track():
    """Record every model call made inside this block (see calls())."""
    token = _calls.set([])
    try:
        yield
    finally:
        _calls.reset(token)


def calls() -> list[dict]:
    return list(_calls.get() or [])


def _record(**kw) -> None:
    lst = _calls.get()
    if lst is not None:
        lst.append(kw)


# ── Circuit breaker ──────────────────────────────────────────────────────────
class Breaker:
    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            return time.monotonic() >= self.open_until

    def success(self) -> None:
        with self.lock:
            self.failures, self.open_until = 0, 0.0

    def failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= config.LLM_BREAKER_FAILURES:
                self.open_until = time.monotonic() + config.LLM_BREAKER_SECONDS
                self.failures = 0
                log.warning("LLM circuit opened for %ss after repeated failures", config.LLM_BREAKER_SECONDS)


_breakers: dict[str, Breaker] = {}


def breaker(model: str) -> Breaker:
    return _breakers.setdefault(model, Breaker())


def breaker_state() -> dict[str, str]:
    return {m: ("open" if not b.allow() else "closed") for m, b in _breakers.items()}


def _models(model: str | None) -> list[str]:
    """The model to use, then the approved fallback for it (only the main answer model has one)."""
    primary = model or config.LLM_MODEL
    fb = config.LLM_FALLBACK_MODEL
    return [primary, fb] if primary == config.LLM_MODEL and fb and fb != primary else [primary]


def _sleep(attempt: int) -> None:
    time.sleep(random.uniform(0, min(8.0, 1.0 * 2 ** attempt)))   # full jitter, capped


def _payload(messages: list[dict], model: str | None, stream: bool, json_mode: bool, temperature: float,
             max_tokens: int | None) -> dict:
    model = model or config.LLM_MODEL
    p: dict = {"model": model, "messages": messages, "stream": stream,
               "options": {"temperature": temperature}}
    if max_tokens:
        p["options"]["num_predict"] = max_tokens
    if json_mode:
        p["format"] = "json"
    if model.startswith("gpt-oss"):
        p["think"] = config.LLM_THINK or "low"
    return p


def _raise_for(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    try:
        detail = r.json().get("error", "")
    except ValueError:
        detail = r.text[:200]
    detail = str(detail)
    log.warning("Ollama error %s: %s", r.status_code, detail[:200])
    if r.status_code in (401, 403):
        raise LLMError("The Ollama API key was rejected. Check OLLAMA_API_KEY in .env.")
    if r.status_code == 404 or "not found" in detail.lower():
        raise LLMError(f"Model not available on your Ollama plan: {detail[:120]}", fallback_ok=True)
    if r.status_code == 429 or "limit" in detail.lower():
        raise LLMError("Ollama usage limit reached for now. Please wait a few minutes and try again.", transient=True)
    raise LLMError(f"The language model service returned an error ({r.status_code}).", transient=r.status_code >= 500)


_RETRY_STATUS = (429, 500, 502, 503, 504)


def _chat_once(body: dict, model: str) -> str:
    """One model, bounded retries with jittered back-off."""
    last: LLMError | None = None
    for attempt in range(config.LLM_RETRIES + 1):
        try:
            r = _http().post("/api/chat", json={**body, "model": model})
            if r.status_code in _RETRY_STATUS and attempt < config.LLM_RETRIES:
                _sleep(attempt)
                continue
            _raise_for(r)
            return (r.json().get("message") or {}).get("content", "")
        except LLMError as exc:
            last = exc
            break
        except httpx.TimeoutException:
            last = LLMError("The language model took too long to respond. Please try again.", transient=True)
        except httpx.HTTPError:
            last = LLMError("Could not reach the language model service. Check your internet connection.", transient=True)
        if attempt < config.LLM_RETRIES:
            _sleep(attempt)
    raise last or LLMError("The language model returned no answer.", transient=True)


def chat(messages: list[dict], *, model: str | None = None, json_mode: bool = False, temperature: float = 0.1,
         max_tokens: int | None = None, info: dict | None = None) -> str:
    """`info`, if given, receives which model answered and whether the fallback was used (for traces/labels)."""
    info = {} if info is None else info
    messages, red = privacy.redact_messages(messages)
    body = _payload(messages, model, False, json_mode, temperature, max_tokens)
    order = _models(model)
    last: LLMError | None = None
    for i, m in enumerate(order):
        b = breaker(m)
        if not b.allow():
            last = LLMError("The language model is temporarily unavailable. Please try again shortly.", transient=True)
            _record(model=m, ok=False, fallback=i > 0, skipped="circuit_open")
            continue
        t0 = time.monotonic()
        try:
            out = _chat_once(body, m)
        except LLMError as exc:
            if exc.transient:
                b.failure()
            _record(model=m, ok=False, fallback=i > 0, ms=int(1000 * (time.monotonic() - t0)))
            last = exc
            if not exc.fallback_ok:
                break
            continue
        b.success()
        _record(model=m, ok=True, fallback=i > 0, ms=int(1000 * (time.monotonic() - t0)))
        info.update(model=m, fallback=i > 0, ok=True)
        return red.restore(out)
    info.update(ok=False, models_tried=order)
    raise last or LLMError("The language model returned no answer.", transient=True)


def stream(messages: list[dict], *, model: str | None = None, temperature: float = 0.1,
           max_tokens: int | None = None, info: dict | None = None) -> Iterator[str]:
    """Yield answer tokens as they arrive (reasoning tokens are skipped), with identifiers restored.

    Falls back to the approved fallback model only if the main model fails before producing any text; a failure in
    the middle of an answer is raised so the caller can mark the answer as incomplete."""
    messages, red = privacy.redact_messages(messages)
    yield from red.stream(_stream_with_fallback(messages, model, temperature, max_tokens, {} if info is None else info))


def _stream_with_fallback(messages, model, temperature, max_tokens, info: dict) -> Iterator[str]:
    order = _models(model)
    last: LLMError | None = None
    for i, m in enumerate(order):
        b = breaker(m)
        if not b.allow():
            last = LLMError("The language model is temporarily unavailable. Please try again shortly.", transient=True)
            _record(model=m, ok=False, fallback=i > 0, skipped="circuit_open")
            continue
        produced = False
        t0 = time.monotonic()
        try:
            for tok in _stream(messages, m, temperature, max_tokens):
                produced = True
                yield tok
        except LLMError as exc:
            if exc.transient:
                b.failure()
            _record(model=m, ok=False, fallback=i > 0, partial=produced, ms=int(1000 * (time.monotonic() - t0)))
            info.update(model=m, fallback=i > 0, ok=False, partial=produced)
            if produced or not exc.fallback_ok:
                raise
            last = exc
            continue
        b.success()
        _record(model=m, ok=True, fallback=i > 0, ms=int(1000 * (time.monotonic() - t0)))
        info.update(model=m, fallback=i > 0, ok=True, partial=False)
        return
    info.update(ok=False, models_tried=order)
    raise last or LLMError("The language model returned no answer.", transient=True)


def _stream(messages: list[dict], model: str, temperature: float, max_tokens: int | None) -> Iterator[str]:
    body = _payload(messages, model, True, False, temperature, max_tokens)
    for attempt in range(config.LLM_RETRIES + 1):
        try:
            with _http().stream("POST", "/api/chat", json=body) as r:
                if r.status_code in _RETRY_STATUS and attempt < config.LLM_RETRIES:
                    r.read()
                    _sleep(attempt)
                    continue
                if r.status_code >= 400:
                    r.read()
                    _raise_for(r)
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    if evt.get("error"):
                        raise LLMError(f"Model error: {str(evt['error'])[:200]}", transient=True)
                    tok = (evt.get("message") or {}).get("content")
                    if tok:
                        yield tok
                    if evt.get("done"):
                        return
            return
        except httpx.TimeoutException as exc:
            raise LLMError("The language model took too long to respond. Please try again.", transient=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError("Lost connection to the language model service.", transient=True) from exc


def chat_json(messages: list[dict], *, model: str | None = None, default: dict | None = None,
              info: dict | None = None) -> dict:
    """JSON-mode call with tolerant parsing; returns `default` on any failure (info['ok'] tells which)."""
    try:
        raw = chat(messages, model=model, json_mode=True, temperature=0.0, info=info)
    except LLMError as exc:
        log.warning("JSON call failed: %s", exc)
        return dict(default or {})
    return parse_json(raw, default)


def parse_json(raw: str, default: dict | None = None) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    for candidate in (raw, raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else ""):
        if not candidate:
            continue
        try:
            v = json.loads(candidate)
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    return dict(default or {})


def ping() -> tuple[bool, str]:
    try:
        r = _http().get("/api/tags", timeout=10)
        return r.status_code < 400, f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
