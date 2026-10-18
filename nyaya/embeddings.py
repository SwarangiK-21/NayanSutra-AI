"""Local embeddings + cross-encoder reranking (ONNX via fastembed — no torch).

Documents never leave the machine for indexing. Models are loaded lazily and
warmed in a background thread at server start, so the first question is fast.
"""
from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache

import numpy as np

from . import config

log = logging.getLogger(__name__)

# Keep huggingface/fastembed quiet and offline once models are cached.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

_lock = threading.Lock()
# bge models expect this instruction on queries (not on passages).
_BGE_QUERY = "Represent this sentence for searching relevant passages: "


def _cache_dir() -> str:
    config.MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    return str(config.MODEL_CACHE)


DEFAULT_EMBED = "BAAI/bge-small-en-v1.5"


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    supported = {m["model"].lower() for m in TextEmbedding.list_supported_models()}
    if config.EMBED_MODEL.lower() not in supported:
        log.error("EMBED_MODEL %r is not available locally via fastembed; using %s instead. "
                  "(For bge-m3 use EMBED_BACKEND=ollama with a local Ollama server.)",
                  config.EMBED_MODEL, DEFAULT_EMBED)
        config.EMBED_MODEL = DEFAULT_EMBED
    log.info("Loading embedding model %s", config.EMBED_MODEL)
    return _offline_first(lambda: TextEmbedding(config.EMBED_MODEL, cache_dir=_cache_dir()))


@lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    log.info("Loading reranker %s", config.RERANK_MODEL)
    return _offline_first(lambda: TextCrossEncoder(config.RERANK_MODEL, cache_dir=_cache_dir()))


def _offline_first(factory):
    """Load from the local cache without touching the network; download only if missing."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        return factory()
    except Exception:
        log.info("Model not cached yet — downloading once")
        os.environ.pop("HF_HUB_OFFLINE", None)
        return factory()


def model_name() -> str:
    return f"{config.EMBED_BACKEND}:{config.EMBED_MODEL}"


def _normalise(m: np.ndarray) -> np.ndarray:
    m = m.astype(np.float32, copy=False)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def _ollama_embed(texts: list[str]) -> np.ndarray:
    import httpx

    out = []
    with httpx.Client(timeout=120) as c:
        for i in range(0, len(texts), 32):
            r = c.post(f"{config.OLLAMA_EMBED_HOST}/api/embed",
                       json={"model": config.EMBED_MODEL, "input": texts[i:i + 32]})
            r.raise_for_status()
            out.extend(r.json()["embeddings"])
    return np.asarray(out, dtype=np.float32)


def embed_passages(texts: list[str], batch_size: int = 32) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    if config.EMBED_BACKEND == "ollama":
        return _normalise(_ollama_embed(texts))
    with _lock:
        vecs = list(_embedder().embed(texts, batch_size=batch_size))
    return _normalise(np.vstack(vecs))


def embed_query(text: str) -> np.ndarray:
    if config.EMBED_BACKEND == "ollama":
        return _normalise(_ollama_embed([text]))[0]
    q = (_BGE_QUERY + text) if "bge" in config.EMBED_MODEL.lower() else text
    with _lock:
        v = next(iter(_embedder().embed([q])))
    return _normalise(np.asarray([v]))[0]


def rerank(query: str, passages: list[str]) -> list[float] | None:
    """Cross-encoder relevance logits (higher = more relevant), or None if disabled."""
    if not config.RERANK_ENABLED or not passages:
        return None
    try:
        with _lock:
            return [float(s) for s in _reranker().rerank(query, passages, batch_size=16)]
    except Exception as exc:  # never fail a query because the reranker hiccuped
        log.warning("Reranker failed: %s", exc)
        return None


def warm_up() -> None:
    """Load models (downloads on first run) so the first user query is fast."""
    try:
        embed_query("warm up")
        rerank("warm up", ["warm up"])
        log.info("Embedding + reranker models ready")
    except Exception as exc:
        log.error("Model warm-up failed: %s", exc)
