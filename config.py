import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CHROMA_DIR = ROOT / ".chroma"

# Split explicitly rather than relying on init_chat_model's "provider:model" string
# parsing, which behaves inconsistently across langchain versions.
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or os.getenv("LLM_Provider") or "groq").lower()
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME") or os.getenv("LLM_MODEL") or "openai/gpt-oss-120b"

# Local embeddings: privileged client documents never leave the machine for indexing.
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# Retrieval: fetch more candidates so the grader has a wider pool to filter from
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200           # slightly more overlap → fewer boundary misses
RETRIEVE_K = 10               # up from 6 — fetch more candidates for grading
MAX_CONTEXT_DOCS = 6          # up from 4 — give the generator richer context
MAX_RETRIEVAL_RETRIES = 2     # up from 1 — two rewrites before abstaining
MAX_GENERATION_ATTEMPTS = 1    # single generation + verify audit; avoids Groq 429 delays
DOC_TYPES = {"statutes", "judgments", "contracts", "other"}

DISCLAIMER = (
    "\n\n---\n*AI-generated research aid, not legal advice. "
    "Verify every citation against the primary source before relying on it.*"
)