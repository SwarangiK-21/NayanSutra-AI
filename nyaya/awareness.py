"""State-wise legal awareness: curated items (data/awareness/awareness.json) whose statutory provisions are
resolved against the official bare-act library at display time, with official links and verification dates."""
from __future__ import annotations

import json
from functools import lru_cache

from . import config, library

PATH = config.ROOT / "data" / "awareness" / "awareness.json"   # curated content shipped with the app


@lru_cache(maxsize=2)
def _load(mtime: float) -> dict:
    return json.loads(PATH.read_text("utf-8"))


def data() -> dict:
    return _load(PATH.stat().st_mtime)


def listing(state: str = "MH", subject: str | None = None, q: str = "", resolve: bool = True) -> dict:
    """Items for a state; `resolve` attaches the statutory text (not needed for search result lists)."""
    d = data()
    codes = {state, "central"}
    items = [i for i in d["items"] if i["state"] in codes and (not subject or i["subject"] == subject)]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i["title"] + " " + i["summary"] + " " + json.dumps(i.get("faqs", []))).lower()]
    out = []
    for i in items:
        item = dict(i)
        item["jurisdiction"] = "Central law — applies in all states" if i["state"] == "central" else \
            next((s["name"] for s in d["states"] if s["code"] == i["state"]), i["state"])
        item["provisions"] = [library.lookup_provision(act, ref, max_chars=1400) for act, ref in i.get("provisions", [])] \
            if resolve else []
        out.append(item)
    out.sort(key=lambda x: (x["state"] == "central", x["subject"]))  # state-specific first
    return {"states": d["states"], "subjects": d["subjects"], "items": out,
            "disclaimer": "General legal information for awareness, not legal advice for your situation."}
