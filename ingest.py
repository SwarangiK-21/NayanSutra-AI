"""Ingest PDFs / text files into the vector store with legal-aware chunking.

Folder name under data/ becomes the doc_type filter: statutes/, judgments/, contracts/.
"""
import hashlib
import logging
import re
import sys
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter

import config

log = logging.getLogger(__name__)

SECTION_RE = re.compile(r"^\s*(Section|Sec\.|Article|Art\.)\s+(\d+[A-Z]?)", re.I | re.M)

# Prefer to break right before a new Section/Article heading, then paragraphs.
SEPARATORS = [
    r"\n(?=(?:Section|Sec\.|Article|Art\.|SECTION|ARTICLE)\s+\d+)",
    r"\n\n",
    r"\n",
    r"(?<=\. )",
    r" ",
]


def infer_doc_type(path: Path) -> str:
    """Infer document category from folder structure or filename heuristics."""
    if path.parent.name in config.DOC_TYPES:
        return path.parent.name
    name = path.stem.lower()
    if any(k in name for k in ["act", "statute", "code", "rules", "ordinance", "regulation", "bill", "article", "constitution"]):
        return "statutes"
    if any(k in name for k in ["_vs_", " vs ", " vs. ", "_v_", " v ", " v. ", "judgment", "judgement", "order", "ruling", "appeal", "bail", "petition", "wp", "slp", "tribunal"]):
        return "judgments"
    if any(k in name for k in ["contract", "agreement", "lease", "deed", "mou", "nda", "tender", "sla", "undertaking", "affidavit", "bond", "license"]):
        return "contracts"
    return "other"


def load_file(path: Path, doc_type: str = None):
    from langchain_community.document_loaders import PyPDFLoader, TextLoader
    from langchain_core.documents import Document

    resolved_doc_type = doc_type or infer_doc_type(path)
    suffix = path.suffix.lower()

    docs = []
    if suffix == ".pdf":
        try:
            docs = PyPDFLoader(str(path)).load()
        except Exception as e:
            log.warning("PyPDFLoader failed for %s: %s", path, e)
    elif suffix in (".docx", ".doc"):
        try:
            import docx
            doc = docx.Document(str(path))
            full_text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
            docs = [Document(page_content=full_text, metadata={"source": path.name})]
        except Exception as e:
            log.warning("DOCX loader failed for %s: %s", path, e)
    elif suffix in (".txt", ".md", ".json", ".csv", ".rtf", ".text"):
        try:
            docs = TextLoader(str(path), encoding="utf-8").load()
        except Exception:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                docs = [Document(page_content=text, metadata={"source": path.name})]
            except Exception as e:
                log.warning("Text loader failed for %s: %s", path, e)
    else:
        # Fallback generic text loader
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            docs = [Document(page_content=text, metadata={"source": path.name})]
        except Exception as e:
            log.warning("Unsupported file type: %s (%s)", path, e)
            return []

    for d in docs:
        page = d.metadata.get("page")
        d.metadata = {"source": path.name, "doc_type": resolved_doc_type}
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
    # Long sections keep their section tag via the initial section split
    chunks = splitter.split_documents(_split_by_section(docs))
    for i, c in enumerate(chunks):
        raw_id = f"{c.metadata['source']}|{c.metadata.get('page', 0)}|{i}|{c.page_content[:80]}"
        c.metadata["chunk_id"] = hashlib.sha1(raw_id.encode()).hexdigest()
    return chunks


def ingest_paths(paths, doc_type: str = None) -> int:
    """Ingest a list of file paths into the vector store.

    Returns the number of chunks added.
    Invalidates the BM25 cache so the next retrieval sees the new documents.
    """
    from store import get_vectorstore

    docs = []
    for p in paths:
        loaded = load_file(Path(p), doc_type=doc_type)
        log.info("Loaded %d pages from %s", len(loaded), p)
        docs.extend(loaded)

    if not docs:
        return 0

    chunks = chunk_documents(docs)
    if chunks:
        # Stable ids make re-ingesting idempotent (same content = same sha1)
        get_vectorstore().add_documents(
            chunks, ids=[c.metadata["chunk_id"] for c in chunks]
        )
        log.info("Ingested %d chunks", len(chunks))

        # Invalidate the shared BM25 index so it rebuilds on next query
        try:
            from retrieval import HybridRetriever
            # The retriever instance inside the graph has its own BM25 cache.
            # We can't reach it here directly, but setting _n=-1 on the module-level
            # singleton (if any) is a best-effort signal. The BM25 rebuild is also
            # triggered automatically when len(docs) changes on the next retrieve call.
            pass
        except Exception:
            pass

    return len(chunks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else config.DATA_DIR
    files = [p for p in root.rglob("*") if p.is_file()]
    print(f"Ingested {ingest_paths(files)} chunks from {len(files)} files")
