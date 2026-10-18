"""Central configuration, read once from .env.

Everything tunable lives here so the rest of the code never touches os.environ.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and v.strip():
            return v.strip().strip('"').strip("'")
    return default


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if not v else v.lower() in {"1", "true", "yes", "on"}


# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = ROOT / "data"
LIBRARY_DIR = DATA_DIR / "library"
BARE_ACTS_DIR = LIBRARY_DIR / "bare_acts"
DRAFTS_DIR = LIBRARY_DIR / "drafts"
JUDGMENTS_DIR = LIBRARY_DIR / "judgments"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(_env("NYAYA_DB", default=str(DATA_DIR / "nyaysutra.db")))
WEB_DIR = ROOT / "web"
# Models live on the fastest local disk (the user's home, usually the SSD).
HOME_DIR = Path(_env("NYAYA_HOME", default=str(Path.home() / ".nyaysutra")))
MODEL_CACHE = Path(_env("MODEL_CACHE", default=str(HOME_DIR / "models")))

# ── LLM (Ollama — cloud with API key, or a local Ollama server) ─────────────
OLLAMA_API_KEY = _env("OLLAMA_API_KEY")
OLLAMA_HOST = _env("OLLAMA_HOST", default="https://ollama.com" if OLLAMA_API_KEY else "http://127.0.0.1:11434")
LLM_MODEL = _env("LLM_MODEL", "LLM_MODEL_NAME", default="gpt-oss:120b")
FAST_LLM_MODEL = _env("FAST_LLM_MODEL", default="gpt-oss:20b")
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", default="180"))
# Reasoning effort for gpt-oss style models: low keeps latency down.
LLM_THINK = _env("LLM_THINK", default="low")

# ── Embeddings + reranking (local, private) ─────────────────────────────────
EMBED_BACKEND = _env("EMBED_BACKEND", default="fastembed")  # fastembed | ollama
EMBED_MODEL = _env("EMBED_MODEL", default="BAAI/bge-small-en-v1.5")
OLLAMA_EMBED_HOST = _env("OLLAMA_EMBED_HOST", default="http://127.0.0.1:11434")
RERANK_ENABLED = _bool("RERANK_ENABLED", True)
RERANK_MODEL = _env("RERANK_MODEL", default="Xenova/ms-marco-MiniLM-L-6-v2")

# ── Retrieval ───────────────────────────────────────────────────────────────
CANDIDATES = 40          # per retriever (dense / lexical) before fusion
RERANK_POOL = 24         # fused candidates scored by the cross-encoder
RERANK_CHARS = 600       # header + opening text is what the cross-encoder reads (speed)
MAX_PER_DOC = 3          # diversity: passages per document in one answer
CONTEXT_PASSAGES = 8     # passages handed to the LLM
CONTEXT_CHAR_BUDGET = 16000
RELEVANCE_FLOOR = float(_env("RELEVANCE_FLOOR", default="-1.0"))  # cross-encoder logit below = weak evidence
DOC_ANALYSIS_MAX_CHARS = 90000  # whole-document analysis in one call below this size

# ── Server / security ───────────────────────────────────────────────────────
APP_HOST = _env("APP_HOST", default="127.0.0.1")
APP_PORT = int(_env("APP_PORT", default="8000"))
APP_PASSWORD = _env("APP_PASSWORD")
ALLOWED_HOSTS = [h.strip() for h in _env("ALLOWED_HOSTS", default="127.0.0.1,localhost").split(",") if h.strip()]
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", default="40"))
MAX_PDF_PAGES = int(_env("MAX_PDF_PAGES", default="2000"))
CHAT_RATE_PER_MIN = int(_env("CHAT_RATE_PER_MIN", default="20"))

DISCLAIMER = (
    "AI-generated research aid, not legal advice. Verify every citation against "
    "the primary source before relying on it."
)


def ensure_dirs() -> None:
    for d in (DATA_DIR, LIBRARY_DIR, BARE_ACTS_DIR, DRAFTS_DIR, JUDGMENTS_DIR, UPLOAD_DIR, MODEL_CACHE):
        d.mkdir(parents=True, exist_ok=True)
