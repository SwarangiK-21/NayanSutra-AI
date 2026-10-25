"""Build docs/NyaySutra_Project_Documentation.pdf from NyaySutra_Project_Documentation.md.

    python docs/project_documentation/build_pdf.py

Needs: the Python packages `markdown` and `pypdfium2`, and Google Chrome or Microsoft Edge (used headless to print
HTML to PDF; page footers use CSS page-margin boxes, Chrome 131+). Two passes: the first PDF tells us on which page
each chapter starts, the second fills those numbers into the table of contents.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown
import pypdfium2 as pdfium

HERE = Path(__file__).resolve().parent
SRC = HERE / "NyaySutra_Project_Documentation.md"
HTML_OUT = HERE / "NyaySutra_Project_Documentation.html"
PDF_OUT = HERE.parent / "NyaySutra_Project_Documentation.pdf"
TITLE = "NyaySutra: Complete Project Documentation — Features, Technology Stack, Architecture, Data Sources and Workflow"

CSS = r"""
@page { size: A4; margin: 18mm 16mm 20mm 16mm;
  @bottom-left { content: "NyaySutra — Project Documentation (prototype)"; font: 8pt 'Segoe UI', Arial, sans-serif; color: #6b7686; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 8pt 'Segoe UI', Arial, sans-serif; color: #6b7686; }
  @top-right { content: "Draft for mentor review · 25 October 2026"; font: 7.5pt 'Segoe UI', Arial, sans-serif; color: #9aa4b2; }
}
@page :first { margin: 0; @bottom-left { content: none } @bottom-right { content: none } @top-right { content: none } }
:root { --navy: #0b2c5f; --saffron: #ff9933; --saffron-d: #d9750f; --green: #138808; --muted: #5a6577; --line: #d5dbe5; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: 'Segoe UI', 'Noto Sans', 'Nirmala UI', Arial, sans-serif; font-size: 10pt; line-height: 1.5; color: #1a2230; margin: 0; }
h1 { break-before: page; font-size: 19pt; color: var(--navy); margin: 0 0 10pt; padding: 0 0 6pt 10pt; border-left: 5px solid var(--saffron);
     border-bottom: 1px solid var(--line); line-height: 1.25; }
h2 { font-size: 12.5pt; color: var(--navy); margin: 16pt 0 6pt; break-after: avoid; }
h3 { font-size: 11pt; color: var(--navy); margin: 12pt 0 4pt; break-after: avoid; }
p { margin: 0 0 7pt; } ul, ol { margin: 0 0 8pt; padding-left: 18pt; } li { margin: 2pt 0; }
a { color: #0a4fb4; text-decoration: none; }
code { font-family: Consolas, 'Courier New', monospace; font-size: 8.8pt; background: #f1f4f8; padding: 0 3px; border-radius: 3px; }
strong { color: #0e1a2b; }
table { width: 100%; border-collapse: collapse; margin: 6pt 0 12pt; font-size: 8.8pt; }
th { background: var(--navy); color: #fff; text-align: left; padding: 5pt 6pt; font-weight: 600; vertical-align: bottom; }
td { padding: 4.5pt 6pt; border-bottom: 1px solid var(--line); vertical-align: top; overflow-wrap: anywhere; }
tr { break-inside: avoid; } thead { display: table-header-group; }
tr:nth-child(even) td { background: #f8fafc; }
table.wide { font-size: 7.9pt; table-layout: fixed; } table.wide td, table.wide th { padding: 3.5pt 4.5pt; }
table.wide th:nth-child(1) { width: 19%; } table.wide th:nth-child(2) { width: 13%; } table.wide th:nth-child(3) { width: 18%; }
table.wide th:nth-child(4) { width: 17%; } table.wide th:nth-child(5) { width: 19%; } table.wide th:nth-child(6) { width: 14%; }
table.numbered td:first-child, table.numbered th:first-child { width: 20pt; white-space: nowrap; }
figure { margin: 8pt 0 12pt; break-inside: avoid; text-align: center; }
figure img { width: 100%; border: 1px solid var(--line); border-radius: 4px; }
figcaption { font-size: 8.5pt; color: var(--muted); margin-top: 4pt; }
p > img { display: block; max-width: 66%; margin: 6pt auto 2pt; border: 1px solid var(--line); border-radius: 4px; break-inside: avoid; }
p.shot { text-align: center; font-size: 8.5pt; color: var(--muted); break-inside: avoid; margin-bottom: 12pt; }
.st { display: inline-block; font-size: 7.6pt; font-weight: 700; padding: 1pt 6pt; border-radius: 9pt; border: 1px solid; vertical-align: middle;
      letter-spacing: .02em; white-space: nowrap; line-height: 1.4; }
.st.ok { background: #e7f4e4; border-color: #9fd193; color: #0c5a05; }
.st.part { background: #fff4e0; border-color: #f0c98a; color: #8a4b00; }
.st.plan { background: #eaf1fb; border-color: #b9cdee; color: #0b2c5f; }
.st.ver { background: #fdecea; border-color: #f1b0a9; color: #a1170c; }
h2 .st { font-size: 7.4pt; margin-left: 4pt; }
/* cover */
.cover { height: 297mm; padding: 30mm 22mm 20mm; position: relative; background: #fff; }
.cover-band { position: absolute; top: 0; left: 0; right: 0; height: 12mm;
  background: linear-gradient(to bottom, var(--saffron) 0 33%, #fff 33% 66%, var(--green) 66% 100%); }
.cover-kicker { margin-top: 18mm; text-transform: uppercase; letter-spacing: .12em; font-size: 9pt; color: var(--saffron-d); font-weight: 700; }
.cover-title { break-before: auto; border: 0; padding: 0; font-size: 30pt; color: var(--navy); margin: 4mm 0 2mm; line-height: 1.15; }
.cover-sub { font-size: 14pt; color: var(--navy); margin-bottom: 10mm; }
.cover-tag { font-size: 11pt; color: #33405a; border-left: 4px solid var(--saffron); padding-left: 10pt; margin-bottom: 12mm; max-width: 150mm; }
table.cover-meta { font-size: 9.5pt; max-width: 165mm; } table.cover-meta td { background: #fff !important; padding: 6pt 8pt; }
table.cover-meta td:first-child { width: 42mm; color: var(--muted); font-weight: 600; }
td.fill { color: #a1170c; font-style: italic; }
.legend { position: absolute; left: 22mm; right: 22mm; bottom: 22mm; border: 1px solid var(--line); border-radius: 6px; padding: 8pt 12pt; font-size: 8.8pt; }
.legend-h { font-weight: 700; color: var(--navy); margin-bottom: 4pt; }
/* table of contents */
.toc { break-before: page; }
.toc h1 { break-before: avoid; }
.toc ol { list-style: none; padding: 0; margin: 0; }
.toc li { display: flex; align-items: baseline; gap: 6pt; padding: 3.4pt 0; border-bottom: 1px dotted #c9d2e0; font-size: 10.2pt; }
.toc li .t { flex: 1; } .toc li .n { min-width: 22pt; text-align: right; color: var(--navy); font-weight: 600; }
.toc li.sub { padding-left: 16pt; font-size: 9pt; color: #33405a; border-bottom-style: none; }
"""


def chrome() -> str:
    for c in (shutil.which("chrome"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", shutil.which("google-chrome"),
              shutil.which("chromium")):
        if c and Path(c).exists():
            return c
    sys.exit("Chrome or Edge is needed to print the PDF.")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def render_body() -> tuple[str, list[tuple[str, str, int]]]:
    md = SRC.read_text("utf-8")
    md = re.sub(r"<!--.*?-->", "", md, count=1, flags=re.S)
    body = markdown.markdown(md, extensions=["tables", "md_in_html", "attr_list", "sane_lists"])
    heads: list[tuple[str, str, int]] = []

    def tag(m: re.Match) -> str:
        level, inner = int(m.group(1)), m.group(2)
        plain = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if level == 1 and 'class="cover-title"' in m.group(0):
            return m.group(0)
        hid = slug(plain)
        heads.append((hid, re.sub(r"\s*(Implemented|Partial coverage|Partial|E-mail not configured|Planned)\s*", " ", plain).strip(), level))
        return f'<h{level} id="{hid}">{inner}</h{level}>'
    body = re.sub(r"<h([12])(?: [^>]*)?>(.*?)</h\1>", tag, body, flags=re.S)
    body = body.replace("<table>\n<thead>\n<tr>\n<th>#</th>", '<table class="numbered">\n<thead>\n<tr>\n<th>#</th>')
    # Wide technology table: smaller type; screenshot captions under each image.
    body = body.replace("<table>\n<thead>\n<tr>\n<th>Technology / platform</th>", '<table class="wide">\n<thead>\n<tr>\n<th>Technology / platform</th>')
    body = re.sub(r'<p><img alt="([^"]+)" src="([^"]+)" /></p>',
                  lambda m: f'<p class="shot"><img alt="{m.group(1)}" src="{m.group(2)}" /><br>{m.group(1)}</p>', body)
    return body, heads


def toc_html(heads: list[tuple[str, str, int]], pages: dict[str, int]) -> str:
    items = []
    for hid, text, level in heads:
        if level == 2 and not text.startswith("4."):
            continue   # sub-sections listed only for the feature chapter
        n = pages.get(hid, "")
        cls = ' class="sub"' if level == 2 else ""
        items.append(f'<li{cls}><span class="t"><a href="#{hid}">{html.escape(text)}</a></span><span class="n">{n}</span></li>')
    return f'<div class="toc"><h1 id="contents">Contents</h1><ol>{"".join(items)}</ol></div>'


def page_html(body: str, toc: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(TITLE)}</title>'
            f"<style>{CSS}</style></head><body>{body.replace('<div id=\"toc-placeholder\"></div>', toc)}</body></html>")


def print_pdf(src_html: Path, out_pdf: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="nyaya_pdf_") as profile:
        cmd = [chrome(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
               "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw", "--virtual-time-budget=8000",
               f"--print-to-pdf={out_pdf}", src_html.resolve().as_uri()]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)


def heading_pages(pdf_path: Path, heads: list[tuple[str, str, int]]) -> dict[str, int]:
    doc = pdfium.PdfDocument(str(pdf_path))
    texts = []
    for i in range(len(doc)):
        tp = doc[i].get_textpage()
        texts.append(re.sub(r"\s+", " ", tp.get_text_range()))
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    ntexts = [norm(t) for t in texts]
    start = next(i for i, t in enumerate(texts) if "Contents" in t) + 1
    while start < len(texts) and ("Contents" in texts[start] and "Executive Summary" in texts[start]):
        start += 1
    out, cursor = {}, start
    for hid, text, level in heads:
        key = norm(text)[:40]
        for i in range(cursor, len(ntexts)):
            if key and key in ntexts[i]:
                out[hid] = i + 1
                cursor = i
                break
    doc.close()
    return out


def main() -> None:
    body, heads = render_body()
    HTML_OUT.write_text(page_html(body, toc_html(heads, {})), "utf-8")
    print_pdf(HTML_OUT, PDF_OUT)
    pages = heading_pages(PDF_OUT, heads)
    HTML_OUT.write_text(page_html(body, toc_html(heads, pages)), "utf-8")
    print_pdf(HTML_OUT, PDF_OUT)
    check = heading_pages(PDF_OUT, heads)
    moved = {h: (pages.get(h), check.get(h)) for h in pages if pages.get(h) != check.get(h)}
    missing = [t for h, t, lvl in heads if lvl == 1 and h not in check]
    n = len(pdfium.PdfDocument(str(PDF_OUT)))
    print(f"PDF: {PDF_OUT} ({n} pages); chapters located: {sum(1 for h, _, l in heads if l == 1 and h in check)}")
    if moved:
        print("WARNING: page numbers moved between passes:", moved)
    if missing:
        print("WARNING: chapters not found in the PDF text:", missing)


if __name__ == "__main__":
    main()
