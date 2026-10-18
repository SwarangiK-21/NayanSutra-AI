"""Offline test setup: temp database/folders, deterministic fake embeddings and a fake LLM.
No network, no model downloads."""
import hashlib
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp(prefix="nyaya_test_"))
os.environ["NYAYA_DB"] = str(_TMP / "test.db")
os.environ["NYAYA_HOME"] = str(_TMP / "home")
os.environ.setdefault("OLLAMA_API_KEY", "test-key")

from nyaya import config, db, embeddings, llm  # noqa: E402

config.DATA_DIR = _TMP / "data"
config.UPLOAD_DIR = config.DATA_DIR / "uploads"
config.LIBRARY_DIR = config.DATA_DIR / "library"
config.BARE_ACTS_DIR = config.LIBRARY_DIR / "bare_acts"
config.DRAFTS_DIR = config.LIBRARY_DIR / "drafts"
config.JUDGMENTS_DIR = config.LIBRARY_DIR / "judgments"
config.MODEL_CACHE = _TMP / "models"

DIM = 128


def _vec(text: str) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
    n = np.linalg.norm(v)
    return v / n if n else v


def fake_passages(texts, batch_size=32):
    return np.vstack([_vec(t) for t in texts]) if texts else np.zeros((0, DIM), dtype=np.float32)


def fake_query(text):
    return _vec(text)


def fake_rerank(query, passages):
    q = set(re.findall(r"[a-z0-9]+", query.lower()))
    return [float(len(q & set(re.findall(r"[a-z0-9]+", p.lower())))) - 1.0 for p in passages]


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat_json(self, messages, model=None, default=None):
        text = messages[-1]["content"]
        self.calls.append(("json", text[:60]))
        if "audit" in text.lower():
            return {"grounded": True, "unsupported": []}
        msg = text.rsplit("User message:", 1)[-1].lower()
        if "limitation" in msg:
            return {"intent": "calculation", "query": msg, "calc": {"cause": "breach_of_contract", "start_date": "2024-03-15"}}
        if "hello" in msg:
            return {"intent": "chitchat", "query": msg}
        return {"intent": "research", "query": msg.strip(), "alt_queries": []}

    def chat(self, messages, **kw):
        self.calls.append(("chat", messages[-1]["content"][:60]))
        return "rewritten query"

    def stream(self, messages, **kw):
        self.calls.append(("stream", messages[-1]["content"][:60]))
        yield from ["**Short answer** — ", "The deposit is refundable [1]."]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_passages", fake_passages)
    monkeypatch.setattr(embeddings, "embed_query", fake_query)
    monkeypatch.setattr(embeddings, "rerank", fake_rerank)
    monkeypatch.setattr(embeddings, "warm_up", lambda: None)
    fake = FakeLLM()
    monkeypatch.setattr(llm, "chat_json", fake.chat_json)
    monkeypatch.setattr(llm, "chat", fake.chat)
    monkeypatch.setattr(llm, "stream", fake.stream)
    monkeypatch.setattr(llm, "ping", lambda: (True, "ok"))
    yield fake


@pytest.fixture
def fresh_db():
    # A new file per test (Windows cannot delete a SQLite file that another thread still has open).
    config.DB_PATH = _TMP / f"test_{uuid.uuid4().hex[:8]}.db"
    db.init()
    from nyaya.vectorindex import index
    index.load()
    yield
    db.reset_connections()
