# wraps the embedding model and Chroma vector store
import os
from functools import lru_cache
from pathlib import Path

from langchain_core.documents import Document

import config

# Prioritize local HuggingFace cache to prevent online timeout delays
os.environ.setdefault("HF_HUB_OFFLINE", "1")


@lru_cache
def get_embeddings():
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(
        model_name=config.EMBED_MODEL,
        encode_kwargs={"normalize_embeddings": True},
    )


@lru_cache
def get_vectorstore():
    from langchain_chroma import Chroma

    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return Chroma(
        collection_name="legal_kb",
        embedding_function=get_embeddings(),
        persist_directory=str(config.CHROMA_DIR),
    )


def all_documents() -> list[Document]:
    raw = get_vectorstore().get(include=["documents", "metadatas"])
    return [
        Document(page_content=t, metadata=m or {})
        for t, m in zip(raw["documents"], raw["metadatas"])
    ]
