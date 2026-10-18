"""Structure-aware chunking for Indian legal documents.

Accuracy in legal RAG depends on chunks that respect legal units, so each
document family gets its own strategy:

* statute   — one chunk per Section / Article / Order-Rule (long provisions are
              split at sub-section boundaries and keep the same label). Every
              chunk carries act, chapter, number and title so "Section 138 NI
              Act" can be looked up exactly, not just semantically.
* judgment  — numbered paragraphs grouped into ~1.2k-char chunks (never split
              mid-paragraph unless a paragraph is huge); label "paras 12–15".
* contract  — numbered clauses grouped the same way; label "clauses 4–5".
* template  — draft templates split at markdown headings.
* generic   — sentence-aware packing with overlap.

Every chunk gets a context header ("The Indian Contract Act, 1872 — Section 73.
Compensation for loss…") that is embedded and keyword-indexed with the text,
which markedly improves retrieval of short provisions.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

TARGET = 1100
MAX_CHARS = 1700
OVERLAP = 160


@dataclass
class Chunk:
    header: str
    text: str
    page_start: int
    page_end: int
    label: str | None = None
    section: str | None = None   # lookup key: S73, A21, O39R1
    title: str | None = None
    chapter: str | None = None


class Paged:
    """Joined document text with char-offset → page-number mapping."""

    def __init__(self, pages: list[str]):
        self.starts: list[int] = []
        parts, pos = [], 0
        for p in pages:
            self.starts.append(pos)
            parts.append(p)
            pos += len(p) + 1
        self.text = "\n".join(parts)

    def page(self, offset: int) -> int:
        return max(1, bisect.bisect_right(self.starts, offset))


# ── Generic sentence packing ─────────────────────────────────────────────────
_SENT_SPLIT = re.compile(r"(?<=[.;:?!])\s+(?=[\"“(\[A-Z0-9])|\n\s*\n")


def _spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Sentence-ish spans of text[start:end], absolute offsets."""
    out, pos = [], start
    for m in _SENT_SPLIT.finditer(text, start, end):
        if m.start() > pos:
            out.append((pos, m.start()))
        pos = m.end()
    if pos < end:
        out.append((pos, end))
    # Hard-split pathological spans (tables, run-on text).
    fixed = []
    for a, b in out:
        while b - a > MAX_CHARS:
            cut = text.rfind(" ", a, a + MAX_CHARS)
            cut = cut if cut > a + MAX_CHARS // 2 else a + MAX_CHARS
            fixed.append((a, cut))
            a = cut
        if b > a:
            fixed.append((a, b))
    return fixed


def pack(text: str, start: int, end: int, target: int = TARGET, overlap: int = OVERLAP) -> list[tuple[int, int]]:
    """Greedy-pack sentence spans into windows of ~target chars with sentence overlap."""
    spans = _spans(text, start, end)
    if not spans:
        return []
    windows, i = [], 0
    while i < len(spans):
        a, j = spans[i][0], i
        while j + 1 < len(spans) and spans[j + 1][1] - a <= target:
            j += 1
        windows.append((a, spans[j][1]))
        if j + 1 >= len(spans):
            break
        # step back over trailing sentences totalling <= overlap chars
        k = j
        while k > i and spans[j][1] - spans[k][0] <= overlap:
            k -= 1
        i = max(i + 1, k + 1)
    return windows


def _mk(paged: Paged, a: int, b: int, header: str, **kw) -> Chunk | None:
    body = paged.text[a:b].strip()
    if len(body) < 25:
        return None
    return Chunk(header=header, text=body, page_start=paged.page(a), page_end=paged.page(max(a, b - 1)), **kw)


def chunk_generic(pages: list[str], title: str) -> list[Chunk]:
    paged = Paged(pages)
    out = []
    for a, b in pack(paged.text, 0, len(paged.text)):
        pa, pb = paged.page(a), paged.page(b - 1)
        label = f"p. {pa}" if pa == pb else f"pp. {pa}–{pb}"
        c = _mk(paged, a, b, f"{title} — {label}", label=label)
        if c:
            out.append(c)
    return out


# ── Statutes ─────────────────────────────────────────────────────────────────
_HEAD = re.compile(r"^\[?\s*(\d{1,3}[A-Z]{0,3})\s*\.\s*(?!\d)(\S.*)$")
_DASH = re.compile(r"^(.{2,260}?)[\s.\]]*(?:[—–―‒]|--|\.-|:-)", re.S)
_CHAPTER = re.compile(r"^(CHAPTER|PART)\s*([IVXLC]+[A-Z]?)\b\.?\s*(.*)$")
_ORDER = re.compile(r"^ORDER\s+([IVXLC]+[A-Z]?)\b\.?\s*(.*)$")
_SCHEDULE = re.compile(
    r"^(?:THE\s+)?(?:(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH|ELEVENTH|TWELFTH)\s+)?SCHEDULE\b"
)
_LOOSE = re.compile(r"^\[?\s*(\d{1,3}[A-Z]?)\.\s+(\S.{3,})$")
_BAD_TITLE = re.compile(r"^(Subs\.|Ins\.|Omitted|Rep\.|Added|Vide|The words|See |Now see)", re.I)
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman_to_int(s: str) -> int:
    s = re.sub(r"[^IVXLC]", "", s.upper())
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def _num(s: str) -> int:
    m = re.match(r"\d+", s)
    return int(m.group()) if m else 0


def _clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip(" .[]")
    return t[:220]


@dataclass
class _Head:
    start: int        # heading line start (absolute)
    body: int         # body start (absolute)
    num: str
    title: str
    kind: str         # section | article | rule | schedule
    container: str    # order roman numeral or schedule tag
    chapter: str


def _heading_name(s: str) -> str:
    s = s.strip(" .—–-:")
    return s.title() if s and s.isupper() else ""


def _lis(heads: list[_Head]) -> list[_Head]:
    """Longest non-decreasing run of provision numbers (drops stray false headings,
    e.g. table-of-contents lines or numbered lists inside a provision)."""
    if not heads:
        return heads
    tails, tails_idx, prev = [], [], [-1] * len(heads)
    for i, h in enumerate(heads):
        n = _num(h.num)
        j = bisect.bisect_right(tails, n)
        if j == len(tails):
            tails.append(n)
            tails_idx.append(i)
        else:
            tails[j] = n
            tails_idx[j] = i
        prev[i] = tails_idx[j - 1] if j > 0 else -1
    out, k = [], tails_idx[-1]
    while k >= 0:
        out.append(heads[k])
        k = prev[k]
    return out[::-1]


def _find_heads(text: str, is_constitution: bool) -> list[_Head]:
    """Two passes: locate where the real body starts (after the table of contents),
    then rescan from just before it so TOC chapter/order headings cannot leak."""
    heads = _scan(text, 0, is_constitution)
    main = [h for h in heads if not h.container] or heads
    if not main:
        return heads
    base = main[0].start
    # Include a CHAPTER/PART heading only if it sits directly above the first provision.
    prev = text[:base].rstrip("\n").split("\n")[-4:]
    pos = base
    for ln in reversed(prev):
        pos = text.rfind(ln, 0, pos)
        if _CHAPTER.match(ln.strip()):
            base = pos
            break
    return _scan(text, base, is_constitution)


def _scan(full: str, base: int, is_constitution: bool) -> list[_Head]:
    text = full[base:]
    lines = text.split("\n")
    offs, pos = [], base
    for ln in lines:
        offs.append(pos)
        pos += len(ln) + 1
    text = full  # offsets below are absolute

    cands: list[_Head] = []
    chapter, order, schedule = "", "", ""
    seen_strict = False
    for i, raw in enumerate(lines):
        ln = raw.strip()
        if seen_strict and len(ln) < 70 and _SCHEDULE.match(ln) and not _DASH.match(ln):
            schedule, order, chapter = ln.title(), "", ln.title()
            continue
        m_ch = _CHAPTER.match(ln)
        if m_ch and len(ln) < 120:
            name = _heading_name(m_ch.group(3) or (lines[i + 1] if i + 1 < len(lines) else ""))
            chapter = f"{m_ch.group(1).title()} {m_ch.group(2)}" + (f" — {name}" if name else "")
            continue
        m_or = _ORDER.match(ln) if schedule else None  # Orders live inside a Schedule (CPC)
        if m_or and len(ln) < 120:
            order = m_or.group(1)
            name = _heading_name(m_or.group(2) or (lines[i + 1] if i + 1 < len(lines) else ""))
            chapter = f"Order {order}" + (f" — {name}" if name else "")
            continue

        line_start = offs[i] + (len(raw) - len(raw.lstrip()))
        m = _HEAD.match(ln)
        if m:
            # Heading text may wrap onto the next line: test the dash pattern on 2 lines.
            rest_off = line_start + ln.index(m.group(2))
            j = min(i + 2, len(lines) - 1)
            window = text[rest_off:offs[j] + len(lines[j])].replace("\n", " ")
            d = _DASH.match(window)
            if d and not _BAD_TITLE.match(d.group(1)) and not re.search(r"\b\d{1,3}[A-Z]?\.\s|\[Repealed", d.group(1)):
                kind = "rule" if order else ("article" if is_constitution else "section")
                cands.append(_Head(line_start, rest_off + d.end(), m.group(1), _clean_title(d.group(1)),
                                   kind, order or schedule, chapter))
                seen_strict = True
                continue
        if schedule and not order:
            lm = _LOOSE.match(ln)
            if lm:
                title = re.split(r"(?<=[a-z])\.\s|\s{2,}", lm.group(2))[0]
                cands.append(_Head(line_start, line_start, lm.group(1), _clean_title(title), "schedule",
                                   schedule, chapter))

    # Keep the longest increasing numbering within each container (main body, each Order, each Schedule).
    groups: dict[str, list[_Head]] = {}
    for h in cands:
        groups.setdefault(h.container, []).append(h)
    kept = [h for g in groups.values() for h in _lis(g)]
    return sorted(kept, key=lambda h: h.start)


def _label(h: _Head, act_short: str) -> tuple[str, str]:
    if h.kind == "rule":
        return f"Order {h.container} Rule {h.num}", f"O{roman_to_int(h.container)}R{h.num}"
    if h.kind == "article":
        return f"Article {h.num}", f"A{h.num}"
    if h.kind == "schedule":
        word = "Article" if "limitation" in act_short.lower() else "Entry"
        return f"{h.container}, {word} {h.num}", f"{'A' if word == 'Article' else 'E'}{h.num}"
    return f"Section {h.num}", f"S{h.num}"


def chunk_statute(pages: list[str], act_title: str, act_short: str = "") -> list[Chunk]:
    paged = Paged(pages)
    text = paged.text
    heads = _find_heads(text, "constitution" in act_title.lower())
    if len(heads) < 3:
        return chunk_generic(pages, act_title)

    out: list[Chunk] = []
    # Preamble: the text just before the first provision (skips the table of contents).
    pre_a = max(0, heads[0].start - 1500)
    pre = text[pre_a:heads[0].start]
    if "ARRANGEMENT OF" not in pre and len(pre.strip()) > 80:
        c = _mk(paged, pre_a, heads[0].start, f"{act_title} — Preamble", label="Preamble")
        if c:
            out.append(c)

    for idx, h in enumerate(heads):
        end = heads[idx + 1].start if idx + 1 < len(heads) else len(text)
        label, key = _label(h, act_short or act_title)
        head = f"{act_title} — {label}. {h.title}" + (f" ({h.chapter})" if h.chapter else "")
        body_text = text[h.body:end].strip()
        if not body_text:
            continue
        if end - h.body <= MAX_CHARS:
            c = _mk(paged, h.body, end, head, label=label, section=key, title=h.title, chapter=h.chapter)
            if c:
                c.page_start = paged.page(h.start)
                out.append(c)
            continue
        windows = pack(text, h.body, end, target=1300, overlap=120)
        for p, (a, b) in enumerate(windows, 1):
            c = _mk(paged, a, b, f"{head} [part {p}/{len(windows)}]", label=label, section=key,
                    title=h.title, chapter=h.chapter)
            if c:
                out.append(c)
    return out


# ── Judgments / contracts: numbered blocks ───────────────────────────────────
_PARA = re.compile(r"^\s*\(?(\d{1,3})[.)]\s+\S", re.M)
_CLAUSE = re.compile(r"^\s*(?:clause\s+|article\s+)?(\d{1,2}(?:\.\d{1,2})?)[.)]?\s+[A-Z\"“]", re.M | re.I)


def _numbered_blocks(text: str, rx: re.Pattern) -> list[tuple[int, str]]:
    """Start offsets of top-level numbered blocks, keeping a monotonically increasing sequence."""
    out, last = [], 0.0
    for m in rx.finditer(text):
        try:
            n = float(m.group(1)) if "." in m.group(1) else float(m.group(1))
        except ValueError:
            continue
        if last < n <= last + 3:
            out.append((m.start(), m.group(1)))
            last = n
    return out


def chunk_blocks(pages: list[str], title: str, kind: str = "judgment") -> list[Chunk]:
    paged = Paged(pages)
    text = paged.text
    rx, word = (_PARA, "para") if kind == "judgment" else (_CLAUSE, "clause")
    blocks = _numbered_blocks(text, rx)
    if len(blocks) < 5:
        return chunk_generic(pages, title)

    out: list[Chunk] = []
    if blocks[0][0] > 80:  # cause title, coram, headnote before para 1
        for a, b in pack(text, 0, blocks[0][0]):
            c = _mk(paged, a, b, f"{title} — opening (parties, bench, background)", label="opening")
            if c:
                out.append(c)

    bounds = [(blocks[i][0], blocks[i + 1][0] if i + 1 < len(blocks) else len(text), blocks[i][1])
              for i in range(len(blocks))]
    group: list[tuple[int, int, str]] = []

    def flush():
        if not group:
            return
        a, b = group[0][0], group[-1][1]
        first, lastn = group[0][2], group[-1][2]
        label = f"{word} {first}" if first == lastn else f"{word}s {first}–{lastn}"
        if b - a <= MAX_CHARS:
            c = _mk(paged, a, b, f"{title} — {label}", label=label)
            if c:
                out.append(c)
        else:
            for wa, wb in pack(text, a, b, target=1300):
                c = _mk(paged, wa, wb, f"{title} — {label}", label=label)
                if c:
                    out.append(c)
        group.clear()

    for blk in bounds:
        size = sum(g[1] - g[0] for g in group)
        if group and size + (blk[1] - blk[0]) > TARGET + 200:
            flush()
        group.append(blk)
    flush()
    return out


# ── Draft templates (markdown) ───────────────────────────────────────────────
def chunk_template(pages: list[str], title: str) -> list[Chunk]:
    paged = Paged(pages)
    text = paged.text
    marks = [m.start() for m in re.finditer(r"^#{1,3} ", text, re.M)] or [0]
    if marks[0] != 0:
        marks.insert(0, 0)
    out = []
    for i, a in enumerate(marks):
        b = marks[i + 1] if i + 1 < len(marks) else len(text)
        heading = text[a:text.find("\n", a) if "\n" in text[a:b] else b].lstrip("# ").strip()[:120]
        for wa, wb in pack(text, a, b, target=1400):
            c = _mk(paged, wa, wb, f"Draft template: {title} — {heading}", label=heading or "template")
            if c:
                out.append(c)
    return out


def chunk_document(pages: list[str], doc_type: str, title: str, act_short: str = "") -> list[Chunk]:
    if doc_type == "statute":
        return chunk_statute(pages, title, act_short)
    if doc_type in ("judgment", "order"):
        return chunk_blocks(pages, title, "judgment")
    if doc_type in ("contract", "pleading"):
        return chunk_blocks(pages, title, "contract")
    if doc_type == "template":
        return chunk_template(pages, title)
    return chunk_generic(pages, title)
