"""Minimal Ollama client (cloud via API key, or a local server) with streaming,
JSON mode, retries and user-friendly errors. One pooled HTTP connection is
reused for every call, which keeps per-request latency low.

This is the single point where text leaves the server for the language model, so personal identifiers are
pseudonymised here (privacy.redact_messages) and restored in the reply."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Iterator

import httpx

from . import config, privacy

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Message is safe to show to the user."""


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
    log.warning("Ollama error %s: %s", r.status_code, str(detail)[:200])
    if r.status_code in (401, 403):
        raise LLMError("The Ollama API key was rejected. Check OLLAMA_API_KEY in .env.")
    if r.status_code == 404 or "not found" in detail.lower():
        raise LLMError(f"Model not available on your Ollama plan: {detail}")
    if r.status_code == 429 or "limit" in detail.lower():
        raise LLMError("Ollama usage limit reached for now. Please wait a few minutes and try again.")
    raise LLMError(f"The language model service returned an error ({r.status_code}).")


def chat(messages: list[dict], *, model: str | None = None, json_mode: bool = False, temperature: float = 0.1,
         max_tokens: int | None = None, retries: int = 2) -> str:
    messages, red = privacy.redact_messages(messages)
    body = _payload(messages, model, False, json_mode, temperature, max_tokens)
    for attempt in range(retries + 1):
        try:
            r = _http().post("/api/chat", json=body)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            _raise_for(r)
            return red.restore((r.json().get("message") or {}).get("content", ""))
        except httpx.TimeoutException as exc:
            if attempt == retries:
                raise LLMError("The language model took too long to respond. Please try again.") from exc
        except httpx.HTTPError as exc:
            if attempt == retries:
                raise LLMError("Could not reach the language model service. Check your internet connection.") from exc
            time.sleep(1.0)
    return ""


def stream(messages: list[dict], *, model: str | None = None, temperature: float = 0.1,
           max_tokens: int | None = None) -> Iterator[str]:
    """Yield answer tokens as they arrive (reasoning tokens are skipped), with identifiers restored."""
    messages, red = privacy.redact_messages(messages)
    yield from red.stream(_stream(messages, model, temperature, max_tokens))


def _stream(messages: list[dict], model: str | None, temperature: float, max_tokens: int | None) -> Iterator[str]:
    body = _payload(messages, model, True, False, temperature, max_tokens)
    for attempt in range(2):
        try:
            with _http().stream("POST", "/api/chat", json=body) as r:
                if r.status_code in (429, 502, 503) and attempt == 0:
                    r.read()
                    time.sleep(2)
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
                        raise LLMError(f"Model error: {evt['error']}")
                    tok = (evt.get("message") or {}).get("content")
                    if tok:
                        yield tok
                    if evt.get("done"):
                        return
            return
        except httpx.TimeoutException as exc:
            raise LLMError("The language model took too long to respond. Please try again.") from exc
        except httpx.HTTPError as exc:
            raise LLMError("Lost connection to the language model service.") from exc


def chat_json(messages: list[dict], *, model: str | None = None, default: dict | None = None) -> dict:
    """JSON-mode call with tolerant parsing; returns `default` on any failure."""
    try:
        raw = chat(messages, model=model, json_mode=True, temperature=0.0)
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
