"""Ingest PDFs / text files into the vector store with legal-aware chunking.

Folder name under data/ becomes the doc_type filter: statutes/, judgments/, contracts/.
"""
import hashlib
import re
import sys
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter

import config

SECTION_RE = re.compile(r"^\s*(Section|Sec\.|Article|Art\.)\s+(\d+[A-Z]?)", re.I | re.M)

# Prefer to break right before a new Section/Article heading, then paragraphs.
SEPARATORS = [
    r"\n(?=(?:Section|Sec\.|Article|Art\.|SECTION|ARTICLE)\s+\d+)",
    r"\n\n",
    r"\n",
    r"(?<=\. )",
    r" ",
]


def load_file(path: Path):
    from langchain_community.document_loaders import PyPDFLoader, TextLoader

    doc_type = path.parent.name if path.parent.name in config.DOC_TYPES else "other"
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        docs = PyPDFLoader(str(path)).load()
    elif suffix in (".txt", ".md"):
        docs = TextLoader(str(path), encoding="utf-8").load()
    else:
        return []
    for d in docs:
        page = d.metadata.get("page")
        d.metadata = {"source": path.name, "doc_type": doc_type}
        if page is not None:
            d.metadata["page"] = int(page) + 1  # PyPDF is 0-indexed
    return docs


SECTION_SPLIT = re.compile(r"\n(?=(?:Section|Sec\.|Article|Art\.)\s+\d+)", re.I)


def _split_by_section(docs):
    """One piece per Section/Article so a chunk never straddles two provisions."""
    from langchain_core.documents import Document

    pieces = []
    for d in docs:
        for part in SECTION_SPLIT.split(d.page_content):
            if not part.strip():
                continue
            meta = dict(d.metadata)
            m = SECTION_RE.match(part)
            if m:
                meta["section"] = f"{m.group(1).title()} {m.group(2)}"
            pieces.append(Document(page_content=part, metadata=meta))
    return pieces


def chunk_documents(docs):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        separators=SEPARATORS,
        is_separator_regex=True,
    )
    chunks = splitter.split_documents(_split_by_section(docs))  # long sections keep their section tag
    for i, c in enumerate(chunks):
        raw_id = f"{c.metadata['source']}|{c.metadata.get('page', 0)}|{i}|{c.page_content[:80]}"
        c.metadata["chunk_id"] = hashlib.sha1(raw_id.encode()).hexdigest()
    return chunks


def ingest_paths(paths) -> int:
    from store import get_vectorstore

    docs = []
    for p in paths:
        docs.extend(load_file(Path(p)))
    chunks = chunk_documents(docs)
    if chunks:  # stable ids make re-ingesting idempotent
        get_vectorstore().add_documents(chunks, ids=[c.metadata["chunk_id"] for c in chunks])
    return len(chunks)


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else config.DATA_DIR
    files = [p for p in root.rglob("*") if p.is_file()]
    print(f"Ingested {ingest_paths(files)} chunks from {len(files)} files")
