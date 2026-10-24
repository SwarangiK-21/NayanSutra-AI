"""Unified search across saved cases, uploaded documents, bare acts, judgments, drafts & notices, legal news
and legal-awareness items. Purely retrieval — no AI-generated text is mixed into results; each result carries
its source, date, jurisdiction and a link to the original material."""
from __future__ import annotations

import json
import re

from . import awareness, db, retrieval

TYPES = ("case", "document", "bare_act", "judgment", "draft", "news", "awareness")


def _snip(text: str, q: str, n: int = 260) -> str:
    text = re.sub(r"\s+", " ", text or "")
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 2]
    pos = min((text.lower().find(w) for w in words if text.lower().find(w) >= 0), default=0)
    start = max(0, pos - 80)
    return ("…" if start else "") + text[start:start + n] + ("…" if len(text) > start + n else "")


def search(q: str, *, types: set[str] | None = None, court: str = "", state: str = "", year: str = "",
           case_type: str = "", doc_type: str = "", source: str = "", limit: int = 40, principal=None) -> dict:
    """Signed-in users search their own cases, documents and drafts plus the public library; guests the library only."""
    from . import authz

    q = (q or "").strip()[:300]
    types = set(types or TYPES) & set(TYPES)
    mine = authz.accessible_case_ids(principal) if principal is not None else []
    team = authz.team_case_ids(principal) if principal is not None else []
    if principal is None or not principal.can("cases.list"):
        types -= {"case", "document"}
    if principal is None or not principal.can("drafting.use"):
        types -= {"draft"}
    results: list[dict] = []
    if not q:
        return {"results": [], "counts": {}}
    like = f"%{q}%"

    if "case" in types and mine:
        sql = (f"SELECT * FROM cases WHERE id IN ({','.join('?' * len(mine))}) AND (title LIKE ? OR client_name LIKE ? "
               "OR opposite_party LIKE ? OR case_number LIKE ? "
               "OR cnr_number LIKE ? OR fir_number LIKE ? OR description LIKE ? OR acts_sections LIKE ?)")
        args: list = [*mine, *[like] * 8]
        for col, val in (("court", court), ("state", state), ("case_type", case_type)):
            if val:
                sql += f" AND {col} LIKE ?"
                args.append(f"%{val}%")
        if year:
            sql += " AND (filing_year=? OR substr(filing_date,1,4)=?)"
            args += [year, year]
        for c in db.conn().execute(sql + " ORDER BY updated_at DESC LIMIT 20", args):
            results.append({"type": "case", "title": c["title"], "source": "Your case files", "date": c["filing_date"] or c["updated_at"],
                            "jurisdiction": ", ".join(x for x in (c["court"], c["district"], c["state"]) if x),
                            "snippet": _snip(f"{c['case_number'] or ''} {c['description'] or ''}", q), "url": f"#/cases/{c['id']}",
                            "meta": {"case_type": c["case_type"]}, "score": 3.0})

    lib_types = {"bare_act", "judgment"} & types
    if lib_types or "document" in types:
        coll_ok = lib_types | ({"case"} if "document" in types else set())
        only = None
        if lib_types and len(lib_types) == 1:  # e.g. precedent search: don't let bare acts crowd out judgments
            only = {r[0] for r in db.conn().execute("SELECT id FROM documents WHERE collection=? AND status='ready'",
                                                    (next(iter(lib_types)),))} or {"-"}
        ids = retrieval.lexical(q, 60, "library", None, only, exclude=frozenset({"draft"})) if lib_types else []
        # documents of every case the caller can access — lexical() limits case docs to one case
        if "document" in types and mine:
            ids += retrieval.keyword_ids(q, f"d.collection='case' AND d.status='ready' AND d.case_id IN "
                                            f"({','.join('?' * len(mine))})", list(mine), 30)
        seen_docs: dict[str, int] = {}
        hits = retrieval.load_hits(list(dict.fromkeys(ids)))
        doc_ids = sorted({h.doc_id for h in hits})
        metas = {r[0]: json.loads(r[1] or "{}") for r in db.conn().execute(   # one query, not one per result
            f"SELECT id, meta FROM documents WHERE id IN ({','.join('?' * len(doc_ids))})", doc_ids)} if doc_ids else {}
        for h in hits:
            if h.collection not in coll_ok or seen_docs.get(h.doc_id, 0) >= 2:
                continue
            if doc_type and h.doc_type != doc_type:
                continue
            seen_docs[h.doc_id] = seen_docs.get(h.doc_id, 0) + 1
            meta = metas.get(h.doc_id, {})
            kind = "document" if h.collection == "case" else h.collection
            results.append({"type": kind, "title": h.title, "label": h.label, "page": h.page_start,
                            "source": {"bare_act": "India Code (official bare act)", "judgment": meta.get("source") or "Judgment library",
                                       "document": "Your uploaded document"}[kind],
                            "date": meta.get("fetched_at") or meta.get("date"), "jurisdiction": "India" if kind != "document" else "",
                            "snippet": _snip(h.text, q), "url": f"/api/documents/{h.doc_id}/file#page={h.page_start or 1}",
                            "open": f"#/doc/{h.doc_id}" if kind == "document" else None, "score": 2.0 if h.exact else 1.5})

    if "draft" in types:
        own = "(case_id IS NULL AND created_by = ?)" + (f" OR case_id IN ({','.join('?' * len(team))})" if team else "")
        for d in db.conn().execute(f"SELECT id, title, kind, doc_type, forum, updated_at, content FROM drafts WHERE ({own}) "
                                   "AND (title LIKE ? OR content LIKE ?) ORDER BY updated_at DESC LIMIT 15",
                                   (principal.user_id, *team, like, like)):
            results.append({"type": "draft", "title": d["title"], "source": f"Your {d['kind']} (AI-assisted draft)",
                            "date": d["updated_at"], "jurisdiction": d["forum"], "snippet": _snip(d["content"], q),
                            "url": f"#/drafting?draft={d['id']}", "score": 1.2})

    if "news" in types:
        sql, args = "SELECT * FROM source_records WHERE kind='news' AND title LIKE ?", [like]
        if source:
            sql += " AND source=?"
            args.append(source)
        for r in db.conn().execute(sql + " ORDER BY published_at DESC LIMIT 15", args):
            results.append({"type": "news", "title": r["title"], "source": r["source"], "date": r["published_at"],
                            "jurisdiction": r["jurisdiction"], "snippet": "", "url": r["url"], "external": True, "score": 1.0})

    if "awareness" in types:
        for i in awareness.listing(state or "MH", q=q, resolve=False)["items"][:10]:
            results.append({"type": "awareness", "title": i["title"], "source": "NyaySutra legal awareness (curated, with official links)",
                            "date": i.get("last_verified"), "jurisdiction": i["jurisdiction"], "snippet": _snip(i["summary"], q),
                            "url": f"#/awareness?item={i['id']}", "score": 1.1})

    if year:  # cases were filtered in SQL; other results keep undated items and those from that year
        results = [r for r in results if r["type"] == "case" or str(r.get("date") or "")[:4] in ("", year)]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"results": results[:limit], "counts": counts}
