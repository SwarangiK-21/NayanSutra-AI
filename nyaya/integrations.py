"""External source integrations: one adapter per source, a scheduler for periodic synchronisation,
change detection, retries with back-off, per-host rate limits, sync logs and notifications.

Access modes (shown to the user exactly as they are — never a fabricated "connected" status):
  * periodic   — the source publishes a machine-readable feed/API and is polled on a schedule
                 (LiveLaw RSS, India Code DSpace API). Conditional GET is used where supported.
  * manual     — human-only portals (CAPTCHA / login, no API): eCourts, Supreme Court, Bombay HC,
                 police FIR portals, LatestLaws. The app offers "Open official source" and lets the
                 user import what they lawfully downloaded; re-imports are change-detected.
No source available to this app offers webhooks/push to a local machine, so there is no live mode.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as SafeET

from . import config, db, reference

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
HOST_MIN_INTERVAL = 3.0           # seconds between two requests to the same host (politeness)
MANUAL_REFRESH_COOLDOWN = 120.0   # a user can force a sync at most this often per source
RETRY_BASE = timedelta(minutes=5)

# For tests: replace with a factory returning an httpx.Client using a MockTransport.
client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(
    timeout=httpx.Timeout(30, connect=15), follow_redirects=True,
    headers={"User-Agent": config.HTTP_USER_AGENT})

_host_last: dict[str, float] = {}
_host_lock = threading.Lock()
_run_lock = threading.Lock()


class SyncError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _respect_rate(url: str) -> None:
    host = urlparse(url).hostname or ""
    with _host_lock:
        wait = HOST_MIN_INTERVAL - (time.monotonic() - _host_last.get(host, -1e9))
        _host_last[host] = time.monotonic() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


def _get(client: httpx.Client, url: str, headers: dict | None = None, attempts: int = 3) -> httpx.Response:
    """GET with retries on network errors / 429 / 5xx (exponential back-off)."""
    last = None
    for i in range(attempts):
        _respect_rate(url)
        try:
            r = client.get(url, headers=headers or {})
            if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                last = SyncError(f"HTTP {r.status_code}")
                time.sleep(min(2 ** i * 2, 20))
                continue
            return r
        except httpx.HTTPError as exc:
            last = SyncError(f"{type(exc).__name__}")
            if i < attempts - 1:
                time.sleep(min(2 ** i * 2, 20))
    raise last or SyncError("request failed")


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def upsert_record(source: str, external_id: str, *, kind: str, title: str, url: str = "", published_at: str | None = None,
                  data: dict | None = None, provenance: str, jurisdiction: str = "", case_id: str | None = None,
                  verified_at: str | None = None, owner: str | None = None, org_id: str | None = None) -> str:
    """Insert or update a source record. Returns 'new', 'updated' or 'unchanged' (duplicate prevention by
    (source, external_id, owner) plus a content hash). Public feed items have no owner; a court record imported by
    a user belongs to that user (another user's import of the same record is a separate row)."""
    h = _hash(title, url, published_at, data)
    now = db.now()
    with db.tx() as c:
        row = c.execute("SELECT id, content_hash FROM source_records WHERE source=? AND external_id=? "
                        "AND COALESCE(created_by, '') = ?", (source, external_id, owner or "")).fetchone()
        if row and row["content_hash"] == h:
            c.execute("UPDATE source_records SET retrieved_at=?, verified_at=COALESCE(?, verified_at), "
                      "case_id=COALESCE(?, case_id) WHERE id=?", (now, verified_at, case_id, row["id"]))
            return "unchanged"
        if row:
            c.execute("UPDATE source_records SET title=?, url=?, published_at=?, retrieved_at=?, verified_at=?, content_hash=?, "
                      "data=?, case_id=COALESCE(?, case_id), jurisdiction=? WHERE id=?",
                      (title[:500], url[:1000], published_at, now, verified_at, h, json.dumps(data or {}, ensure_ascii=False),
                       case_id, jurisdiction, row["id"]))
            return "updated"
        c.execute("INSERT INTO source_records(id, source, external_id, kind, title, url, jurisdiction, published_at, retrieved_at, "
                  "verified_at, content_hash, provenance, data, case_id, created_by, org_id) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (db.new_id("rec_"), source, external_id[:500], kind, title[:500], url[:1000], jurisdiction, published_at, now,
                   verified_at, h, provenance, json.dumps(data or {}, ensure_ascii=False), case_id, owner, org_id))
        return "new"


# ── Adapters ─────────────────────────────────────────────────────────────────
class Adapter:
    id = ""
    mode = "manual"                       # periodic | manual
    interval = timedelta(hours=24)

    @property
    def meta(self) -> dict:
        return reference.SOURCES[self.id]

    def sync(self, state: dict) -> dict:  # -> {"new", "updated", "not_modified", "etag", "last_modified"}
        raise SyncError("This source has no automated access; use the official portal.")


class LiveLawFeed(Adapter):
    id, mode, interval = "livelaw", "periodic", timedelta(minutes=60)

    def sync(self, state: dict) -> dict:
        headers = {}
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
        with client_factory() as client:
            r = _get(client, self.meta["url"], headers)
        if r.status_code == 304:
            return {"not_modified": True, "new": 0, "updated": 0}
        if r.status_code != 200:
            raise SyncError(f"Feed returned HTTP {r.status_code}")
        if len(r.content) > MAX_FEED_BYTES:
            raise SyncError("Feed too large")
        try:
            root = SafeET.fromstring(r.content)
        except Exception as exc:  # defusedxml raises several types for hostile/invalid XML
            raise SyncError("Feed is not valid RSS") from exc
        new = updated = 0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link.startswith("https://www.livelaw.in/"):
                continue  # only links back to the publisher are kept
            pub = (item.findtext("pubDate") or "").strip()
            try:
                pub_iso = _iso(parsedate_to_datetime(pub)) if pub else None
            except (TypeError, ValueError):
                pub_iso = None
            guid = (item.findtext("guid") or link).strip()
            cats = [c.text.strip() for c in item.findall("category") if c.text]
            res = upsert_record(self.id, guid, kind="news", title=title, url=link, published_at=pub_iso,
                                data={"categories": cats[:6]}, provenance="feed", jurisdiction="India",
                                verified_at=db.now())
            new += res == "new"
            updated += res == "updated"
        return {"new": new, "updated": updated, "etag": r.headers.get("etag"),
                "last_modified": r.headers.get("last-modified")}


class IndiaCodeUpdates(Adapter):
    """Detects when India Code changes the record of an act in the local library (new version / withdrawal)."""
    id, mode, interval = "indiacode", "periodic", timedelta(days=1)
    API = "https://indiacode.gov.in/server/api/core/items/"

    def sync(self, state: dict) -> dict:
        manifest = json.loads((config.BARE_ACTS_DIR / "manifest.json").read_text("utf-8"))
        new = updated = 0
        changed = []
        with client_factory() as client:
            for fname, m in manifest.items():
                uuid = (m.get("item_url") or "").rsplit("/", 1)[-1]
                if not uuid:
                    continue
                r = _get(client, self.API + uuid)
                if r.status_code != 200:
                    continue
                d = r.json()
                data = {"lastModified": d.get("lastModified"), "withdrawn": d.get("withdrawn"), "file": fname}
                res = upsert_record(self.id, uuid, kind="act_version", title=m["title"], url=m["item_url"],
                                    published_at=d.get("lastModified"), data=data, provenance="api",
                                    jurisdiction="Maharashtra" if "Maharashtra" in m["title"] else "Central",
                                    verified_at=db.now())
                new += res == "new"
                if res == "updated":
                    updated += 1
                    changed.append(m["title"])
        for title in changed:
            db.notify("source_update", f"India Code updated the record of {title}",
                      "The official record changed since it was last checked. Re-run scripts/fetch_bare_acts.py and "
                      "'python -m nyaya.ingest library' to download and index the current text.", "#/updates?tab=sources",
                      dedupe_key=f"indiacode:{title}:{datetime.now():%Y-%m-%d}")
        return {"new": new, "updated": updated}


class Manual(Adapter):
    def __init__(self, sid: str):
        self.id = sid


ADAPTERS: dict[str, Adapter] = {a.id: a for a in (LiveLawFeed(), IndiaCodeUpdates())}
for _sid, _m in reference.SOURCES.items():
    ADAPTERS.setdefault(_sid, Manual(_sid))


# ── State, scheduler, sync ───────────────────────────────────────────────────
def _state(iid: str) -> dict:
    row = db.conn().execute("SELECT * FROM integrations WHERE id=?", (iid,)).fetchone()
    if row:
        return dict(row)
    a = ADAPTERS[iid]
    with db.tx() as c:
        c.execute("INSERT INTO integrations(id, enabled, status, next_due_at) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                  (iid, 1, "manual" if a.mode == "manual" else "never", _iso(_utcnow())))
    return dict(db.conn().execute("SELECT * FROM integrations WHERE id=?", (iid,)).fetchone())


def run_sync(iid: str, trigger: str = "scheduled") -> dict:
    a = ADAPTERS.get(iid)
    if not a:
        raise ValueError("Unknown integration")
    st = _state(iid)
    if a.mode == "manual":
        raise ValueError("This source has no automated access. Open the official portal and import the record manually.")
    if trigger == "manual" and st.get("last_attempt_at"):
        since = (_utcnow() - datetime.fromisoformat(st["last_attempt_at"])).total_seconds()
        if since < MANUAL_REFRESH_COOLDOWN:
            raise ValueError(f"Synced {int(since)} s ago — please wait {int(MANUAL_REFRESH_COOLDOWN - since)} s.")
    with _run_lock:
        started = _iso(_utcnow())
        with db.tx() as c:
            log_id = c.execute("INSERT INTO sync_log(integration_id, trigger, started_at) VALUES(?,?,?) RETURNING id",
                               (iid, trigger, started)).fetchone()[0]
            c.execute("UPDATE integrations SET last_attempt_at=? WHERE id=?", (started, iid))
        try:
            res = a.sync(st)
        except Exception as exc:  # network errors, bad payloads: logged, retried with back-off
            msg = str(exc) if isinstance(exc, SyncError) else f"{type(exc).__name__}"
            fails = (st.get("fail_count") or 0) + 1
            retry = min(RETRY_BASE * (2 ** (fails - 1)), a.interval)
            with db.tx() as c:
                c.execute("UPDATE integrations SET status='error', last_error=?, fail_count=?, next_due_at=? WHERE id=?",
                          (msg[:300], fails, _iso(_utcnow() + retry), iid))
                c.execute("UPDATE sync_log SET finished_at=?, outcome='error', error=? WHERE id=?",
                          (_iso(_utcnow()), msg[:300], log_id))
            if fails == 3:
                db.notify("system", f"{a.meta['name']}: synchronisation failing", f"Last error: {msg}",
                          "#/updates?tab=sources", dedupe_key=f"syncfail:{iid}:{datetime.now():%Y-%m-%d}")
            log.warning("sync %s failed (%d): %s", iid, fails, msg)
            return {"ok": False, "error": msg, "retry_in_minutes": int(retry.total_seconds() // 60)}
        outcome = "not_modified" if res.get("not_modified") else "ok"
        with db.tx() as c:
            c.execute("UPDATE integrations SET status='ok', last_success_at=?, last_error=NULL, fail_count=0, next_due_at=?, "
                      "etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified) WHERE id=?",
                      (_iso(_utcnow()), _iso(_utcnow() + a.interval), res.get("etag"), res.get("last_modified"), iid))
            c.execute("UPDATE sync_log SET finished_at=?, outcome=?, items_new=?, items_updated=? WHERE id=?",
                      (_iso(_utcnow()), outcome, res.get("new", 0), res.get("updated", 0), log_id))
        if iid == "livelaw" and res.get("new"):
            db.notify("source_update", f"LiveLaw: {res['new']} new headline(s)", "", "#/updates?tab=news",
                      dedupe_key=f"livelaw:{log_id}")
        db.audit("integration.sync", "integration", iid, trigger=trigger, outcome=outcome, new=res.get("new", 0))
        return {"ok": True, "outcome": outcome, **{k: res.get(k, 0) for k in ("new", "updated")}}


def due() -> list[str]:
    now = _iso(_utcnow())
    out = []
    for iid, a in ADAPTERS.items():
        if a.mode != "periodic":
            continue
        st = _state(iid)
        if st["enabled"] and (not st.get("next_due_at") or st["next_due_at"] <= now):
            out.append(iid)
    return out


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sync-scheduler")
        self.stop = threading.Event()

    def run(self):
        with db.system():   # scheduled sync of public sources is internal work
            self._loop()

    def _loop(self):
        self.stop.wait(15)  # let the app start first
        while not self.stop.is_set():
            for iid in due():
                if self.stop.is_set():
                    break
                try:
                    run_sync(iid, "scheduled")
                except Exception:
                    log.exception("scheduler: %s", iid)
            self.stop.wait(config.SYNC_TICK_SECONDS)


def status() -> list[dict]:
    """Health of each source (shared configuration; read by signed-in users, changed by administrators only)."""
    with db.system():
        return _status()


def _status() -> list[dict]:
    out = []
    for iid, a in ADAPTERS.items():
        st = _state(iid)
        m = reference.SOURCES[iid]
        n = db.conn().execute("SELECT COUNT(*) FROM source_records WHERE source=?", (iid,)).fetchone()[0]
        logs = [dict(r) for r in db.conn().execute(
            "SELECT trigger, started_at, finished_at, outcome, items_new, items_updated, error FROM sync_log "
            "WHERE integration_id=? ORDER BY id DESC LIMIT 5", (iid,))]
        out.append({"id": iid, "name": m["name"], "kind": m["kind"], "access": m["access"], "mode": a.mode,
                    "url": m["url"], "home": m.get("home"), "extra_links": m.get("extra_links", {}),
                    "why_manual": m.get("why_manual"), "note": m.get("note"), "checked": m.get("checked"),
                    "interval_minutes": int(a.interval.total_seconds() // 60) if a.mode == "periodic" else None,
                    "enabled": bool(st["enabled"]), "status": st["status"], "last_attempt_at": st.get("last_attempt_at"),
                    "last_success_at": st.get("last_success_at"), "last_error": st.get("last_error"),
                    "next_due_at": st.get("next_due_at") if a.mode == "periodic" else None,
                    "records": n, "log": logs})
    return out


def set_enabled(iid: str, enabled: bool) -> None:
    _state(iid)
    with db.tx() as c:
        c.execute("UPDATE integrations SET enabled=? WHERE id=?", (1 if enabled else 0, iid))
    db.audit("integration.toggle", "integration", iid, enabled=enabled)


def news(limit: int = 50, q: str = "") -> list[dict]:
    sql = "SELECT * FROM source_records WHERE kind='news'"
    args: list = []
    if q:
        sql += " AND title LIKE ?"
        args.append(f"%{q}%")
    sql += " ORDER BY published_at DESC LIMIT ?"
    return [db.row_to_dict(r, ("data",)) for r in db.conn().execute(sql, [*args, limit])]
