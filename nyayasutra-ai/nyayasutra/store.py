#wraps the embedding model and Chroma vector store

from functools import lru_cache

from langchain_core.documents import Document

import config


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
