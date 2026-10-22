"""Optional OCR for scanned PDFs and images, via the Tesseract command-line engine.

Tesseract is not bundled (it is a native program). Install it with the eng, hin and mar language
data and either put it on PATH or set TESSERACT_CMD in .env. Without it, scanned uploads fail with
a clear message — text is never guessed.

Low-confidence words are kept but marked as  [?word]  so readers (and the LLM) can see the
uncertainty; per-page confidence is recorded in the document's metadata.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

LOW_CONF = 60          # Tesseract word confidence (0-100) below which a word is marked uncertain
WANTED = ("eng", "hin", "mar")
MAX_IMAGE_PIXELS = 60_000_000


class OCRUnavailable(Exception):
    pass


@lru_cache(maxsize=1)
def engine() -> dict | None:
    """{'cmd', 'langs', 'version'} if Tesseract is usable, else None. Cached for the process lifetime."""
    candidates = [config.TESSERACT_CMD, shutil.which("tesseract"),
                  r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
    for cmd in filter(None, candidates):
        if not Path(cmd).exists() and not shutil.which(cmd):
            continue
        try:
            out = subprocess.run([cmd, "--list-langs"], capture_output=True, text=True, timeout=20)
            langs = [ln.strip() for ln in (out.stdout or "").splitlines()[1:] if ln.strip()]
            ver = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=20)
            return {"cmd": cmd, "langs": [lg for lg in WANTED if lg in langs] or langs[:3],
                    "version": (ver.stdout or ver.stderr or "").split("\n", 1)[0].strip(), "engine": "Tesseract"}
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def status() -> dict | None:
    e = engine()
    return {"engine": e["engine"], "version": e["version"], "langs": e["langs"]} if e else None


def _tsv_to_text(tsv: str) -> tuple[str, float, int, int]:
    """Rebuild lines from Tesseract TSV; returns (text, mean confidence, words, low-confidence words)."""
    lines: dict[tuple, list[str]] = {}
    confs: list[float] = []
    low = 0
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        word = (row.get("text") or "").strip()
        if not word:
            continue
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            conf = -1
        if conf >= 0:
            confs.append(conf)
        if 0 <= conf < LOW_CONF:
            low += 1
            word = f"[?{word}]"
        key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        lines.setdefault(key, []).append(word)
    text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
    return text, (sum(confs) / len(confs) if confs else 0.0), len(confs), low


def ocr_image(image) -> dict:
    """OCR one PIL image. Returns {'text', 'confidence', 'words', 'low_words'}."""
    e = engine()
    if not e:
        raise OCRUnavailable()
    with tempfile.TemporaryDirectory(prefix="nyaya_ocr_") as tmp:
        path = os.path.join(tmp, "page.png")
        img = image.convert("L") if image.mode not in ("L", "RGB") else image
        img.save(path)
        try:
            out = subprocess.run([e["cmd"], path, "stdout", "-l", "+".join(e["langs"]), "--psm", "3", "tsv"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("OCR timed out on a page") from exc
    if out.returncode != 0:
        log.warning("tesseract failed: %s", (out.stderr or "")[:300])
        raise RuntimeError("OCR engine error")
    text, conf, words, low = _tsv_to_text(out.stdout)
    return {"text": text, "confidence": round(conf, 1), "words": words, "low_words": low}


def open_image(path: Path):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # decompression-bomb guard
    img = Image.open(path)
    img.load()
    return img
