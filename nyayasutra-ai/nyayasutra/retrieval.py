"""Hybrid retrieval: dense (Chroma) + lexical (BM25), fused with Reciprocal Rank Fusion.

Lexical matters in law: exact section numbers, case names and defined terms
("Section 138", "Kesavananda Bharati") are poorly served by embeddings alone.
"""
from typing import Optional

from langchain_core.documents import Document

import config


def _key(d: Document) -> str:
    return d.metadata.get("chunk_id") or str(hash(d.page_content))


def rrf(rank_lists: list[list[Document]], k: int = 60) -> list[Document]:
    scores: dict[str, float] = {}
    docs: dict[str, Document] = {}
    for lst in rank_lists:
        for rank, d in enumerate(lst):
            key = _key(d)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            docs[key] = d
    return [docs[key] for key in sorted(scores, key=scores.get, reverse=True)]


class HybridRetriever:
    def __init__(self, k: int = config.RETRIEVE_K):
        self.k = k
        self._bm25 = None
        self._n = -1

    def _bm25_index(self):
        from langchain_community.retrievers import BM25Retriever
        from store import all_documents

        docs = all_documents()
        if not docs:
            return None
        if self._bm25 is None or len(docs) != self._n:  # rebuild after new ingests
            self._bm25 = BM25Retriever.from_documents(docs, k=self.k * 3)
            self._n = len(docs)
        return self._bm25

    def retrieve(self, query: str, doc_type: Optional[str] = None) -> list[Document]:
        from store import get_vectorstore

        flt = {"doc_type": doc_type} if doc_type else None
        dense = get_vectorstore().similarity_search(query, k=self.k * 2, filter=flt)

        lexical: list[Document] = []
        bm25 = self._bm25_index()
        if bm25:
            lexical = bm25.invoke(query)
            if doc_type:
                lexical = [d for d in lexical if d.metadata.get("doc_type") == doc_type]

        return rrf([dense, lexical])[: self.k]
