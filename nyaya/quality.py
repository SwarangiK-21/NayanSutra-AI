"""Extraction quality checks: blank pages, garbled text and printed page numbers that reveal missing pages.

The result is stored with the document (meta.quality) and shown to the user. A document whose text is mostly
unusable is quarantined: it is kept, but not indexed or offered to the AI, until someone looks at it.
"""
from __future__ import annotations

import re

_SUSPECT = re.compile(r"[�-\x00-\x08\x0b\x0c\x0e-\x1f]")
PAGE_NO = re.compile(r"^\s*(?:page|pg\.?|p\.)?\s*[-–]?\s*(\d{1,4})\s*(?:(?:of|/)\s*(\d{1,4}))?\s*[-–]?\s*$", re.I)


def printed_numbers(raw_pages: list[str]) -> list[tuple[int, int, int | None]]:
    """(position, printed number, printed total) for pages whose first or last lines carry a page number."""
    out = []
    for pos, text in enumerate(raw_pages, 1):
        lines = [ln for ln in text.splitlines() if ln.strip()]
        for ln in lines[:2] + lines[-3:]:
            m = PAGE_NO.match(ln)
            if m and int(m.group(1)) > 0:
                out.append((pos, int(m.group(1)), int(m.group(2)) if m.group(2) else None))
                break
    return out


def missing_pages(found: list[tuple[int, int, int | None]], n_pages: int) -> list[str]:
    """Gaps in the printed numbering compared with the pages actually present ('printed pages 5–6')."""
    if len(found) < 3:   # too few numbers to trust (a lone "12" may be a paragraph number)
        return []
    gaps = []
    for (pa, na, _), (pb, nb, _) in zip(found, found[1:]):
        if nb > na and nb - na > pb - pa:
            first, last = na + (pb - pa), nb - 1
            gaps.append(f"{first}" if first == last else f"{first}–{last}")
    totals = [t for _, _, t in found if t]
    if totals and found[-1][0] == n_pages and max(n for _, n, _ in found) < max(totals):
        hi, lo = max(totals), max(n for _, n, _ in found) + 1
        gaps.append(f"{lo}" if lo == hi else f"{lo}–{hi}")
    return gaps


def assess(pages: list[str], meta: dict | None = None) -> dict:
    meta = meta or {}
    empty, garbled = [], []
    for i, p in enumerate(pages, 1):
        text = p.strip()
        if len(text) < 20:
            empty.append(i)
            continue
        visible = [ch for ch in text if not ch.isspace()]
        suspect = len(_SUSPECT.findall(text)) / max(1, len(visible))
        letters = sum(ch.isalpha() for ch in visible) / max(1, len(visible))
        if suspect > 0.15 or (len(visible) > 200 and letters < 0.35):
            garbled.append(i)
    n = len(pages)
    usable = n - len(empty) - len(garbled)
    gaps = missing_pages([tuple(x) for x in meta.get("printed_page_numbers") or []], n)
    warnings = []
    if empty:
        warnings.append(f"{len(empty)} blank or unreadable page(s): {', '.join(map(str, empty[:20]))}")
    if garbled:
        warnings.append(f"{len(garbled)} page(s) with garbled text (fonts or encoding): {', '.join(map(str, garbled[:20]))}")
    if gaps:
        warnings.append(f"Printed page numbering suggests missing pages: {', '.join(gaps[:10])}")
    if meta.get("ocr_needed_pages"):
        warnings.append(f"Scanned pages without OCR: {', '.join(map(str, meta['ocr_needed_pages'][:20]))}")
    verdict, reason = ("warnings" if warnings else "ok"), ""
    if n and usable == 0:
        verdict, reason = "quarantine", "No page has usable text."
    elif n >= 2 and len(garbled) > n / 2:
        verdict, reason = "quarantine", ("Most pages have garbled text (unusual fonts or encoding), so answers from this "
                                         "document would be unreliable. Export it again as PDF or upload a scan with OCR.")
    return {"verdict": verdict, "reason": reason, "pages": n, "usable_pages": usable, "empty_pages": empty[:200],
            "garbled_pages": garbled[:200], "missing_printed_pages": gaps[:20], "warnings": warnings}
