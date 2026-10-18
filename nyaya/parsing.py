"""Text extraction for PDF / DOCX / TXT with cleaning tuned for Indian legal documents.

PDFs go through PDFium (pypdfium2): fast, accurate word spacing, and it is the
hardened parser Chrome uses — a sensible choice for untrusted uploads.
"""
from __future__ import annotations

import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import config


class ParseError(Exception):
    """User-facing extraction problem (scanned PDF, encrypted file, ...)."""


@dataclass
class Parsed:
    pages: list[str]                 # cleaned text per page (1 entry for non-paged formats)
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.pages)


# ── Cleaning ─────────────────────────────────────────────────────────────────
_FOOTNOTE_REF = re.compile(r"(?<![\w\]])\d{1,2}\s*\n\s*\[")          # "1\n[(b) ..."  ->  "[(b) ..."
_FOOTNOTE_STARS = re.compile(r"\d{1,2}\s*\*\s*\*\s*\*")                # "1***" -> "***"
_FOOTNOTE_LINE = re.compile(
    r"^\s*\d{1,2}\s*\.?\s+(Subs\.|Ins\.|Omitted|Rep\.|Added|The words|Vide|Renumbered|Certain words|"
    r"Now see|See |Came into force|This Act has|Extended to|Brought into force|Cl\. |Clause \(|"
    r"Sub-section|The original|For the Statement|Amended by|Earlier|Proviso|Section \d+ (?:was|of))",
    re.I,
)
_PAGE_NUM = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*(?:of\s*\d+)?\s*$", re.I)
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_SPACES = re.compile(r"[ \t ]+")


def _norm_line(s: str) -> str:
    return re.sub(r"\d+", "#", s.strip().lower())


def clean_pages(pages: list[str]) -> list[str]:
    """Normalise unicode, strip running headers/footers, page numbers and footnote noise."""
    pages = [unicodedata.normalize("NFKC", p).replace("\r\n", "\n").replace("\r", "\n").replace("￾", "-")
             for p in pages]
    # Running headers/footers: lines (digits normalised) that recur on many pages.
    n = len(pages)
    repeated: set[str] = set()
    if n >= 4:
        counts: Counter[str] = Counter()
        for p in pages:
            lines = [ln for ln in p.split("\n") if ln.strip()]
            counts.update({_norm_line(ln) for ln in lines[:3] + lines[-3:]})
        repeated = {k for k, v in counts.items() if v >= max(3, 0.4 * n) and len(k) < 160}

    out = []
    for p in pages:
        p = _FOOTNOTE_REF.sub("[", p)
        p = _FOOTNOTE_STARS.sub("***", p)
        kept = []
        for ln in p.split("\n"):
            s = _SPACES.sub(" ", ln).strip()
            if not s or _PAGE_NUM.match(s) or s == "IndiaCode" or _norm_line(s) in repeated:
                if not s:
                    kept.append("")
                continue
            if _FOOTNOTE_LINE.match(s):
                continue
            kept.append(s)
        text = "\n".join(kept)
        text = _HYPHEN_BREAK.sub(r"\1\2", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        out.append(text)
    return out


# ── Extractors ───────────────────────────────────────────────────────────────
def _pdf(path: Path) -> Parsed:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        msg = str(exc).lower()
        if "password" in msg:
            raise ParseError("This PDF is password-protected. Remove the password and upload again.") from exc
        raise ParseError("This file could not be read as a PDF (it may be damaged).") from exc
    try:
        if len(doc) > config.MAX_PDF_PAGES:
            raise ParseError(f"PDF has {len(doc)} pages; the limit is {config.MAX_PDF_PAGES}.")
        raw = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            raw.append(tp.get_text_range())
            tp.close()
            page.close()
    finally:
        doc.close()
    pages = clean_pages(raw)
    total = sum(len(p) for p in pages)
    if total < max(200, 25 * len(pages)):
        raise ParseError(
            "No readable text found — this looks like a scanned PDF. Please upload a text-based PDF "
            "(or run OCR on it first)."
        )
    return Parsed(pages=pages, meta={"pages": len(pages)})


def _docx(path: Path) -> Parsed:
    # Zip-bomb guard before handing the archive to python-docx.
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > 300 * 1024 * 1024:
            raise ParseError("DOCX expands to an unreasonable size; refusing to open it.")
        if "word/document.xml" not in z.namelist():
            raise ParseError("Not a valid Word document.")
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return Parsed(pages=clean_pages(["\n".join(parts)]), meta={"pages": 1})


def _text(path: Path) -> Parsed:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            txt = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        txt = data.decode("utf-8", errors="replace")
    return Parsed(pages=clean_pages([txt]), meta={"pages": 1})


EXTRACTORS = {".pdf": _pdf, ".docx": _docx, ".txt": _text, ".md": _text}


def extract(path: Path) -> Parsed:
    fn = EXTRACTORS.get(path.suffix.lower())
    if fn is None:
        raise ParseError(f"Unsupported file type {path.suffix}. Use PDF, DOCX, TXT or MD.")
    parsed = fn(path)
    if parsed.chars < 20:
        raise ParseError("The document appears to be empty.")
    return parsed
