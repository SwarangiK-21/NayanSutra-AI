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
    meta: dict = {}
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
        # Pages without a text layer (scans) are OCR'd when an engine is installed.
        scanned = [i for i, t in enumerate(raw) if len(t.strip()) < 25]
        if scanned:
            meta.update(_ocr_pdf_pages(doc, scanned, raw, all_scanned=len(scanned) == len(raw)))
    finally:
        doc.close()
    pages = clean_pages(raw)
    total = sum(len(p) for p in pages)
    if total < max(200, 25 * len(pages)) and not meta.get("ocr"):
        raise ParseError(
            "No readable text found — this looks like a scanned PDF. OCR is not installed on this computer "
            "(see README: Tesseract with Hindi/Marathi data), so the text cannot be read reliably."
        )
    return Parsed(pages=pages, meta={"pages": len(pages), **meta})


def _ocr_pdf_pages(doc, idx: list[int], raw: list[str], all_scanned: bool) -> dict:
    from . import ocr

    if not ocr.engine():
        return {"ocr_needed_pages": [i + 1 for i in idx]} if not all_scanned else {}
    if len(idx) > config.OCR_MAX_PAGES:
        raise ParseError(f"{len(idx)} scanned pages need OCR; the limit is {config.OCR_MAX_PAGES} "
                         "(OCR_MAX_PAGES in .env).")
    per_page = []
    for i in idx:
        page = doc[i]
        try:
            img = page.render(scale=300 / 72).to_pil()  # ~300 dpi, what Tesseract is tuned for
        finally:
            page.close()
        try:
            res = ocr.ocr_image(img)
        except RuntimeError as exc:
            raise ParseError(f"OCR failed on page {i + 1}: {exc}.") from exc
        raw[i] = res["text"]
        per_page.append({"page": i + 1, "confidence": res["confidence"], "low_words": res["low_words"],
                         "words": res["words"]})
    words = sum(p["words"] for p in per_page) or 1
    return {"ocr": {"engine": ocr.engine()["engine"], "langs": ocr.engine()["langs"], "pages": per_page,
                    "low_conf_ratio": round(sum(p["low_words"] for p in per_page) / words, 3)}}


def _image(path: Path) -> Parsed:
    from . import ocr

    if not ocr.engine():
        raise ParseError("Image documents need OCR, which is not installed on this computer "
                         "(see README: Tesseract with Hindi/Marathi data).")
    try:
        img = ocr.open_image(path)
    except Exception as exc:  # PIL raises many types, including DecompressionBombError
        raise ParseError("This image could not be opened (damaged, unsupported or too large).") from exc
    frames = []
    try:
        n = getattr(img, "n_frames", 1)  # multi-page TIFF
        for f in range(min(n, config.OCR_MAX_PAGES)):
            img.seek(f)
            frames.append(img.copy())
    except EOFError:
        pass
    pages, per_page = [], []
    for i, frame in enumerate(frames or [img], 1):
        try:
            res = ocr.ocr_image(frame)
        except RuntimeError as exc:
            raise ParseError(f"OCR failed on page {i}: {exc}.") from exc
        pages.append(res["text"])
        per_page.append({"page": i, "confidence": res["confidence"], "low_words": res["low_words"], "words": res["words"]})
    words = sum(p["words"] for p in per_page) or 1
    return Parsed(pages=clean_pages(pages), meta={"pages": len(pages), "ocr": {
        "engine": ocr.engine()["engine"], "langs": ocr.engine()["langs"], "pages": per_page,
        "low_conf_ratio": round(sum(p["low_words"] for p in per_page) / words, 3)}})


def _doc(path: Path) -> Parsed:
    """Legacy Word 97-2003 (.doc) via antiword when available."""
    import shutil
    import subprocess

    exe = shutil.which("antiword")
    if not exe:
        raise ParseError("Legacy .doc files need the 'antiword' tool, which is not installed. "
                         "Open the file in Word and save it as .docx, then upload again.")
    try:
        out = subprocess.run([exe, "-w", "0", str(path)], capture_output=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise ParseError("Reading the .doc file timed out.") from exc
    if out.returncode != 0:
        raise ParseError("This .doc file could not be read. Save it as .docx and upload again.")
    return Parsed(pages=clean_pages([out.stdout.decode("utf-8", errors="replace")]), meta={"pages": 1})


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


EXTRACTORS = {".pdf": _pdf, ".docx": _docx, ".doc": _doc, ".txt": _text, ".md": _text,
              ".png": _image, ".jpg": _image, ".jpeg": _image, ".tif": _image, ".tiff": _image, ".webp": _image}


def extract(path: Path) -> Parsed:
    from . import langdetect

    fn = EXTRACTORS.get(path.suffix.lower())
    if fn is None:
        raise ParseError(f"Unsupported file type {path.suffix}. Use PDF, DOC/DOCX, TXT, MD or an image.")
    parsed = fn(path)
    if parsed.chars < 20:
        raise ParseError("The document appears to be empty.")
    parsed.meta["language"] = langdetect.detect(parsed.text)
    return parsed
