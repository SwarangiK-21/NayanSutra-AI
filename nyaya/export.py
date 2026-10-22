"""Export drafts to Word (.docx) and PDF.

Handles the Markdown subset the drafting engine produces: headings, paragraphs, **bold**, numbered and
bulleted lists, simple tables. [●: …] placeholders stay visible (highlighted) so nothing unknown is
silently dropped; internal fact tags are removed. PDF text is shaped with HarfBuzz so Hindi/Marathi
conjuncts render correctly (font: Nirmala UI on Windows, or EXPORT_FONT / EXPORT_FONT_BOLD in .env).
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

from . import drafting

REVIEW_LABEL = "DRAFT — FOR REVIEW BY A QUALIFIED LEGAL PROFESSIONAL"
_BOLD = re.compile(r"(\*\*[^*]+\*\*)")
_PH = re.compile(r"(\[●[^\]]*\])")
HEADINGS = ("h1", "h2", "h3", "h4")   # note: "hr" (rule) must not be treated as a heading


def _blocks(md: str) -> list[tuple[str, object]]:
    """[(kind, payload)] with kind in h1..h4, p, li, oli, table, hr."""
    out: list[tuple[str, object]] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            out.append((f"h{len(m.group(1))}", m.group(2)))
        elif re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", ln):
            out.append(("hr", None))
        elif re.match(r"^\s*\|.*\|\s*$", ln) and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            rows = [[c.strip() for c in ln.strip().strip("|").split("|")]]
            i += 2
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(("table", rows))
            continue
        elif m := re.match(r"^\s*[-*+]\s+(.*)$", ln):
            out.append(("li", m.group(1)))
        elif m := re.match(r"^\s*(\d+)[.)]\s+(.*)$", ln):
            out.append(("oli", (m.group(1), m.group(2))))
        else:
            out.append(("p", ln.strip()))
        i += 1
    return out


def _runs(text: str) -> list[tuple[str, bool, bool]]:
    """Split inline text into (text, bold, placeholder) runs."""
    runs = []
    for part in _BOLD.split(text):
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**")
        part = part[2:-2] if bold else part
        for sub in _PH.split(part):
            if sub:
                runs.append((sub.replace("`", ""), bold, sub.startswith("[●")))
    return runs


def _prepare(content: str) -> str:
    content = drafting.clean_for_export(content)
    content = re.sub(r"^[\s*_#>]*DRAFT\s*[—-]\s*FOR REVIEW[^\n]*\n", "", content, flags=re.I)  # re-added as a header
    return content


# ── DOCX ─────────────────────────────────────────────────────────────────────
def to_docx(title: str, content: str, sources: list[dict]) -> bytes:
    import docx
    from docx.enum.text import WD_COLOR_INDEX, WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    d = docx.Document()
    st = d.styles["Normal"]
    st.font.name = "Nirmala UI"
    st.font.size = Pt(12)
    p = d.add_paragraph()
    r = p.add_run(REVIEW_LABEL)
    r.bold = True
    r.font.color.rgb = RGBColor(0xB4, 0x23, 0x18)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    def fill(par, text):
        for t, bold, ph in _runs(text):
            run = par.add_run(t)
            run.bold = bold
            if ph:
                run.font.highlight_color = WD_COLOR_INDEX.YELLOW

    for kind, payload in _blocks(_prepare(content)):
        if kind in HEADINGS:
            h = d.add_heading(level=min(int(kind[1]), 4))
            fill(h, str(payload))
        elif kind == "li":
            fill(d.add_paragraph(style="List Bullet"), str(payload))
        elif kind == "oli":
            n, text = payload
            fill(d.add_paragraph(), f"{n}. {text}")
        elif kind == "table":
            rows = payload
            tbl = d.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            tbl.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    fill(tbl.cell(ri, ci).paragraphs[0], cell)
        elif kind == "hr":
            d.add_paragraph("―" * 20)
        else:
            fill(d.add_paragraph(), str(payload))
    if sources:
        d.add_heading("Sources consulted (local library)", level=3)
        for s in sources:
            d.add_paragraph(f"[{s['n']}] {s.get('citation') or s.get('title')}", style="List Bullet")
    d.core_properties.title = title[:200]
    d.core_properties.comments = REVIEW_LABEL
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ── PDF ──────────────────────────────────────────────────────────────────────
def _font_paths() -> tuple[str | None, int, str | None, int]:
    reg, bold = os.getenv("EXPORT_FONT"), os.getenv("EXPORT_FONT_BOLD")
    if reg:
        return reg, 0, bold or reg, 0
    nirmala = Path(r"C:\Windows\Fonts\Nirmala.ttc")
    if nirmala.exists():
        return str(nirmala), 0, str(nirmala), 1
    for cand in ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(cand).exists():
            return cand, 0, cand, 0
    return None, 0, None, 0


def to_pdf(title: str, content: str, sources: list[dict]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(True, 18)
    reg, ri, bold, bi = _font_paths()
    if reg:
        kw = {"collection_font_number": ri} if reg.lower().endswith(".ttc") else {}
        pdf.add_font("Body", "", reg, **kw)
        kwb = {"collection_font_number": bi} if (bold or "").lower().endswith(".ttc") else {}
        pdf.add_font("Body", "B", bold, **kwb)
        family = "Body"
        pdf.set_text_shaping(True)
    else:  # no Unicode font available: core font (Latin only)
        family = "Helvetica"
    pdf.set_title(title[:200])
    pdf.set_subject(REVIEW_LABEL)
    pdf.add_page()
    pdf.set_font(family, "B", 11)
    pdf.set_text_color(180, 35, 24)
    pdf.cell(0, 8, REVIEW_LABEL, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    base = pdf.l_margin

    def para(text: str, size=11.5, style="", indent=0.0):
        # Runs are written one by one (not fpdf2's markdown mode, which would treat "--" as italics).
        pdf.set_left_margin(base + indent)
        pdf.set_x(base + indent)
        h = size * 0.55
        for t, b, ph in _runs(text):
            st = ("B" if b or "B" in style else "") + ("U" if ph else "")  # placeholders are underlined
            pdf.set_font(family, st, size)
            if family == "Helvetica":
                t = t.encode("latin-1", "replace").decode("latin-1")
            pdf.write(h, t)
        pdf.ln(h + 1.5)
        pdf.set_left_margin(base)

    for kind, payload in _blocks(_prepare(content)):
        if kind in HEADINGS:
            para(str(payload), size={"h1": 15, "h2": 13.5, "h3": 12.5}.get(kind, 12), style="B")
        elif kind == "li":
            para("•  " + str(payload), indent=4)
        elif kind == "oli":
            para(f"{payload[0]}.  {payload[1]}", indent=2)
        elif kind == "table":
            pdf.set_font(family, "", 10)
            with pdf.table(text_align="LEFT", line_height=5) as tbl:
                for row in payload:
                    r = tbl.row()
                    for cell in row:
                        r.cell(re.sub(r"\*\*", "", cell))
            pdf.ln(2)
        elif kind == "hr":
            y = pdf.get_y() + 1
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(4)
        else:
            para(str(payload))
    if sources:
        para("Sources consulted (local library)", size=11.5, style="B")
        for s in sources:
            para(f"[{s['n']}] {s.get('citation') or s.get('title')}", size=10, indent=2)
    return bytes(pdf.output())
