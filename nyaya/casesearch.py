"""Case information search across any combination of identifiers.

What it does — honestly, given what the official sources allow:
  1. validates the identifiers and asks for what is missing (an FIR number alone identifies nothing:
     state, district, police station and year are needed);
  2. infers the jurisdiction (CNR prefix → state and year; court name → official portal);
  3. finds matching matters in the user's own workspace and records previously imported from official portals;
  4. lists the official source(s) to consult, with the exact values to enter, because those portals are
     CAPTCHA-protected and offer no API — automated retrieval would mean bypassing access controls;
  5. lets the user import what they read there (record + orders), with source, retrieval time and the
     fields left unverified, and save it to a case. Re-imports are compared field by field and changes notified.
It never reports that a court or police record exists unless a matching record is actually on file.
"""
from __future__ import annotations

import json
import re

from . import cases, db, integrations, reference

ID_FIELDS = ("cnr_number", "case_number", "filing_number", "fir_number")
PARTY_FIELDS = ("petitioner", "respondent")
TRACKED = ("status", "next_hearing_date", "stage", "court", "judge", "sections", "orders")


def _clean(p: dict) -> dict:
    out = {}
    for k, v in p.items():
        if v is None or isinstance(v, (list, dict)):
            continue
        v = re.sub(r"\s+", " ", str(v)).strip()[:200]
        if v:
            out[k] = v
    return out


def validate(p: dict) -> tuple[dict, list[dict], list[dict]]:
    """Returns (normalised params, errors, needs). errors block the search; needs explain what else is required."""
    p = _clean(p)
    errors, needs = [], []
    if not any(p.get(k) for k in (*ID_FIELDS, *PARTY_FIELDS)):
        errors.append({"field": None, "code": "need_identifier",
                       "message": "Enter at least one identifier: CNR, case number, filing number, FIR number or a party name."})
    if p.get("cnr_number"):
        cnr = reference.parse_cnr(p["cnr_number"])
        if not cnr["valid"]:
            errors.append({"field": "cnr_number", "code": "invalid_cnr", "message": cnr["error"]})
        else:
            p["cnr_number"] = cnr["cnr"]
    if p.get("filing_year"):
        if not re.fullmatch(r"(19[5-9]\d|20\d\d)", p["filing_year"]):
            errors.append({"field": "filing_year", "code": "invalid_year", "message": "Year must be a 4-digit year."})
    if p.get("state") and p["state"] not in reference.STATES:
        errors.append({"field": "state", "code": "invalid_state", "message": "Choose a state from the list."})
    if p.get("fir_number"):
        missing = [f for f in ("state", "district", "police_station", "filing_year") if not p.get(f)]
        if missing:
            needs.append({"for": "fir_number", "fields": missing,
                          "message": "FIR numbers restart every year at every police station, so an FIR is identified only "
                                     "by state + district + police station + year + FIR number."})
    if p.get("case_number") and not p.get("cnr_number"):
        missing = [f for f in ("court", "case_type", "filing_year") if not p.get(f)]
        if missing:
            needs.append({"for": "case_number", "fields": missing,
                          "message": "A case number is unique only within a court, case type and year."})
    if (p.get("petitioner") or p.get("respondent")) and not p.get("court") and not p.get("state"):
        needs.append({"for": "party_name", "fields": ["court", "state", "filing_year"],
                      "message": "Party-name searches on official portals are run per court / establishment and year."})
    return p, errors, needs


def jurisdiction(p: dict) -> dict:
    j: dict = {"state": p.get("state"), "district": p.get("district"), "court": p.get("court"), "inferred": []}
    if p.get("cnr_number"):
        cnr = reference.parse_cnr(p["cnr_number"])
        if cnr["valid"]:
            j["cnr"] = cnr
            if cnr["state"] and not j["state"]:
                j["state"] = cnr["state"]
                j["inferred"].append(f"State inferred from CNR prefix {cnr['state_code']} — confirm on eCourts.")
            if not p.get("filing_year"):
                j["inferred"].append(f"Year {cnr['year']} read from the CNR.")
    level = next((lvl for name, lvl in reference.COURTS if p.get("court") and name.lower() == p["court"].lower()), None)
    j["level"] = level
    return j


def official_sources(p: dict, j: dict) -> list[dict]:
    want: list[tuple[str, str]] = []
    if p.get("fir_number"):
        if (j.get("state") or "") == "Maharashtra":
            want.append(("mh_police_fir", "fir_number"))
        want.append(("ecourts", "fir_number"))
    if p.get("cnr_number"):
        want.append(("ecourts", "cnr"))
    court_src = reference.source_for_court(p.get("court", "")) if p.get("court") else None
    if p.get("case_number"):
        want.append((court_src or "ecourts", "case_number"))
    if p.get("filing_number"):
        want.append(("sci" if court_src == "sci" else "ecourts", "diary_number" if court_src == "sci" else "filing_number"))
    if p.get("petitioner") or p.get("respondent"):
        want.append((court_src if court_src in ("bombay_hc", "ecourts") else "ecourts", "party_name"))
    out, seen = [], set()
    for sid, mode in want:
        if (sid, mode) in seen:
            continue
        seen.add((sid, mode))
        s = reference.SOURCES[sid]
        fields = s.get("search_modes", {}).get(mode, [])
        values = {f: (p.get(f) or (p.get("petitioner") or p.get("respondent") if f == "party_name" else None)
                      or (j.get("state") if f == "state" else None)) for f in fields}
        out.append({"source": sid, "name": s["name"], "access": s["access"], "url": s["url"], "home": s.get("home"),
                    "extra_links": s.get("extra_links", {}), "why_manual": s.get("why_manual"), "search_mode": mode,
                    "enter": values, "missing": [f for f, v in values.items() if not v], "checked": s.get("checked")})
    if p.get("fir_number") and (j.get("state") or "") not in ("", "Maharashtra"):
        out.append({"source": None, "name": f"{j['state']} Police citizen services", "access": "unknown", "url": None,
                    "why_manual": "No FIR search service has been verified for this state yet. Use the State Police's "
                                  "official citizen portal; do not rely on unofficial aggregators.",
                    "search_mode": "fir_number", "enter": {}, "missing": []})
    return out


def local_matches(p: dict, principal=None) -> tuple[list[dict], list[dict]]:
    """Matches among the caller's own cases and their own imported records only."""
    from . import authz

    mine = authz.accessible_case_ids(principal) if principal is not None else []
    user_id = principal.user_id if principal is not None else None
    conds, args = [], []
    for f in ("cnr_number", "case_number", "filing_number", "fir_number"):
        if p.get(f):
            conds.append(f"REPLACE(UPPER({f}),' ','')=REPLACE(UPPER(?),' ','')")
            args.append(p[f])
    for f in PARTY_FIELDS:
        if p.get(f):
            conds.append("(client_name LIKE ? OR opposite_party LIKE ? OR title LIKE ?)")
            args += [f"%{p[f]}%"] * 3
    found = []
    if conds and mine:
        rows = db.conn().execute(f"SELECT * FROM cases WHERE id IN ({','.join('?' * len(mine))}) AND "
                                 f"({' OR '.join(conds)}) ORDER BY updated_at DESC LIMIT 20", [*mine, *args])
        for r in rows:
            c = dict(r)
            score = sum(1 for f in ID_FIELDS if p.get(f) and (c.get(f) or "").replace(" ", "").upper() == p[f].replace(" ", "").upper())
            if p.get("filing_year") and str(c.get("filing_year") or "") not in ("", p["filing_year"]):
                continue  # identifiers repeat across years: a different year is a different matter
            if p.get("police_station") and c.get("police_station") and p["police_station"].lower() not in c["police_station"].lower():
                continue
            c["match"] = "identifier" if score else "party name"
            found.append(c)
    recs = []
    terms = [p[f] for f in (*ID_FIELDS, *PARTY_FIELDS) if p.get(f)]
    if terms and user_id:
        team = authz.team_case_ids(principal)
        rows = db.conn().execute(
            "SELECT * FROM source_records WHERE kind='case_record' AND (created_by = ?" +
            (f" OR case_id IN ({','.join('?' * len(team))})" if team else "") + ") ORDER BY retrieved_at DESC LIMIT 500",
            [user_id, *team])
        for r in rows:
            rec = db.row_to_dict(r, ("data",))
            blob = (rec["external_id"] + " " + json.dumps(rec["data"], ensure_ascii=False)).upper().replace(" ", "")
            if any(t.upper().replace(" ", "") in blob for t in terms):
                recs.append(rec)
    return found, recs[:20]


def search(params: dict, principal=None) -> dict:
    p, errors, needs = validate(params)
    if errors:
        return {"ok": False, "query": p, "errors": errors, "needs": needs}
    j = jurisdiction(p)
    cases_found, records = local_matches(p, principal)
    return {"ok": True, "query": p, "errors": [], "needs": needs, "jurisdiction": j, "local_cases": cases_found,
            "imported_records": records, "official_sources": official_sources(p, j),
            "automated_retrieval": False,
            "notice": "No official court or police source used here offers an API; their search pages require a CAPTCHA. "
                      "NyaySutra will not bypass that. Open the official source, then import what you find."}


# ── Manual import + change detection ────────────────────────────────────────
def external_id(rec: dict) -> str:
    if rec.get("cnr_number"):
        return rec["cnr_number"].upper()
    if rec.get("fir_number"):
        return "FIR|" + "|".join((rec.get(k) or "").upper() for k in ("state", "district", "police_station", "fir_number", "filing_year"))
    return "CASE|" + "|".join((rec.get(k) or "").upper() for k in ("court", "case_type", "case_number", "filing_number", "filing_year"))


def import_record(source: str, rec: dict, case_id: str | None = None) -> dict:
    if source not in reference.SOURCES or reference.SOURCES[source]["kind"] not in ("case_records", "fir"):
        raise ValueError("Choose the official source the details were copied from.")
    rec = _clean({k: v for k, v in rec.items() if not isinstance(v, (list, dict))}) | \
        {k: v for k, v in rec.items() if isinstance(v, list)}
    p, errors, _ = validate(rec)
    if errors and not (len(errors) == 1 and errors[0]["code"] == "need_identifier" and rec.get("title")):
        raise ValueError(errors[0]["message"])
    orders = [o for o in rec.get("orders") or [] if isinstance(o, dict) and (o.get("date") or o.get("title"))][:50]
    data = {**{k: v for k, v in p.items()}, "orders": orders}
    data["unverified_fields"] = sorted(k for k in data if k not in ("orders", "unverified_fields"))
    title = rec.get("title") or " v. ".join(x for x in (p.get("petitioner"), p.get("respondent")) if x) or external_id(p)
    ext = external_id(p)
    ident = db.current()
    owner = ident.user_id
    prev = db.conn().execute("SELECT id, data, case_id FROM source_records WHERE source=? AND external_id=? "
                             "AND COALESCE(created_by, '') = ?", (source, ext, owner or "")).fetchone()
    changes = []
    if prev:
        old = json.loads(prev["data"] or "{}")
        for k in TRACKED:
            if json.dumps(old.get(k), sort_keys=True) != json.dumps(data.get(k), sort_keys=True) and (old.get(k) or data.get(k)):
                changes.append({"field": k, "old": old.get(k), "new": data.get(k)})
    res = integrations.upsert_record(source, ext, kind="case_record", title=title[:300], url=rec.get("source_url") or
                                     reference.SOURCES[source]["url"], published_at=None, data=data,
                                     provenance="user_import", jurisdiction=p.get("state") or "", case_id=case_id,
                                     verified_at=db.now(), owner=owner, org_id=ident.org_id or db.DEFAULT_ORG)
    row = db.row_to_dict(db.conn().execute("SELECT * FROM source_records WHERE source=? AND external_id=? "
                                           "AND COALESCE(created_by, '') = ?", (source, ext, owner or "")).fetchone(),
                         ("data",))
    linked = case_id or row.get("case_id")
    if linked and not case_id and not db.conn().execute(   # a link made earlier counts only while still on the case team
            "SELECT 1 FROM case_members WHERE case_id=? AND user_id=? AND access IN ('owner','advocate')",
            (linked, owner or "")).fetchone():
        linked = None
    if changes:
        desc = "; ".join(f"{c['field'].replace('_', ' ')}: {_short(c['old'])} → {_short(c['new'])}" for c in changes[:4])
        db.notify("case_update", f"Case record changed: {title[:120]}", desc,
                  f"#/cases/{linked}" if linked else "#/lookup", linked,
                  dedupe_key=f"rec:{row['id']}:{row['content_hash'][:16]}",
                  recipients=None if linked else [owner] if owner else [])
        if linked:
            apply_to_case(linked, data, changes)
    db.audit("record.import", "source_record", row["id"], source=source, result=res, changes=len(changes))
    return {"record": row, "result": res, "changes": changes}


def _short(v) -> str:
    if isinstance(v, list):
        return f"{len(v)} item(s)"
    return str(v or "—")[:60]


def apply_to_case(case_id: str, data: dict, changes: list[dict]) -> None:
    upd = {}
    for c in changes:
        if c["field"] == "next_hearing_date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(c["new"] or "")):
            upd["next_hearing_date"] = c["new"]
    if upd:
        try:
            cases.update_case(case_id, upd)
        except cases.FieldError:
            pass
    for c in changes:
        if c["field"] in ("status", "orders", "stage"):
            cases.add_note(case_id, f"Imported from official portal: {c['field']} changed — {_short(c['old'])} → {_short(c['new'])}",
                           author="Sync")


def save_to_case(record_id: str) -> dict:
    rec = db.row_to_dict(db.conn().execute("SELECT * FROM source_records WHERE id=?", (record_id,)).fetchone(), ("data",))
    if not rec or rec["kind"] != "case_record":
        raise ValueError("Record not found")
    if rec.get("case_id") and cases.get_case(rec["case_id"]):
        return cases.get_case(rec["case_id"])
    d = rec["data"]
    cat = d.get("category") if d.get("category") in reference.CATEGORIES else ("Criminal" if d.get("fir_number") else "Civil")
    year = d.get("filing_year") or (reference.parse_cnr(d["cnr_number"]).get("year") if d.get("cnr_number") else None)
    case = cases.create_case({
        "title": rec["title"], "case_type": cat, "case_subtype": d.get("case_type", ""), "case_number": d.get("case_number", ""),
        "filing_number": d.get("filing_number", ""), "fir_number": d.get("fir_number", ""), "cnr_number": d.get("cnr_number", ""),
        "court": d.get("court", ""), "state": d.get("state", ""), "district": d.get("district", "") if d.get("state") != "Maharashtra"
        or d.get("district") in reference.DISTRICTS["Maharashtra"] else "", "police_station": d.get("police_station", ""),
        "filing_year": year, "client_name": d.get("petitioner", ""), "opposite_party": d.get("respondent", ""),
        "next_hearing_date": d.get("next_hearing_date") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.get("next_hearing_date") or "") else None,
        "acts_sections": d.get("sections", ""), "is_draft": True,
        "description": f"Imported from {reference.SOURCES[rec['source']]['name']} on {rec['retrieved_at'][:10]} "
                       f"(user-entered; verify against the official record).",
    })
    with db.tx() as c:
        c.execute("UPDATE source_records SET case_id=? WHERE id=?", (case["id"], record_id))
    if d.get("orders"):
        cases.add_note(case["id"], "Orders noted from the official portal: " + "; ".join(
            f"{o.get('date', '')} {o.get('title', '')}".strip() for o in d["orders"][:10]), author="Import")
    db.audit("record.save_case", "source_record", record_id, case_id=case["id"])
    return cases.get_case(case["id"])


def records_for_case(case_id: str) -> list[dict]:
    return [db.row_to_dict(r, ("data",)) for r in db.conn().execute(
        "SELECT * FROM source_records WHERE case_id=? ORDER BY retrieved_at DESC", (case_id,))]
