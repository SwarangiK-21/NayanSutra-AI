"""Active-content checks for uploads, plus an optional ClamAV scan.

Legal files are stored and later served back to other members of the case, so a file that can run code when
opened is refused: PDFs with JavaScript, launch actions, embedded files or rich media; Word files with macros,
embedded objects or remote templates. The user is told how to obtain a clean copy (print to PDF / save as .docx).

ClamAV (free, open source) is used when it is installed (or CLAMAV_CMD points at clamscan/clamdscan). When it is
configured explicitly and fails, uploads are refused (fail closed); when only auto-detected, a failure is logged.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


class Rejected(Exception):
    """The file is refused; the message is safe to show."""


_PDF_ACTIVE = {
    b"/javascript": "JavaScript", b"/js": "JavaScript", b"/launch": "a launch action", b"/embeddedfile": "an embedded file",
    b"/richmedia": "rich media", b"/xfa": "an XFA form", b"/submitform": "a form that submits data",
    b"/importdata": "a data import action", b"/gotoe": "an embedded-file link",
}
_PDF_NAME = re.compile(rb"/[A-Za-z#0-9]+")


def _pdf_names(data: bytes) -> set[bytes]:
    """PDF name tokens, with #xx escapes decoded (e.g. /J#61vaScript) and lower-cased."""
    out = set()
    for m in _PDF_NAME.finditer(data):
        name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda x: bytes([int(x.group(1), 16)]), m.group(0))
        out.add(name.lower())
    return out


def check_pdf(path: Path) -> None:
    data = path.read_bytes()
    found = sorted({label for name, label in _PDF_ACTIVE.items() if name in _pdf_names(data)})
    try:  # objects inside compressed object streams are invisible to the byte scan: ask PDFium as well
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw

        pdf = pdfium.PdfDocument(str(path))
        try:
            if raw.FPDFDoc_GetJavaScriptActionCount(pdf.raw) > 0:
                found.append("JavaScript")
            if raw.FPDFDoc_GetAttachmentCount(pdf.raw) > 0:
                found.append("an embedded file")
        finally:
            pdf.close()
    except Exception as exc:  # unreadable PDFs are reported later by the parser
        log.debug("PDFium active-content check skipped: %s", type(exc).__name__)
    if found:
        raise Rejected(f"This PDF contains {', '.join(sorted(set(found)))}, which could run when opened. "
                       "Print it to a new PDF (or export it again without attachments) and upload that copy.")


def check_docx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            names = [n.lower() for n in z.namelist()]
            if any(n.endswith("vbaproject.bin") or n.endswith("vbadata.xml") for n in names):
                raise Rejected("This Word file contains macros. Save it as a normal .docx (without macros) and upload again.")
            if any(n.startswith("word/embeddings/") or n.startswith("word/activex/") for n in names):
                raise Rejected("This Word file contains embedded objects or controls. Remove them (or save as PDF) "
                               "and upload again.")
            ct = z.read("[Content_Types].xml").decode("utf-8", "ignore").lower() if "[content_types].xml" in names else ""
            if "macroenabled" in ct or "vbaproject" in ct:
                raise Rejected("This is a macro-enabled Word file. Save it as a normal .docx and upload again.")
            for n in names:
                if n.endswith(".rels"):
                    rels = z.read(z.namelist()[names.index(n)]).decode("utf-8", "ignore")
                    if re.search(r'TargetMode\s*=\s*"External"', rels) and re.search(
                            r"/(attachedTemplate|oleObject|frame|subDocument)\"", rels):
                        raise Rejected("This Word file loads content from the internet when opened (remote template "
                                       "or linked object). Save a clean copy and upload again.")
    except zipfile.BadZipFile:
        raise Rejected("The Word file is damaged.") from None


_OLE_MACRO_MARKERS = ("_VBA_PROJECT".encode("utf-16-le"), "Macros".encode("utf-16-le"), b"VBA_PROJECT")


def check_doc(path: Path) -> None:
    data = path.read_bytes()
    if any(m in data for m in _OLE_MACRO_MARKERS):
        raise Rejected("This old-format Word file contains macros. Save it as .docx (without macros) and upload again.")


def _clamav() -> tuple[str | None, bool]:
    """(command, explicitly configured)."""
    if config.CLAMAV_CMD.lower() == "off":
        return None, False
    if config.CLAMAV_CMD:
        return config.CLAMAV_CMD, True
    return shutil.which("clamdscan") or shutil.which("clamscan"), False


def malware_scan_status() -> str:
    cmd, _ = _clamav()
    return f"ClamAV ({Path(cmd).name})" if cmd else "not configured"


def clamav_scan(path: Path) -> None:
    cmd, explicit = _clamav()
    if not cmd:
        return
    try:
        r = subprocess.run([cmd, "--no-summary", str(path)], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ClamAV scan could not run: %s", type(exc).__name__)
        if explicit:
            raise Rejected("The virus scanner is unavailable, so the file was not accepted. Try again later.") from exc
        return
    if r.returncode == 1:
        log.warning("ClamAV flagged an upload")
        raise Rejected("The virus scanner flagged this file. It was not stored.")
    if r.returncode != 0:
        log.warning("ClamAV returned %s", r.returncode)
        if explicit:
            raise Rejected("The virus scanner could not check this file, so it was not accepted.")


def check(path: Path, ext: str) -> None:
    """Raise Rejected if the staged upload must not be stored."""
    if ext == ".pdf":
        check_pdf(path)
    elif ext == ".docx":
        check_docx(path)
    elif ext == ".doc":
        check_doc(path)
    clamav_scan(path)
