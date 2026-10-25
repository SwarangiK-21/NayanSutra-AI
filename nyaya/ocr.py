"""OCR for scanned PDFs and images.

Two engines, chosen with OCR_ENGINE (auto = Tesseract when it is installed, else RapidOCR):

* RapidOCR (default, installed with the app's Python packages — no system install or admin rights): PaddleOCR models
  run on ONNX Runtime, downloaded once (about 40 MB) from RapidAI's model hub on first use, or ahead of time with
  `python -m nyaya.ocr download`. The default model reads English; a Devanagari model (Hindi, Marathi, also English)
  is kept in MODEL_CACHE/rapidocr. A page is read
  with the bundled model first; when it is unsure of some lines, the Devanagari model reads the page too and
  replaces those lines where it is more confident — so English, Hindi, Marathi and mixed pages all work. Spaces the recogniser drops in
  Latin text ("Section318") are restored.
* Tesseract (optional native program with eng/hin/mar data; TESSERACT_CMD or PATH).

Low-confidence words are kept but marked as  [?word]  so readers (and the LLM) can see the uncertainty; per-page
confidence is recorded in the document's metadata. Text is never guessed: a page nothing can read stays empty.
"""
from __future__ import annotations

import csv
import re
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

LOW_CONF = 60          # word confidence (0-100) below which a word is marked uncertain
WANTED = ("eng", "hin", "mar")
MAX_IMAGE_PIXELS = 60_000_000
RETRY_BELOW = 0.85     # RapidOCR: a line read with less confidence than this sends the page to the Devanagari model
_lock = threading.Lock()   # one page at a time: OCR is CPU-bound and the engines keep state


class OCRUnavailable(Exception):
    pass


# ── Engine selection ─────────────────────────────────────────────────────────
def _tesseract() -> dict | None:
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


def _rapid_dir() -> Path:
    return config.MODEL_CACHE / "rapidocr"


def _devanagari_ready() -> bool:
    return any(_rapid_dir().rglob("devanagari_*rec*.onnx"))


def _rapidocr() -> dict | None:
    try:
        import rapidocr  # noqa: F401
        from rapidocr import _version
    except Exception:   # not installed, or its native parts failed to load
        return None
    langs = ["eng", "hin", "mar"] if _devanagari_ready() else ["eng"]
    return {"cmd": None, "langs": langs, "version": getattr(_version, "__version__", ""), "engine": "RapidOCR"}


@lru_cache(maxsize=1)
def engine() -> dict | None:
    """{'engine', 'langs', 'version', 'cmd'} for the engine in use, else None. Cached for the process lifetime."""
    choice = config.OCR_ENGINE
    if choice == "off":
        return None
    if choice in ("auto", "tesseract"):
        t = _tesseract()
        if t or choice == "tesseract":
            return t
    return _rapidocr()


def status() -> dict | None:
    e = engine()
    return {"engine": e["engine"], "version": e["version"], "langs": e["langs"]} if e else None


# ── RapidOCR ─────────────────────────────────────────────────────────────────
@lru_cache(maxsize=2)
def _rapid(kind: str):
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    params = {"Global.log_level": "warning", "Global.max_side_len": 3000}
    if kind == "devanagari":   # downloaded once into MODEL_CACHE; the default models ship inside the package
        _rapid_dir().mkdir(parents=True, exist_ok=True)
        params.update({"Global.model_root_dir": str(_rapid_dir()), "Rec.lang_type": LangRec.DEVANAGARI,
                       "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE})
    return RapidOCR(params=params)


def download_models() -> bool:
    """Fetch the OCR models now (first use would otherwise download them). Returns True when Hindi/Marathi is ready."""
    import numpy as np

    blank = np.full((32, 64, 3), 255, dtype=np.uint8)
    try:
        with _lock:
            _rapid("default")(blank)
            _rapid("devanagari")(blank)
    except Exception as exc:
        log.warning("Devanagari OCR model unavailable: %s", exc)
    engine.cache_clear()
    return _devanagari_ready()


def _lines(res) -> list[dict]:
    if res is None or res.boxes is None or res.txts is None:
        return []
    out = []
    for box, txt, score in zip(res.boxes, res.txts, res.scores):
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        out.append({"x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys), "text": (txt or "").strip(),
                    "score": float(score)})
    return [ln for ln in out if ln["text"]]


def _to_text(lines: list[dict]) -> tuple[str, float, int, int]:
    """Lines in reading order (top to bottom, left to right; boxes on the same row are joined)."""
    rows: list[list[dict]] = []
    for ln in sorted(lines, key=lambda r: ((r["y0"] + r["y1"]) / 2, r["x0"])):
        mid, h = (ln["y0"] + ln["y1"]) / 2, ln["y1"] - ln["y0"]
        if rows and abs(mid - rows[-1][0]["_mid"]) < 0.5 * max(h, rows[-1][0]["_h"]):
            rows[-1].append(ln)
        else:
            rows.append([ln])
        ln["_mid"], ln["_h"] = mid, h
    words = low = 0
    confs: list[float] = []
    text_rows = []
    for row in rows:
        parts = []
        for ln in sorted(row, key=lambda r: r["x0"]):
            conf = ln["score"] * 100
            toks = ln["text"].split()
            words += len(toks)
            confs += [conf] * len(toks)
            if conf < LOW_CONF:
                low += len(toks)
                parts.append(" ".join(f"[?{tk}]" for tk in toks))
            else:
                parts.append(ln["text"])
        text_rows.append("  ".join(parts))
    return "\n".join(text_rows), (sum(confs) / len(confs) if confs else 0.0), words, low


_CAMEL = re.compile(r"(?<=[a-z]{3})(?=[A-Z][a-z])")                     # versusVikram (not McDonald)
_WORD_NUM = re.compile(r"(?<=[A-Za-z]{3})(?=\d)")                        # Section318, March2026
_NUM_WORD = re.compile(r"(?<=\d)(?=(?!(?:st|nd|rd|th)\b)[A-Za-z]{2,})")  # 18November, 2,50,000on (not 18th)
_COLON_NUM = re.compile(r"(?<=[A-Za-z])([:;])(?=[\dA-Za-z])")            # hearing:18
_RUPEES = re.compile(r"\bRs\.(?=\d)")


def respace(text: str) -> str:
    """Put back spaces the recogniser dropped in Latin-script text (Devanagari is left as read)."""
    text = _CAMEL.sub(" ", text)
    text = _WORD_NUM.sub(" ", text)
    text = _NUM_WORD.sub(" ", text)
    text = _COLON_NUM.sub(r"\1 ", text)
    return _RUPEES.sub("Rs. ", text)


def _overlap(a: dict, b: dict) -> float:
    """Shared area of two line boxes, relative to the smaller one."""
    w = max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    h = max(0.0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]))
    area = lambda r: max(1.0, (r["x1"] - r["x0"]) * (r["y1"] - r["y0"]))
    return w * h / min(area(a), area(b))


def _improve(lines: list[dict], other: list[dict]) -> list[dict]:
    """Replace each uncertain line by the second model's reading of the same place when that one is more confident;
    lines only the second model found are added. Confident lines are left alone."""
    out = []
    for ln in lines:
        if ln["score"] < RETRY_BELOW:
            best = max(other, key=lambda o: _overlap(ln, o), default=None)
            if best is not None and _overlap(ln, best) > 0.5 and best["score"] > ln["score"] + 0.02:
                ln = best
        out.append(ln)
    out += [o for o in other if o["score"] >= RETRY_BELOW and all(_overlap(o, ln) <= 0.3 for ln in lines)]
    return out


def _ocr_rapid(image) -> dict:
    import numpy as np

    arr = np.asarray(image.convert("RGB"))
    lines = _lines(_rapid("default")(arr))
    used = ["default"]
    # Only pages with uncertain lines (typically Hindi / Marathi text) get the second, Devanagari reading.
    if any(ln["score"] < RETRY_BELOW for ln in lines) and (_devanagari_ready() or config.OCR_DOWNLOAD):
        try:
            lines = _improve(lines, _lines(_rapid("devanagari")(arr)))
            used.append("devanagari")
            engine.cache_clear()   # the model may have just been downloaded: report hin/mar from now on
        except Exception as exc:   # e.g. offline on first use: keep the default reading
            log.warning("Devanagari OCR unavailable: %s", exc)
    for ln in lines:
        ln["text"] = respace(ln["text"])   # only touches Latin letters and digits
    text, conf, words, low = _to_text(lines)
    return {"text": text, "confidence": round(conf, 1), "words": words, "low_words": low, "models": used}


# ── Tesseract ────────────────────────────────────────────────────────────────
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


def _ocr_tesseract(e: dict, image) -> dict:
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


def ocr_image(image) -> dict:
    """OCR one PIL image. Returns {'text', 'confidence', 'words', 'low_words'}."""
    e = engine()
    if not e:
        raise OCRUnavailable()
    with _lock:
        if e["engine"] == "Tesseract":
            return _ocr_tesseract(e, image)
        try:
            return _ocr_rapid(image)
        except Exception as exc:
            log.warning("RapidOCR failed: %s", type(exc).__name__)
            raise RuntimeError("OCR engine error") from exc


def open_image(path: Path):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # decompression-bomb guard
    img = Image.open(path)
    img.load()
    return img


if __name__ == "__main__":   # python -m nyaya.ocr download
    if sys.argv[1:] == ["download"]:
        ok = download_models()
        print("Devanagari (Hindi/Marathi) OCR model ready." if ok else "Could not download the Devanagari model.")
        print("OCR engine:", status())
    else:
        print("usage: python -m nyaya.ocr download")
