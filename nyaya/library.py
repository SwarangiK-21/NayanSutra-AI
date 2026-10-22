"""Provision verification against the local bare-act library (official India Code copies).

`lookup_provision("CPC", "Order VII Rule 1")` returns the indexed passage, its page, a link to the
original PDF and the India Code provenance — or found=False. Nothing in the app states what a
provision says without going through here.
"""
from __future__ import annotations

import json
from functools import lru_cache

from . import citations, config, db, retrieval


@lru_cache(maxsize=1)
def _manifest() -> dict:
    p = config.BARE_ACTS_DIR / "manifest.json"
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def act_meta(doc_title: str) -> dict:
    for m in _manifest().values():
        if m.get("title") == doc_title:
            return m
    return {}


def lookup_provision(act: str, ref: str, max_chars: int = 1600) -> dict:
    out = {"act": act, "ref": ref, "found": False}
    refs = citations.parse_refs(f"{ref} of the {act}") or citations.parse_refs(f"{ref} {act}")
    refs = [r for r in refs if r.act] or refs
    if not refs:
        return out
    ids = retrieval.exact(refs[:1], "library", None, None)
    hits = [h for h in retrieval.load_hits(ids) if h.collection == "bare_act"]
    if not hits:
        return out
    # Split provisions are stored in parts: join the parts of the same provision in order.
    first = hits[0]
    parts = [h for h in hits if h.doc_id == first.doc_id and h.section == first.section]
    text = "\n…\n".join(h.text for h in parts)
    m = act_meta(first.title)
    out.update(found=True, label=first.label, heading=first.header, doc_id=first.doc_id, act_title=first.title,
               page=first.page_start, chunk_id=first.chunk_id, text=text[:max_chars], truncated=len(text) > max_chars,
               pdf_url=f"/api/documents/{first.doc_id}/file#page={first.page_start or 1}",
               official_url=m.get("item_url"), fetched_at=m.get("fetched_at"), repealed=bool(m.get("repealed")),
               enforcement_date=m.get("enforcement_date"))
    return out


def act_titles() -> list[str]:
    return [r["title"] for r in db.conn().execute("SELECT title FROM documents WHERE collection='bare_act' ORDER BY title")]
