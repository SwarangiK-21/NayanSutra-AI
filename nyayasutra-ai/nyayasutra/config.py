import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CHROMA_DIR = ROOT / ".chroma"

LLM_MODEL = os.getenv(
    "LLM_MODEL",
    "groq:llama-3.3-70b-versatile"
)
# Local embeddings: privileged client documents never leave the machine for indexing.
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150
RETRIEVE_K = 8
MAX_CONTEXT_DOCS = 6
MAX_RETRIEVAL_RETRIES = 2
MAX_GENERATION_ATTEMPTS = 2
DOC_TYPES = {"statutes", "judgments", "contracts", "other"}

DISCLAIMER = (
    "\n\n---\n*AI-generated research aid, not legal advice. "
    "Verify every citation against the primary source before relying on it.*"
)
