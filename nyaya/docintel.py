"""Document intelligence: original vs extracted text, English translation, structured English summary
(parties, dates, case numbers, facts, claims, relief, provisions, deadlines, action items) with page links,
and summaries in Hindi / Marathi on request.

Whole-document work sends document text to the configured LLM, so every call requires explicit consent
from the UI and is written to the audit log. Extracted items are checked against the source text and
marked unverified when the quoted value cannot be found on the cited page.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import config, db, ingest, langdetect, llm, prompts

PART_CHARS = 6000


def _doc(doc_id: str) -> dict:
    d = db.row_to_dict(db.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    if not d:
        raise ValueError("Document not found")
    if d["status"] != "ready":
        raise ValueError("The document is not ready yet")
    return d


def derivative(doc_id: str, kind: str) -> dict | None:
    r = db.row_to_dict(db.conn().execute("SELECT * FROM doc_derivatives WHERE doc_id=? AND kind=?", (doc_id, kind)).fetchone())
    return r


def _save(doc_id: str, kind: str, lang: str, content: str, meta: dict) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO doc_derivatives(doc_id, kind, lang, content, meta, created_at) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(doc_id, kind) DO UPDATE SET lang=excluded.lang, content=excluded.content, "
                  "meta=excluded.meta, created_at=excluded.created_at",
                  (doc_id, kind, lang, content, json.dumps(meta, ensure_ascii=False), db.now()))


def extracted_pages(doc_id: str) -> list[str]:
    """Exact extracted text per page (preserved at ingestion); older uploads fall back to the indexed passages."""
    d = derivative(doc_id, "extracted")
    if d:
        try:
            return json.loads(d["content"])
        except ValueError:
            pass
    chunks, _ = ingest.document_text(doc_id)
    pages: dict[int, list[str]] = {}
    for ps, _, text in chunks:
        pages.setdefault(ps or 1, []).append(text)
    return ["\n".join(pages[k]) for k in sorted(pages)]


def overview(doc_id: str) -> dict:
    d = db.row_to_dict(db.conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    if not d:
        raise ValueError("Document not found")
    pages = extracted_pages(doc_id) if d["status"] == "ready" else []
    lang = {k: d["meta"].get(k) for k in ("lang", "lang_name", "script", "lang_confidence")}
    if not lang["lang"] and pages:
        det = langdetect.detect("\n".join(pages))
        lang = {"lang": det["lang"], "lang_name": det["lang_name"], "script": det["script"], "lang_confidence": det["confidence"]}
    out = {"doc": {k: v for k, v in d.items() if k not in ("path", "sha256")}, "language": lang,
           "ocr": d["meta"].get("ocr"), "ocr_needed_pages": d["meta"].get("ocr_needed_pages"),
           "pages": pages[:400], "derivatives": {}}
    for r in db.conn().execute("SELECT kind, lang, content, meta, created_at FROM doc_derivatives WHERE doc_id=? AND kind!='extracted'",
                               (doc_id,)):
        item = db.row_to_dict(r)
        if item["kind"] in ("translation_en",):
            try:
                item["content"] = json.loads(item["content"])
            except ValueError:
                pass
        if item["kind"] == "facts":
            item["content"] = json.loads(item["content"])
        out["derivatives"][item["kind"]] = item
    return out


# ── Translation ──────────────────────────────────────────────────────────────
def _parts(pages: list[str]) -> list[str]:
    """Group pages into ~PART_CHARS pieces, each page tagged with its [p. N] marker."""
    parts, buf = [], ""
    for i, p in enumerate(pages, 1):
        block = f"[p. {i}]\n{p}\n"
        if buf and len(buf) + len(block) > PART_CHARS:
            parts.append(buf)
            buf = ""
        while len(block) > PART_CHARS:  # a very long page is split, keeping its marker on each piece
            parts.append(block[:PART_CHARS])
            block = f"[p. {i}] (cont.)\n" + block[PART_CHARS:]
        buf += block
    if buf:
        parts.append(buf)
    return parts


def _split_by_page(text: str, n_pages: int) -> list[str]:
    out = [""] * n_pages
    cur = 0
    for line in text.split("\n"):
        m = re.match(r"^\s*\[p\.\s*(\d+)\](?:\s*\(cont\.\))?\s*$", line)
        if m:
            cur = min(max(int(m.group(1)) - 1, 0), n_pages - 1)
            continue
        out[cur] += line + "\n"
    return [p.strip() for p in out]


def translate(doc_id: str, progress=lambda *a: None) -> dict:
    d = _doc(doc_id)
    pages = extracted_pages(doc_id)
    lang = d["meta"].get("lang") or langdetect.detect("\n".join(pages))["lang"]
    if lang == "en":
        raise ValueError("This document is already in English — no translation needed.")
    total_chars = sum(len(p) for p in pages)
    if total_chars > config.DOC_LLM_MAX_CHARS:
        raise ValueError(f"The document is too long to translate in one go ({total_chars:,} characters; "
                         f"limit {config.DOC_LLM_MAX_CHARS:,}).")
    src = langdetect.LANG_NAMES.get(lang, "the source language")
    parts = _parts(pages)
    # Resume after an interruption: parts already translated for exactly this text are kept (checkpoint).
    sig = hashlib.sha256("".join(parts).encode()).hexdigest()[:16]
    cp = getattr(progress, "checkpoint", None) or {}
    out = list(cp.get("parts") or []) if cp.get("sig") == sig else []
    for i, part in enumerate(parts, 1):
        progress(i - 1, len(parts), f"Translating part {i} of {len(parts)}" + (" (resumed)" if i <= len(out) else ""))
        if i <= len(out):
            continue
        out.append(llm.chat([{"role": "user", "content": prompts.TRANSLATE.format(src=src, text=part)}], temperature=0.0))
        if hasattr(progress, "save_checkpoint"):
            progress.save_checkpoint({"sig": sig, "parts": out})
    progress(len(parts), len(parts), "Indexing the translation")
    translated = _split_by_page("\n".join(out), len(pages))
    uncertain = sum(t.count("[uncertain translation]") + t.count("[unclear:") for t in translated)
    meta = {"source_lang": lang, "model": config.LLM_MODEL, "parts": len(parts), "uncertain_marks": uncertain}
    _save(doc_id, "translation_en", "en", json.dumps(translated, ensure_ascii=False), meta)
    derived = ingest.index_derived(d, translated, f"{d['title']} (English translation)", "translation_en")
    db.audit("document.translate", "document", doc_id, source_lang=lang, derived_doc=derived, sent_to_llm=True)
    return {"pages": len(translated), "uncertain_marks": uncertain}


# ── Structured summary ───────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[\s.,:;()\-/]+", "", (s or "").lower())


def verify_items(facts: dict, pages: list[str]) -> dict:
    """Check that exact values (names, numbers, provisions) occur in the document, on the cited page if given."""
    whole = _norm("\n".join(pages))

    def check(item: dict, field: str) -> dict:
        val = _norm(str(item.get(field) or ""))
        page = item.get("page")
        on_page = None
        if isinstance(page, int) and 1 <= page <= len(pages):
            on_page = bool(val) and val in _norm(pages[page - 1])
        found = bool(val) and val in whole
        item["verified"] = bool(on_page) if on_page is not None else found
        if found and on_page is False:
            item["note"] = "found in the document, but not on the cited page"
        elif not found:
            item["note"] = "not found verbatim in the document — verify"
        return item

    for key, field in (("parties", "name"), ("case_numbers", "value"), ("legal_provisions", "reference")):
        facts[key] = [check(dict(x), field) for x in facts.get(key) or [] if isinstance(x, dict)]
    for key in ("dates", "key_facts", "claims", "relief_requested", "deadlines"):
        facts[key] = [dict(x, page_valid=isinstance(x.get("page"), int) and 1 <= x["page"] <= len(pages))
                      for x in facts.get(key) or [] if isinstance(x, dict)]
    return facts


def summarize(doc_id: str, lang: str = "en", progress=lambda *a: None) -> dict:
    d = _doc(doc_id)
    pages = extracted_pages(doc_id)
    tr = derivative(doc_id, "translation_en")
    src_lang = d["meta"].get("lang") or "en"
    use_pages = json.loads(tr["content"]) if tr else pages
    basis = "english_translation" if tr else ("original_english" if src_lang == "en" else "original_text")
    facts_row = derivative(doc_id, "facts")
    if facts_row and json.loads(facts_row["meta"] or "{}").get("basis") == basis:
        facts = json.loads(facts_row["content"])
    else:
        body = "\n".join(f"[p. {i}] {p}" for i, p in enumerate(use_pages, 1))
        if len(body) > config.DOC_LLM_MAX_CHARS:
            raise ValueError(f"The document is too long to summarise in one go ({len(body):,} characters).")
        if len(body) > config.DOC_ANALYSIS_MAX_CHARS:
            parts = [body[i:i + 40000] for i in range(0, len(body), 40000)]
            sig = hashlib.sha256(body.encode()).hexdigest()[:16]
            cp = getattr(progress, "checkpoint", None) or {}
            notes = list(cp.get("notes") or []) if cp.get("sig") == sig else []   # resume after an interruption
            for i, part in enumerate(parts, 1):
                progress(i - 1, len(parts) + 1, f"Reading part {i} of {len(parts)}")
                if i <= len(notes):
                    continue
                notes.append(llm.chat([{"role": "user", "content": prompts.MAP_NOTES.format(
                    task="facts, parties, dates, case numbers, provisions, relief, deadlines", request="structured summary",
                    i=i, n=len(parts), text=part)}], model=config.FAST_LLM_MODEL))
                if hasattr(progress, "save_checkpoint"):
                    progress.save_checkpoint({"sig": sig, "notes": notes})
            body = "\n\n".join(f"NOTES ON PART {i}:\n{n}" for i, n in enumerate(notes, 1))
        progress(0, 2, "Extracting parties, dates, provisions and deadlines")
        facts = llm.chat_json([{"role": "user", "content": prompts.DOC_FACTS.format(title=d["title"], text=body)}],
                              default={})
        if not facts:
            raise llm.LLMError("The model did not return a usable summary. Please try again.")
        facts = verify_items(facts, use_pages)
        _save(doc_id, "facts", "en", json.dumps(facts, ensure_ascii=False),
              {"basis": basis, "source_lang": src_lang, "model": config.LLM_MODEL})
        db.audit("document.summarize", "document", doc_id, basis=basis, sent_to_llm=True)
    result = {"facts": facts, "basis": basis}
    if lang in ("hi", "mr"):
        progress(1, 2, f"Writing the summary in {prompts.LANG_NAMES[lang]}")
        text = llm.chat([{"role": "user", "content": prompts.SUMMARY_IN_LANG.format(
            lang=prompts.LANG_NAMES[lang], facts=json.dumps(facts, ensure_ascii=False)[:30000])}])
        _save(doc_id, f"summary_{lang}", lang, text, {"basis": basis, "model": config.LLM_MODEL})
        result["summary"] = text
    progress(2, 2, "Done")
    return result
