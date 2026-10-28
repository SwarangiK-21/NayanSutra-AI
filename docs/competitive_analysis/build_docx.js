// Builds NyaySutra_Competitive_Analysis.docx (editable) from report.json written by build_report.py.
//   python docs/competitive_analysis/build_report.py
//   node docs/competitive_analysis/build_docx.js        (needs the npm package "docx": npm install docx)
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, WidthType, ShadingType, HeadingLevel,
  AlignmentType, PageOrientation, Footer, PageNumber, ExternalHyperlink, TableOfContents, PageBreak, BorderStyle,
} = require("docx");

const here = __dirname;
const R = JSON.parse(fs.readFileSync(path.join(here, "report.json"), "utf8"));
const NAVY = "0B2C5F", SAFFRON = "D9750F", GREY = "5A6577", ZEBRA = "F5F7FA", TOTAL = "FFF8EE";
const PAGE_W = 16838, PAGE_H = 11906, MARGIN = 720, CONTENT_W = PAGE_W - 2 * MARGIN;   // A4 landscape, 0.5" margins
const border = { style: BorderStyle.SINGLE, size: 4, color: "C9D1DC" };
const borders = { top: border, bottom: border, left: border, right: border };

function widthsFor(t) {
  const n = t.columns.length;
  let w = t.widths && t.widths.length === n ? t.widths : Array(n).fill(100 / n);
  const sum = w.reduce((a, b) => a + b, 0);
  const dxa = w.map((x) => Math.floor((x / sum) * CONTENT_W));
  dxa[dxa.length - 1] += CONTENT_W - dxa.reduce((a, b) => a + b, 0);   // columns must sum to the table width
  return dxa;
}

function runs(text, opts) {
  return String(text).split("\n").map((line) => new Paragraph({
    spacing: { before: 20, after: 20 },
    children: [new TextRun({ text: line, ...opts })],
  }));
}

function cellParas(text, col, opts) {
  if (col === "Link" && /^https?:\/\//.test(text)) {
    return [new Paragraph({ children: [new ExternalHyperlink({ link: text,
      children: [new TextRun({ text, style: "Hyperlink", ...opts })] })] })];
  }
  if (text === "PASS" || text === "FAIL") {
    return runs(text, { ...opts, bold: true, color: text === "PASS" ? "0C5A05" : "B42318" });
  }
  return runs(text, opts);
}

function table(t) {
  const w = widthsFor(t);
  const small = /small/.test(t.cls || "");
  const size = small ? 15 : 17;            // half-points: 7.5 pt / 8.5 pt
  const totals = /totals/.test(t.cls || "");
  const header = new TableRow({
    tableHeader: true,
    children: t.columns.map((c, i) => new TableCell({
      borders, width: { size: w[i], type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, color: "auto", fill: NAVY },
      margins: { top: 40, bottom: 40, left: 70, right: 70 },
      children: runs(c, { bold: true, color: "FFFFFF", size }),
    })),
  });
  const rows = t.rows.map((r, ri) => {
    const isTotal = totals && ri >= t.rows.length - 4;
    return new TableRow({
      cantSplit: true,
      children: t.columns.map((col, i) => new TableCell({
        borders, width: { size: w[i], type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, color: "auto", fill: isTotal ? TOTAL : ri % 2 ? ZEBRA : "FFFFFF" },
        margins: { top: 30, bottom: 30, left: 70, right: 70 },
        children: cellParas(r[i] ?? "", col, { size, bold: isTotal || (t.cls === "swot" && i % 2 === 0) }),
      })),
    });
  });
  return new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: w, rows: [header, ...rows] });
}

const body = [];
// Cover
body.push(new Paragraph({ spacing: { before: 2400 }, border: { bottom: { style: BorderStyle.SINGLE, size: 18, color: SAFFRON, space: 4 } },
  children: [new TextRun({ text: R.title, bold: true, size: 44, color: NAVY })] }));
body.push(new Paragraph({ spacing: { after: 300 }, children: [new TextRun({ text: R.subtitle, size: 24, color: GREY })] }));
for (const [k, v] of [["Project", "NyaySutra-AI (repository commit d0243e1)"], ["Date", R.date],
  ["Prepared for", "Project mentors"], ["Prepared by", "[To be filled in]"],
  ["Status", "Prototype audit + desk research. Competitor AI products were not tested (no accounts); see Section 19."]]) {
  body.push(new Paragraph({ children: [new TextRun({ text: `${k}: `, bold: true, size: 20 }), new TextRun({ text: v, size: 20 })] }));
}
body.push(new Paragraph({ children: [new PageBreak()] }));
body.push(new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: "Contents", bold: true, size: 28, color: NAVY })] }));
body.push(new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-1" }));
body.push(new Paragraph({ children: [new TextRun({ text: "(Right-click the contents and choose Update Field to fill in page numbers.)", italics: true, size: 16, color: GREY })] }));
body.push(new Paragraph({ children: [new PageBreak()] }));

for (const s of R.sections) {
  body.push(new Paragraph({ heading: HeadingLevel.HEADING_1, pageBreakBefore: false, children: [new TextRun(s.title)] }));
  for (const t of s.tables) {
    body.push(new Paragraph({ keepNext: true, spacing: { before: 120, after: 60 },
      children: [new TextRun({ text: t.caption, bold: true, color: NAVY, size: 19 })] }));
    body.push(table(t));
  }
}

const doc = new Document({
  creator: "NyaySutra-AI project", title: R.title, description: R.subtitle,
  styles: {
    default: { document: { run: { font: "Segoe UI", size: 18 } } },
    paragraphStyles: [{ id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { size: 28, bold: true, color: NAVY }, paragraph: { spacing: { before: 280, after: 120 }, outlineLevel: 0,
        border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: SAFFRON, space: 2 } } } }],
  },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838, orientation: PageOrientation.LANDSCAPE },
      margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [
      new TextRun({ text: "NyaySutra-AI Competitive Analysis · page ", size: 16, color: GREY }),
      new TextRun({ children: [PageNumber.CURRENT], size: 16, color: GREY }),
      new TextRun({ text: " of ", size: 16, color: GREY }),
      new TextRun({ children: [PageNumber.TOTAL_PAGES], size: 16, color: GREY })] })] }) },
    children: body,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  const out = path.join(here, "NyaySutra_Competitive_Analysis.docx");
  fs.writeFileSync(out, buf);
  console.log("written:", out, buf.length, "bytes");
});
