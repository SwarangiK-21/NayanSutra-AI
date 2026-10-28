"""Builds the competitive analysis report from report_data.py:

    NyaySutra_Competitive_Analysis.html   (intermediate, printable)
    NyaySutra_Competitive_Analysis.pdf    (headless Chrome / Edge --print-to-pdf, A4 landscape)
    report.json                           (same tables, consumed by build_docx.js for the editable DOCX)

    python docs/competitive_analysis/build_report.py
    node   docs/competitive_analysis/build_docx.js      (needs the npm package "docx")
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import report_data as d

HERE = Path(__file__).resolve().parent
OUT = HERE / "NyaySutra_Competitive_Analysis"
LABEL = {"V": "Verified", "C": "Vendor claim", "I": "Independent", "E": "Estimate / secondary", "U": "Unknown"}


def T(caption, columns, rows, widths=None, cls=""):
    return {"caption": caption, "columns": list(columns), "rows": [[str(c) for c in r] for r in rows],
            "widths": widths, "cls": cls}


def sections() -> list[dict]:
    S = d.SCORE
    crit = d.CRITERIA
    ny = d.SCORES["NyaySutra-AI"]
    score_rows = [(c[0], c[1], c[2], s, f"{c[2] * s / 10:.1f}", conf, ev) for c, (s, conf, ev) in zip(crit, ny)]
    score_rows.append(("", "Total", 100, "", f"{sum(c[2] * s for c, (s, _, _) in zip(crit, ny)) / 10:.1f} / 100",
                       "", f"= {S['NyaySutra-AI']['evidenced']:.2f} / 10"))
    matrix = []
    for i, c in enumerate(crit):
        row = [f"{c[0]} {c[1]} ({c[2]})"]
        for p in d.PRODUCTS:
            s, conf, _ = d.SCORES[p][i]
            row.append("U" if s is None else f"{s} ({conf})")
        matrix.append(row)
    matrix.append(["Evidenced score /10"] + [f"{S[p]['evidenced']:.2f}" for p in d.PRODUCTS])
    matrix.append(["Evidence coverage (weight %)"] + [f"{S[p]['coverage']}%" for p in d.PRODUCTS])
    matrix.append(["Conservative score /10 (U = 0)"] + [f"{S[p]['conservative']:.2f}" for p in d.PRODUCTS])
    matrix.append(["Overall confidence"] + [S[p]["confidence"] for p in d.PRODUCTS])
    evid = [(p, " · ".join(f"{c[0]}: {e}" for c, (s, _, e) in zip(crit, d.SCORES[p]) if s is not None))
            for p in d.PRODUCTS if p != "NyaySutra-AI"]
    summary_rank = sorted(((p, S[p]) for p in d.PRODUCTS), key=lambda x: -x[1]["coverage"])
    swot = [(f"Strengths", "\n".join("• " + x for x in d.SWOT["Strengths"]),
             "Weaknesses", "\n".join("• " + x for x in d.SWOT["Weaknesses"])),
            ("Opportunities", "\n".join("• " + x for x in d.SWOT["Opportunities"]),
             "Threats", "\n".join("• " + x for x in d.SWOT["Threats"]))]
    road = []
    for phase, items in d.ROADMAP.items():
        road.append(T(phase, ["#", "Action", "Effort", "Impact", "Dependencies", "Cost option", "Acceptance criteria"],
                      [(i, *r) for i, r in enumerate(items, 1)], [3, 27, 6, 7, 14, 12, 31]))
    return [
        {"title": "1. Executive summary", "tables": [
            T("Key findings for mentors", ["Topic", "Finding", "Evidence"], d.EXEC, [18, 66, 16])]},
        {"title": "2. Methodology and evidence labels", "tables": [
            T("Method", ["Step", "What was done", "Evidence"], d.METHOD, [14, 70, 16]),
            T("Evidence labels used in every table", ["Label", "Meaning"],
              [("V", "Verified — tested by us, or read on the vendor's own official page during this audit"),
               ("C", "Vendor claim — stated by the vendor or a vendor-sourced article; not independently checked"),
               ("I", "Independent evidence — third-party study or reporting"),
               ("E", "Estimate / secondary — directory listing or competitor-written comparison; low reliability"),
               ("U", "Unknown — no usable evidence; never scored"),
               ("H / M / L", "Confidence of a score: High / Medium / Low")], [12, 88])]},
        {"title": "3. NyaySutra-AI — what the repository actually contains", "tables": [
            T("Repository and database inventory (inspected, read-only)", ["Area", "Finding", "Source", "Label"],
              d.INVENTORY, [14, 70, 8, 8])]},
        {"title": "4. Prototype test results", "tables": [
            T("Summary", ["Measure", "Result", "Source"], d.eval_summary(), [30, 60, 10]),
            T("Live evaluation — 31 representative questions (automatic pass/fail checks)",
              ["ID", "Category", "Question", "Routed to", "Secs", "Result", "Notes"], d.eval_rows(),
              [5, 14, 36, 10, 6, 7, 22], "small")]},
        {"title": "5. Competitor landscape (India first, selected global)", "tables": [
            T("Products researched", ["Product", "Company", "Type", "Coverage", "Status", "Pricing (label, source)",
                                      "Key AI features (label, source)", "Comparable?"], d.LANDSCAPE,
              [11, 10, 11, 8, 11, 17, 21, 11], "small")]},
        {"title": "6. Named products that could not be verified", "tables": [
            T("Requested names with no matching product", ["Name given", "Search result", "Closest match", "Treatment"],
              d.NOT_FOUND, [14, 26, 36, 24])]},
        {"title": "7. Competitor workflow test (only what was accessible without an account)", "tables": [
            T("Indian Kanoon public search vs NyaySutra, same queries (26 Oct 2026)",
              ["Query", "Indian Kanoon (S2)", "NyaySutra (R2)", "Observation"], d.COMPETITOR_TEST, [18, 28, 28, 26])]},
        {"title": "8. Scoring rubric (100 points)", "tables": [
            T("Criteria, weights and anchors", ["ID", "Criterion", "Weight", "What it measures", "Scale anchors (0–10)"],
              d.CRITERIA, [5, 22, 7, 28, 38])]},
        {"title": "9. Scores", "tables": [
            T("NyaySutra-AI — score by criterion", ["ID", "Criterion", "Weight", "Score /10", "Weighted",
                                                    "Confidence", "Evidence"], score_rows, [5, 25, 7, 8, 9, 9, 37]),
            T("Comparison matrix — score (confidence); U = unknown, not scored",
              ["Criterion (weight)"] + d.PRODUCTS, matrix, [16] + [8.4] * 10, "small matrix totals"),
            T("How to read the totals",
              ["Measure", "Calculation", "Use"],
              [("Evidenced score", "Σ(weight × score) ÷ Σ(weight of evidenced criteria)",
                "Quality where evidence exists; NOT comparable across products with different coverage"),
               ("Evidence coverage", "Σ weight of criteria with a score", "How much of the rubric could be evidenced"),
               ("Conservative score", "Σ(weight × score) ÷ 100 (unknown = 0)", "Lower bound; penalises missing evidence"),
               ("Example of the trap", "Claude 5.72 vs ChatGPT 5.51 evidenced", "Claude is higher only because its "
                "accuracy is unknown (unscored), not because it is better")], [18, 40, 42]),
            T("Evidence behind each competitor score", ["Product", "Criterion: evidence"], evid, [16, 84], "small")]},
        {"title": "10. Defensible position", "tables": [
            T("Maturity assessment vs market ranking", ["Question", "Answer"], d.POSITION, [22, 78])]},
        {"title": "11. NyaySutra audit by dimension", "tables": [
            T("All requested audit dimensions", ["Dimension", "Rating", "Evidence", "How verified", "Gap"],
              d.AUDIT, [15, 10, 40, 13, 22], "small")]},
        {"title": "12. Feature comparison matrix", "tables": [
            T("V = verified, C = vendor claim, ~ = partly / generic, ✗ = absent, ? = unknown, — = not applicable",
              ["Feature", *d.PRODUCTS], d.FEATURES, [16] + [8.4] * 10, "small matrix")]},
        {"title": "13. Where NyaySutra wins and loses", "tables": [
            T("Wins", ["Area", "NyaySutra evidence", "Competitor comparison"], d.WINS, [18, 46, 36]),
            T("Losses", ["Area", "Gap", "Severity"], d.LOSSES, [22, 64, 14])]},
        {"title": "14. Unique selling proposition and target market", "tables": [
            T("Positioning", ["Item", "Statement"], d.USP, [22, 78])]},
        {"title": "15. SWOT analysis", "tables": [
            T("SWOT", ["Quadrant", "Points", "Quadrant", "Points"], swot, [12, 38, 12, 38], "swot")]},
        {"title": "16. Risks", "tables": [
            T("Risk register", ["Risk", "Likelihood", "Impact", "Mitigation"], d.RISKS, [36, 11, 10, 43])]},
        {"title": "17. Feature gaps (prioritised)", "tables": [
            T("Gaps", ["Priority", "Gap", "Severity", "Note"], d.GAPS, [9, 41, 12, 38])]},
        {"title": "18. Roadmap — 30, 60 and 90 days", "tables": road +
            [T("Effort scale", ["Code", "Meaning"], [("S", "≤ 3 days"), ("M", "1–2 weeks"), ("L", "3–4 weeks")], [12, 88])]},
        {"title": "19. Confidence and limitations", "tables": [
            T("Limitations", ["Limitation", "Detail", "Effect on conclusions"], d.LIMITS, [20, 64, 16])]},
        {"title": "20. Source register", "tables": [
            T(f"All sources (accessed {d.ACCESSED})", ["ID", "Source", "Link", "Type (label)"],
              d.SOURCES, [5, 38, 40, 17], "small sources")]},
    ]


CSS = """
@page { size: A4 landscape; margin: 14mm 12mm 16mm 12mm;
  @bottom-center { content: "NyaySutra-AI Competitive Analysis · page " counter(page) " of " counter(pages);
    font: 8pt 'Segoe UI', Arial, sans-serif; color: #5a6577; } }
@page :first { @bottom-center { content: ""; } }
body { font: 9.5pt/1.35 'Segoe UI', 'Noto Sans', Arial, sans-serif; color: #1a2230; }
h1 { color: #0b2c5f; font-size: 22pt; margin: 0 0 6px; }
h2 { color: #0b2c5f; font-size: 14pt; border-bottom: 2px solid #d9750f; padding-bottom: 3px; margin: 18px 0 8px;
     page-break-after: avoid; }
.cover { height: 170mm; display: flex; flex-direction: column; justify-content: center; page-break-after: always; }
.cover .sub { font-size: 12pt; color: #5a6577; margin-bottom: 18px; }
.tricolour { height: 6px; width: 180px; background: linear-gradient(90deg,#ff9933 33%,#fff 33% 66%,#138808 66%);
             border: 1px solid #ddd; margin-bottom: 18px; }
table { width: 100%; border-collapse: collapse; margin: 4px 0 12px; table-layout: fixed; page-break-inside: auto; }
caption { text-align: left; font-weight: 700; color: #0b2c5f; padding: 2px 0 4px; caption-side: top; }
th { background: #0b2c5f; color: #fff; text-align: left; padding: 4px 5px; font-size: 8.8pt; vertical-align: top; }
td { border: 1px solid #c9d1dc; padding: 3px 5px; vertical-align: top; word-wrap: break-word; overflow-wrap: anywhere;
     white-space: pre-line; }
tr { page-break-inside: avoid; }
tr:nth-child(even) td { background: #f5f7fa; }
table.small td, table.small th { font-size: 7.9pt; }
table.matrix td:not(:first-child), table.matrix th:not(:first-child) { text-align: center; }
table.totals tr:nth-last-child(-n+4) td { font-weight: 700; background: #fff8ee; }
table.swot td:nth-child(odd) { font-weight: 700; background: #eef2f8; color: #0b2c5f; }
td.pass { color: #0c5a05; font-weight: 700; } td.fail { color: #b42318; font-weight: 700; }
a { color: #0b4aa2; text-decoration: none; }
.meta { width: 70%; } .meta td:first-child { width: 18%; } .meta td { border: none; background: none !important; padding: 2px 8px 2px 0; }
"""


def cell(v: str, col: str) -> str:
    e = html.escape(v)
    if col == "Link" and re.match(r"https?://", v):
        return f'<td><a href="{e}">{e}</a></td>'
    if v in ("PASS", "FAIL"):
        return f'<td class="{v.lower()}">{v}</td>'
    return f"<td>{e}</td>"


def to_html(secs: list[dict]) -> str:
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(d.TITLE)}</title>"
             f"<style>{CSS}</style></head><body>",
             "<div class='cover'><div class='tricolour'></div>"
             f"<h1>{html.escape(d.TITLE)}</h1><div class='sub'>{html.escape(d.SUBTITLE)}</div>"
             "<table class='meta'>"
             f"<tr><td><b>Project</b></td><td>NyaySutra-AI (repository commit d0243e1)</td></tr>"
             f"<tr><td><b>Date</b></td><td>{d.ACCESSED}</td></tr>"
             f"<tr><td><b>Prepared for</b></td><td>Project mentors</td></tr>"
             f"<tr><td><b>Prepared by</b></td><td>[To be filled in]</td></tr>"
             f"<tr><td><b>Status</b></td><td>Prototype audit + desk research. Competitor AI products were not "
             f"tested (no accounts); see Section 19.</td></tr></table></div>"]
    for s in secs:
        parts.append(f"<section><h2>{html.escape(s['title'])}</h2>")
        for t in s["tables"]:
            cols = "".join(f"<col style='width:{w}%'>" for w in (t["widths"] or []))
            head = "".join(f"<th>{html.escape(c)}</th>" for c in t["columns"])
            body = "".join("<tr>" + "".join(cell(v, t["columns"][i] if i < len(t["columns"]) else "")
                                            for i, v in enumerate(r)) + "</tr>" for r in t["rows"])
            parts.append(f"<table class='{t['cls']}'><caption>{html.escape(t['caption'])}</caption>"
                         f"<colgroup>{cols}</colgroup><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        parts.append("</section>")
    parts.append("</body></html>")
    return "".join(parts)


def chrome() -> str:
    for c in (shutil.which("chrome"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", shutil.which("google-chrome")):
        if c and Path(c).exists():
            return c
    raise SystemExit("Chrome or Edge is needed to print the PDF")


def main() -> None:
    secs = sections()
    OUT.with_suffix(".html").write_text(to_html(secs), encoding="utf-8")
    (HERE / "report.json").write_text(json.dumps(
        {"title": d.TITLE, "subtitle": d.SUBTITLE, "date": d.ACCESSED, "sections": secs}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                        "--no-pdf-header-footer", f"--print-to-pdf={OUT.with_suffix('.pdf')}",
                        OUT.with_suffix(".html").as_uri()], check=True, timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("written:", OUT.with_suffix(".pdf"), OUT.with_suffix(".html"), HERE / "report.json")


if __name__ == "__main__":
    main()
