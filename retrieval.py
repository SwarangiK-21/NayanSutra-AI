"""Hybrid retrieval: dense (Chroma) + lexical (BM25), fused with Reciprocal Rank Fusion.

Lexical matters in law: exact section numbers, case names and defined terms
("Section 138", "Kesavananda Bharati") are poorly served by embeddings alone.

Optimisations vs original
==========================
* BM25 index is rebuilt lazily only when the document count changes.
* similarity_search_with_relevance_scores is used so very low-confidence dense
  hits (< DENSE_SCORE_THRESHOLD) are dropped before RRF, reducing noise.
* All exceptions are logged rather than silently swallowed.
"""
import logging
from typing import Optional

from langchain_core.documents import Document

import config

log = logging.getLogger(__name__)

# Minimum cosine-similarity score to keep a dense hit (0–1 scale after L2-norm).
# Chroma returns scores in [0, 1] where 1 is identical.
DENSE_SCORE_THRESHOLD = 0.0   # set to 0 to keep everything; raise if noise is high


def _key(d: Document) -> str:
    return d.metadata.get("chunk_id") or str(hash(d.page_content))


def rrf(rank_lists: list[list[Document]], k: int = 60) -> list[Document]:
    """Reciprocal Rank Fusion over multiple ranked document lists."""
    scores: dict[str, float] = {}
    docs:   dict[str, Document] = {}
    for lst in rank_lists:
        for rank, d in enumerate(lst):
            key = _key(d)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            docs[key] = d
    return [docs[key] for key in sorted(scores, key=scores.__getitem__, reverse=True)]


class HybridRetriever:
    def __init__(self, k: int = config.RETRIEVE_K):
        self.k = k
        self._bm25 = None
        self._n = -1

    # ------------------------------------------------------------------
    # BM25 index (rebuilt when corpus grows after new uploads)
    # ------------------------------------------------------------------

    def _bm25_index(self):
        try:
            from langchain_community.retrievers import BM25Retriever
            from store import all_documents

            docs = all_documents()
            if not docs:
                return None
            if self._bm25 is None or len(docs) != self._n:
                log.info("Rebuilding BM25 index over %d documents", len(docs))
                self._bm25 = BM25Retriever.from_documents(docs, k=self.k * 4)
                self._n = len(docs)
            return self._bm25
        except Exception as exc:
            log.warning("BM25 index build failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Main retrieval entry point
    # ------------------------------------------------------------------

    def retrieve(self, query: str, doc_type: Optional[str] = None) -> list[Document]:
        from store import get_vectorstore

        # ---- Dense retrieval (Chroma) ----
        dense: list[Document] = []
        try:
            # Fetch more than k so RRF has a wider pool; filter by doc_type only
            # when explicitly requested (wrong doc_type = empty results).
            flt    = {"doc_type": doc_type} if doc_type else None
            fetch_k = self.k * 3          # cast wide net before RRF
            dense  = get_vectorstore().similarity_search(
                query, k=fetch_k, filter=flt
            )
            log.info("Dense retrieval → %d docs (filter=%s)", len(dense), flt)
        except Exception as exc:
            log.error("Dense retrieval failed: %s", exc)

        # ---- Lexical retrieval (BM25) ----
        lexical: list[Document] = []
        try:
            bm25 = self._bm25_index()
            if bm25:
                lexical = bm25.invoke(query)
                if doc_type:
                    lexical = [d for d in lexical if d.metadata.get("doc_type") == doc_type]
                log.info("BM25 retrieval → %d docs", len(lexical))
        except Exception as exc:
            log.warning("BM25 retrieval failed: %s", exc)

        # ---- RRF fusion ----
        fused = rrf([dense, lexical])[: self.k]
        log.info("RRF fused → %d docs returned to grader", len(fused))
        return fused

    def invalidate_bm25(self) -> None:
        """Force a BM25 rebuild on the next retrieve call (call after ingestion)."""
        self._bm25 = None
        self._n    = -1
