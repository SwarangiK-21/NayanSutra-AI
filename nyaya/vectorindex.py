"""In-memory exact vector search over embeddings persisted in SQLite.

For a legal workspace (tens of thousands of chunks) an exact cosine scan with
NumPy takes a few milliseconds, gives perfect recall (no ANN approximation),
needs no separate vector server, and supports arbitrary metadata filters
(matter, collection, document) with boolean masks.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from . import db

log = logging.getLogger(__name__)


class VectorIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self.ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, 0), dtype=np.float32)
        self.doc = np.zeros(0, dtype=object)
        self.case = np.zeros(0, dtype=object)
        self.coll = np.zeros(0, dtype=object)

    def __len__(self) -> int:
        return len(self.ids)

    def load(self) -> None:
        if db.backend() == "postgres":
            return self._load_remote()
        rows = db.conn().execute(
            "SELECT c.id, c.doc_id, d.case_id, d.collection, c.emb FROM chunks c "
            "JOIN documents d ON d.id = c.doc_id WHERE d.status='ready' AND c.emb IS NOT NULL ORDER BY c.id"
        ).fetchall()
        self._set(rows)

    # ── Supabase: metadata from Postgres, embeddings from a local cache + incremental download ──
    CACHE_BATCH = 400   # rows per embedding query: well inside the statement timeout even on slow links

    def _cache_path(self):
        from . import config, embeddings
        safe = "".join(ch if ch.isalnum() else "_" for ch in embeddings.model_name())
        return config.HOME_DIR / f"vector_cache_{safe}.npz"

    def _load_remote(self) -> None:
        """Chunk ids are never reused (re-indexing inserts new rows), so a cached embedding for an id stays valid;
        only ids missing from the cache are downloaded. The cache is keyed by embedding model."""
        meta = db.conn().execute(
            "SELECT c.id, c.doc_id, d.case_id, d.collection FROM chunks c JOIN documents d ON d.id = c.doc_id "
            "WHERE d.status='ready' AND c.emb IS NOT NULL ORDER BY c.id").fetchall()
        cache: dict[int, np.ndarray] = {}
        path = self._cache_path()
        try:
            with np.load(path) as z:
                cache = dict(zip(z["ids"].tolist(), z["mat"]))
        except (OSError, ValueError, KeyError):
            cache = {}
        missing = [r[0] for r in meta if r[0] not in cache]
        if missing:
            log.info("Downloading %d embeddings from Supabase (%d cached)", len(missing), len(cache))
        for i in range(0, len(missing), self.CACHE_BATCH):
            part = missing[i:i + self.CACHE_BATCH]
            rows = db.conn().execute(f"SELECT id, emb FROM chunks WHERE id IN ({','.join('?' * len(part))})", part).fetchall()
            for r in rows:
                cache[r[0]] = np.frombuffer(bytes(r[1]), dtype=np.float32)
        rows = [(r[0], r[1], r[2], r[3], cache[r[0]]) for r in meta if r[0] in cache]
        self._set(rows, arrays=True)
        if missing or len(cache) != len(rows):
            self.save_cache()

    def save_cache(self) -> None:
        if db.backend() != "postgres":
            return
        try:
            path = self._cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.npz")
            with self._lock:
                np.savez(tmp, ids=self.ids, mat=self.mat)
            tmp.replace(path)   # atomic: an interrupted save never leaves a corrupt cache
        except OSError as exc:
            log.warning("Could not write the vector cache: %s", exc)

    def _set(self, rows, arrays: bool = False) -> None:
        with self._lock:
            if not rows:
                self.ids = np.zeros(0, dtype=np.int64)
                self.mat = np.zeros((0, 0), dtype=np.float32)
                self.doc = self.case = self.coll = np.zeros(0, dtype=object)
                self._ready.set()
                return
            self.ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            self.doc = np.array([r[1] for r in rows], dtype=object)
            self.case = np.array([r[2] or "" for r in rows], dtype=object)
            self.coll = np.array([r[3] for r in rows], dtype=object)
            # One contiguous buffer instead of thousands of small arrays.
            if arrays:
                self.mat = np.vstack([r[4] for r in rows]).astype(np.float32)
            else:
                self.mat = np.frombuffer(b"".join(r[4] for r in rows), dtype=np.float32).reshape(len(rows), -1).copy()
        self._ready.set()
        log.info("Vector index loaded: %d chunks, dim %d", len(rows), self.mat.shape[1])

    def wait_ready(self, timeout: float = 120.0) -> None:
        self._ready.wait(timeout)

    def add(self, ids: list[int], doc_id: str, case_id: str | None, collection: str, vecs: np.ndarray) -> None:
        if not ids:
            return
        with self._lock:
            if self.mat.size and self.mat.shape[1] != vecs.shape[1]:
                raise ValueError("embedding dimension changed; rebuild the index")
            self.ids = np.concatenate([self.ids, np.asarray(ids, dtype=np.int64)])
            self.mat = vecs.astype(np.float32) if not self.mat.size else np.vstack([self.mat, vecs.astype(np.float32)])
            n = len(ids)
            self.doc = np.concatenate([self.doc, np.array([doc_id] * n, dtype=object)])
            self.case = np.concatenate([self.case, np.array([case_id or ""] * n, dtype=object)])
            self.coll = np.concatenate([self.coll, np.array([collection] * n, dtype=object)])
        self.save_cache()   # Supabase mode: the next start does not need to download these embeddings again

    def remove_doc(self, doc_id: str) -> None:
        with self._lock:
            keep = self.doc != doc_id
            self.ids, self.mat = self.ids[keep], self.mat[keep] if self.mat.size else self.mat
            self.doc, self.case, self.coll = self.doc[keep], self.case[keep], self.coll[keep]

    def search(self, qvec: np.ndarray, k: int, *, case_id: str | None = None,
               collections: set[str] | None = None, doc_ids: set[str] | None = None,
               include_case_docs: bool = True, skip_doc_ids: set[str] | frozenset[str] | None = None
               ) -> list[tuple[int, float]]:
        """Top-k (chunk_id, cosine) under the filters.

        Library collections are shared; 'case' collection rows are only visible
        for the matching case_id (per-matter isolation)."""
        with self._lock:
            if not len(self.ids):
                return []
            mask = np.ones(len(self.ids), dtype=bool)
            if doc_ids is not None:
                mask &= np.isin(self.doc, list(doc_ids))
            if collections is not None:
                mask &= np.isin(self.coll, list(collections))
            if skip_doc_ids:
                mask &= ~np.isin(self.doc, list(skip_doc_ids))
            is_case = self.coll == "case"
            if include_case_docs and case_id:
                mask &= ~is_case | (self.case == case_id)
            else:
                mask &= ~is_case
            idx = np.flatnonzero(mask)
            if not len(idx):
                return []
            scores = self.mat[idx] @ qvec.astype(np.float32)
            k = min(k, len(idx))
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            return [(int(self.ids[idx[t]]), float(scores[t])) for t in top]


index = VectorIndex()
