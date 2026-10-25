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

# ── Document intelligence ───────────────────────────────────────────────────
# Optional OCR engine for scanned PDFs / images (Tesseract with eng+hin+mar data). Empty = auto-detect.
TESSERACT_CMD = _env("TESSERACT_CMD")
OCR_MAX_PAGES = int(_env("OCR_MAX_PAGES", default="60"))
# Whole-document translation / summaries send document text to the LLM; the UI asks first.
DOC_LLM_MAX_CHARS = int(_env("DOC_LLM_MAX_CHARS", default="120000"))

# ── Supabase (primary storage when configured) ──────────────────────────────
# Postgres connection string for the backend role (session pooler); empty = local SQLite file (offline / tests).
SUPABASE_DB_URL = _env("SUPABASE_DB_URL")
DB_POOL_MAX = int(_env("DB_POOL_MAX", default="8"))
# Storage API (server-side only — never sent to the browser). Secret key 'sb_secret_…' or legacy service_role key.
SUPABASE_URL = _env("SUPABASE_URL").rstrip("/")
SUPABASE_SECRET_KEY = _env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = _env("SUPABASE_BUCKET", default="case-files")

# ── Integrations / sync ─────────────────────────────────────────────────────
SYNC_ENABLED = _bool("SYNC_ENABLED", True)
SYNC_TICK_SECONDS = int(_env("SYNC_TICK_SECONDS", default="60"))
HTTP_USER_AGENT = "NyaySutra-AI/1.0 (+local legal research assistant)"

# ── Calendar reminders ──────────────────────────────────────────────────────
DEFAULT_TIMEZONE = _env("DEFAULT_TIMEZONE", default="Asia/Kolkata")
REMINDERS_ENABLED = _bool("REMINDERS_ENABLED", True)        # background dispatcher (off in tests)
REMINDER_TICK_SECONDS = int(_env("REMINDER_TICK_SECONDS", default="30"))
# Optional e-mail reminders through your own SMTP server (off until SMTP_HOST and SMTP_FROM are set).
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", default="587"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM")
SMTP_STARTTLS = _bool("SMTP_STARTTLS", True)
SMTP_SSL = _bool("SMTP_SSL", False)

# ── Accounts (Supabase Auth, used server-side) ──────────────────────────────
# Publishable key ('sb_publishable_…' or the legacy anon key): used only by the backend for the Auth API.
SUPABASE_PUBLISHABLE_KEY = _env("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY")
# supabase = per-user accounts; local = single-user operator on this computer (no accounts); auto = supabase when
# SUPABASE_URL and the publishable key are set.
AUTH_MODE = _env("AUTH_MODE", default="auto").lower()
# open = anyone may register (as a Lawyer); invite_only = only invited e-mail addresses get access
SIGNUP_MODE = _env("SIGNUP_MODE", default="open").lower()
MFA_REQUIRED_ROLES = tuple(r.strip() for r in _env("MFA_REQUIRED_ROLES", default="admin,advocate").split(",")
                           if r.strip())
SESSION_TTL_HOURS = int(_env("SESSION_TTL_HOURS", default="12"))
SESSION_IDLE_MINUTES = int(_env("SESSION_IDLE_MINUTES", default="60"))
LOGIN_MAX_FAILURES = int(_env("LOGIN_MAX_FAILURES", default="5"))
LOGIN_LOCK_MINUTES = int(_env("LOGIN_LOCK_MINUTES", default="15"))
INVITE_DAYS = int(_env("INVITE_DAYS", default="14"))
# Shared server key for CSRF values and recovery-code hashes (needed when several app instances run).
APP_SECRET_KEY = _env("APP_SECRET_KEY")
# Public address of the app, e.g. https://nyaysutra.example.org — sets Secure cookies + HSTS and the e-mail link target.
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")


def auth_mode() -> str:
    if AUTH_MODE in ("supabase", "local"):
        return AUTH_MODE
    return "supabase" if SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY else "local"


# ── AI data protection ──────────────────────────────────────────────────────
# Replace Aadhaar / PAN / phone / e-mail / bank account / card / passport numbers with placeholders before text is
# sent to the external LLM, and put the real values back in the reply.
AI_PII_REDACTION = _bool("AI_PII_REDACTION", True)
# Optional malware scan of uploads with ClamAV (path to clamscan/clamdscan); empty = auto-detect, 'off' = disabled.
CLAMAV_CMD = _env("CLAMAV_CMD")

# ── Background jobs and bulk upload ─────────────────────────────────────────
INGEST_WORKERS = int(_env("INGEST_WORKERS", default="1"))   # parse/OCR/embed is CPU-bound: keep it small
JOB_WORKERS = int(_env("JOB_WORKERS", default="2"))         # translation, summaries, drafts (mostly waiting on the LLM)
INGEST_MAX_ATTEMPTS = int(_env("INGEST_MAX_ATTEMPTS", default="3"))
JOB_TIMEOUT_SECONDS = int(_env("JOB_TIMEOUT_SECONDS", default="900"))
BATCH_MAX_FILES = int(_env("BATCH_MAX_FILES", default="50"))
BATCH_MAX_TOTAL_MB = int(_env("BATCH_MAX_TOTAL_MB", default="500"))

# ── LLM resilience ──────────────────────────────────────────────────────────
LLM_RETRIES = int(_env("LLM_RETRIES", default="2"))                     # extra attempts per model, with jittered back-off
LLM_FALLBACK_MODEL = _env("LLM_FALLBACK_MODEL", default="gpt-oss:20b")  # used (and labelled) when the main model fails
LLM_BREAKER_FAILURES = int(_env("LLM_BREAKER_FAILURES", default="4"))   # consecutive failures that open the circuit
LLM_BREAKER_SECONDS = int(_env("LLM_BREAKER_SECONDS", default="60"))    # how long calls to that model are skipped

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


def to_stored(path: Path) -> str:
    """Paths inside the project are stored relative to it, so the folder can be moved or cloned."""
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(p)


def from_stored(stored: str) -> Path:
    p = Path(stored)
    return p if p.is_absolute() else ROOT / p


def ensure_dirs() -> None:
    for d in (DATA_DIR, LIBRARY_DIR, BARE_ACTS_DIR, DRAFTS_DIR, JUDGMENTS_DIR, UPLOAD_DIR, MODEL_CACHE):
        d.mkdir(parents=True, exist_ok=True)
