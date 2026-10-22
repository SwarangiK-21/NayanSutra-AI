"""FastAPI server: JSON API + Server-Sent Events chat stream + static frontend."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (agent, awareness, cases, casesearch, citations, config, db, docintel, drafting, embeddings, export,
               ingest, integrations, jobs, llm, ocr, provisions, reference, retrieval, search, security, tools)
from .vectorindex import index

log = logging.getLogger("nyaya")

_ingest_pool: ThreadPoolExecutor | None = None  # created per app lifespan
_state = {"models_ready": False, "llm": None, "llm_checked": 0.0}
_scheduler: integrations.Scheduler | None = None


def _warm():
    index.load()          # vectors from SQLite → RAM (background, so the UI is up immediately)
    embeddings.warm_up()  # load ONNX models
    _state["models_ready"] = True


def _apply_retention() -> None:
    """Data minimisation: delete conversations older than the configured retention period (if one is set)."""
    days = db.get_pref("chat_retention_days")
    if days and days.isdigit():
        cutoff = (date.today() - timedelta(days=int(days))).isoformat()
        with db.tx() as c:
            n = c.execute("DELETE FROM chats WHERE updated_at < ?", (cutoff,)).rowcount
        if n:
            db.audit("retention.purge_chats", "chat", None, deleted=n, older_than_days=int(days))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ingest_pool
    _ingest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
    db.init()
    cases.seed_if_empty()
    _apply_retention()
    stored = db.get_meta("embed_model")
    if stored and stored != embeddings.model_name():
        log.warning("Index was built with %s but EMBED_MODEL is %s — run: python -m nyaya.ingest reembed",
                    stored, embeddings.model_name())
    # Re-queue uploads interrupted by a restart.
    for r in db.conn().execute("SELECT id FROM documents WHERE status='processing'").fetchall():
        _ingest_pool.submit(ingest.process, r["id"])
    threading.Thread(target=_warm, daemon=True).start()
    global _scheduler
    if config.SYNC_ENABLED:
        _scheduler = integrations.Scheduler()
        _scheduler.start()
    yield
    if _scheduler:
        _scheduler.stop.set()
    _ingest_pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="NyaySutra AI", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# ── Security middleware ──────────────────────────────────────────────────────
@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.headers.get("host")
    if not security.host_allowed(host):
        return JSONResponse({"detail": "Host not allowed"}, status_code=400)
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/login", "/api/session"):
        token = request.cookies.get(security.COOKIE)
        if not security.valid_token(token):
            return JSONResponse({"detail": "Session expired — reload the page"}, status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not security.origin_allowed(request.headers.get("origin"), host):
                return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
            if request.headers.get(security.CSRF_HEADER) != security.csrf_for(token):
                return JSONResponse({"detail": "Missing or invalid CSRF token"}, status_code=403)
    response: Response = await call_next(request)
    if "content-security-policy" not in response.headers:  # endpoints may set a stricter / specific policy
        response.headers["Content-Security-Policy"] = security.CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    # Microphone only for this origin (voice input in Legal Research); the browser still asks the user.
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/"):
        # Revalidate (cheap 304 via ETag) so an updated app.js / app.css is picked up on reload.
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Internal error"}, status_code=500)


def client_key(request: Request) -> str:
    return request.client.host if request.client else "local"


def rate(limit: int, bucket: str):
    def dep(request: Request):
        if not security.limiter.allow(f"{bucket}:{client_key(request)}", limit):
            raise HTTPException(429, "Too many requests — please slow down.")
    return dep


def _set_session(resp: Response, token: str | None = None) -> str:
    token = token or security.issue_token()
    resp.set_cookie(security.COOKIE, token, max_age=security.SESSION_TTL, httponly=True, samesite="strict",
                    path="/")
    return token


# ── Pages / session ──────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index_page(request: Request):
    resp = FileResponse(config.WEB_DIR / "index.html", media_type="text/html")
    resp.headers["Cache-Control"] = "no-store"
    if not config.APP_PASSWORD and not security.valid_token(request.cookies.get(security.COOKIE)):
        _set_session(resp)
    return resp


@app.get("/api/session")
def session(request: Request):
    token = request.cookies.get(security.COOKIE)
    if security.valid_token(token):
        return {"authenticated": True, "csrf": security.csrf_for(token), "password_required": bool(config.APP_PASSWORD)}
    if not config.APP_PASSWORD:
        token = security.issue_token()
        resp = JSONResponse({"authenticated": True, "csrf": security.csrf_for(token), "password_required": False})
        _set_session(resp, token)
        return resp
    return {"authenticated": False, "password_required": True}


class Login(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/login", dependencies=[Depends(rate(5, "login"))])
def login(body: Login):
    if not security.check_password(body.password):
        time.sleep(0.5)
        raise HTTPException(401, "Incorrect password")
    token = security.issue_token()
    resp = JSONResponse({"ok": True, "csrf": security.csrf_for(token)})
    _set_session(resp, token)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(security.COOKIE, path="/")
    return resp


# ── Status / dashboard ───────────────────────────────────────────────────────
@app.get("/api/status")
def status():
    if time.time() - _state["llm_checked"] > 120:
        ok, _ = llm.ping()
        _state.update(llm=ok, llm_checked=time.time())
    counts = {r["collection"]: r["n"] for r in db.conn().execute(
        "SELECT collection, COUNT(*) n FROM documents WHERE status='ready' GROUP BY collection")}
    return {
        "llm_model": config.LLM_MODEL, "fast_model": config.FAST_LLM_MODEL, "embed_model": config.EMBED_MODEL,
        "reranker": config.RERANK_MODEL if config.RERANK_ENABLED else None, "llm_reachable": _state["llm"],
        "llm_host": "Ollama Cloud" if "ollama.com" in config.OLLAMA_HOST else "Local Ollama",
        "models_ready": _state["models_ready"], "indexed_chunks": len(index), "documents": counts,
        "processing": db.conn().execute("SELECT COUNT(*) FROM documents WHERE status='processing'").fetchone()[0],
        "ocr": ocr.status(), "sync_enabled": config.SYNC_ENABLED,
    }


@app.get("/api/dashboard")
def dashboard():
    today = date.today()
    upcoming = [dict(r) for r in db.conn().execute(
        "SELECT id, title, case_type, court, next_hearing_date, status FROM cases "
        "WHERE next_hearing_date >= ? AND next_hearing_date <= ? ORDER BY next_hearing_date LIMIT 8",
        (today.isoformat(), (today + timedelta(days=45)).isoformat()))]
    by_type = {r["case_type"]: r["n"] for r in db.conn().execute("SELECT case_type, COUNT(*) n FROM cases GROUP BY case_type")}
    recent = [dict(r) for r in db.conn().execute(
        "SELECT ch.id, ch.title, ch.case_id, ch.updated_at, c.title AS case_title FROM chats ch "
        "LEFT JOIN cases c ON c.id = ch.case_id ORDER BY ch.updated_at DESC LIMIT 6")]
    lib = {r["collection"]: r["n"] for r in db.conn().execute(
        "SELECT collection, COUNT(*) n FROM documents WHERE status='ready' GROUP BY collection")}
    return {"upcoming": upcoming, "by_type": by_type, "recent_chats": recent, "library": lib,
            "total_cases": sum(by_type.values())}


# ── Preferences, notifications, activity ────────────────────────────────────
LANGS = ("en", "hi", "mr")
PREF_KEYS = {"lang": LANGS, "voice_lang": ("en-IN", "hi-IN", "mr-IN"), "tts": ("on", "off"),
             "voice_consent": ("granted", "denied", ""), "draft_style": None, "chat_retention_days": None}


class PrefsIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


@app.get("/api/prefs")
def get_prefs():
    rows = db.conn().execute("SELECT key, value FROM prefs").fetchall()
    out = {"lang": "en"}
    out.update({r["key"]: r["value"] for r in rows if r["key"] in PREF_KEYS})
    return out


@app.put("/api/prefs")
def put_prefs(body: PrefsIn):
    for k, v in body.values.items():
        allowed = PREF_KEYS.get(k, ())
        v = str(v)[:200]
        if k not in PREF_KEYS or (allowed is not None and v not in allowed):
            raise HTTPException(400, f"Invalid preference: {k}")
        if k == "chat_retention_days" and v and not (v.isdigit() and 1 <= int(v) <= 3650):
            raise HTTPException(400, "Retention must be 1–3650 days, or empty to keep forever.")
        db.set_pref(k, v)
    return get_prefs()


@app.get("/api/notifications")
def notifications(unread: bool = False, limit: int = 50):
    sql = "SELECT * FROM notifications" + (" WHERE read_at IS NULL" if unread else "") + \
          " ORDER BY created_at DESC, id DESC LIMIT ?"
    items = [dict(r) for r in db.conn().execute(sql, (max(1, min(limit, 200)),))]
    n_unread = db.conn().execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]
    return {"items": items, "unread": n_unread}


@app.post("/api/notifications/read")
def notifications_read():
    with db.tx() as c:
        c.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (db.now(),))
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = 100):
    rows = db.conn().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
    return {"items": [db.row_to_dict(r, ("detail",)) for r in rows]}


# ── Cases ────────────────────────────────────────────────────────────────────
class CaseIn(BaseModel):
    title: str | None = Field(None, max_length=200)
    case_type: str | None = Field(None, max_length=20)
    case_subtype: str | None = Field(None, max_length=120)
    case_number: str | None = Field(None, max_length=100)
    filing_number: str | None = Field(None, max_length=100)
    fir_number: str | None = Field(None, max_length=60)
    cnr_number: str | None = Field(None, max_length=30)
    court: str | None = Field(None, max_length=200)
    state: str | None = Field(None, max_length=80)
    district: str | None = Field(None, max_length=80)
    police_station: str | None = Field(None, max_length=120)
    filing_year: str | int | None = None
    client_name: str | None = Field(None, max_length=200)
    client_role: str | None = Field(None, max_length=100)
    opposite_party: str | None = Field(None, max_length=200)
    opposite_role: str | None = Field(None, max_length=100)
    filing_date: str | None = Field(None, max_length=10)
    next_hearing_date: str | None = Field(None, max_length=10)
    status: str | None = Field(None, max_length=40)
    acts_sections: str | None = Field(None, max_length=600)
    description: str | None = Field(None, max_length=6000)
    is_draft: bool | None = None


class NoteIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field("Advocate", max_length=80)


def _field_error(exc: cases.FieldError) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "field": exc.field, "code": exc.code}, status_code=400)


@app.get("/api/reference")
def reference_data():
    return reference.form_options()


@app.get("/api/cases")
def list_cases():
    return {"cases": cases.list_cases(), "types": cases.CASE_TYPES, "statuses": cases.CASE_STATUSES}


@app.post("/api/cases")
def create_case(body: CaseIn):
    try:
        return cases.create_case(body.model_dump(exclude_none=True))
    except cases.FieldError as exc:
        return _field_error(exc)


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    c = cases.get_case(case_id)
    if not c:
        raise HTTPException(404, "Case not found")
    return c


@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, body: CaseIn):
    try:
        c = cases.update_case(case_id, body.model_dump(exclude_none=True))
    except cases.FieldError as exc:
        return _field_error(exc)
    if not c:
        raise HTTPException(404, "Case not found")
    return c


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str):
    if not cases.delete_case(case_id):
        raise HTTPException(404, "Case not found")
    return {"ok": True}


@app.post("/api/cases/{case_id}/notes")
def add_note(case_id: str, body: NoteIn):
    c = cases.add_note(case_id, body.content, body.author)
    if not c:
        raise HTTPException(404, "Case not found")
    return c


# ── Documents ────────────────────────────────────────────────────────────────
DOC_FIELDS = "id, case_id, collection, doc_type, title, filename, pages, chars, n_chunks, status, error, meta, created_at"


@app.get("/api/documents")
def list_documents(case_id: str | None = None, collection: str | None = None):
    sql, args = f"SELECT {DOC_FIELDS} FROM documents WHERE 1=1", []
    if case_id:
        # Derived documents (English translations) are reached from their source document, not listed.
        sql += " AND case_id=? AND json_extract(meta,'$.derived_from') IS NULL"
        args.append(case_id)
    if collection:
        sql += " AND collection=?"
        args.append(collection)
    sql += " ORDER BY created_at DESC LIMIT 500"
    return {"documents": [db.row_to_dict(r) for r in db.conn().execute(sql, args)], "types": ingest.DOC_TYPES}


@app.post("/api/cases/{case_id}/documents", dependencies=[Depends(rate(30, "upload"))])
def upload(case_id: str, file: UploadFile = File(...), doc_type: str = Form("auto")):
    if not cases.get_case(case_id):
        raise HTTPException(404, "Case not found")
    name = security.safe_filename(file.filename or "document")
    ext = Path(name).suffix.lower()
    if ext not in security.ALLOWED_EXT:
        raise HTTPException(415, "Accepted: PDF, DOC/DOCX, TXT, MD and images (PNG, JPG, TIFF, WEBP).")
    if doc_type not in ingest.DOC_TYPES and doc_type != "auto":
        doc_type = "auto"
    folder = config.UPLOAD_DIR / case_id.replace("/", "_")
    folder.mkdir(parents=True, exist_ok=True)
    doc_id = db.new_id("doc_")
    dest = folder / f"{doc_id}{ext}"
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with open(dest, "wb") as out:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File is larger than {config.MAX_UPLOAD_MB} MB.")
                out.write(chunk)
        if not security.sniff_ok(dest, ext):
            raise HTTPException(415, "File content does not match its extension.")
        doc, created = ingest.register(dest, collection="case", case_id=case_id,
                                       doc_type=None if doc_type == "auto" else doc_type,
                                       title=None, filename=name, doc_id=doc_id)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if not created:
        dest.unlink(missing_ok=True)
        if doc["status"] == "failed":  # retry a previously failed file
            _ingest_pool.submit(ingest.process, doc["id"])
        return {**_public(doc), "duplicate": True}
    _ingest_pool.submit(ingest.process, doc["id"])
    db.audit("document.upload", "document", doc["id"], case_id=case_id, ext=ext, bytes=size)
    return _public(doc)


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("path", "sha256")}


@app.get("/api/documents/{doc_id}")
def document(doc_id: str):
    row = db.conn().execute(f"SELECT {DOC_FIELDS} FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    return db.row_to_dict(row)


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
    row = db.conn().execute("SELECT collection FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    if row["collection"] != "case":
        raise HTTPException(403, "Library documents are managed from the data/library folder.")
    ingest.delete_document(doc_id)
    db.audit("document.delete", "document", doc_id)
    return {"ok": True}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str):
    row = db.conn().execute("SELECT path, filename FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    path = config.from_stored(row["path"]).resolve()
    if config.DATA_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "File missing")
    ext = path.suffix.lower()
    disposition = "attachment" if ext == ".docx" else "inline"
    resp = FileResponse(path, media_type=security.MIME.get(ext, "application/octet-stream"),
                        filename=row["filename"], content_disposition_type=disposition)
    if ext != ".pdf":  # browsers' PDF viewers do not run under a sandbox CSP
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    else:  # the document viewer embeds PDFs; only this origin may frame them
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return resp


@app.get("/api/documents/{doc_id}/text")
def document_text(doc_id: str):
    row = db.conn().execute("SELECT path, collection, title FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    if row["collection"] == "draft":
        return {"title": row["title"], "text": config.from_stored(row["path"]).read_text("utf-8")}
    chunks, _ = ingest.document_text(doc_id)
    return {"title": row["title"], "text": "\n\n".join(t for *_, t in chunks)[:200000]}


@app.get("/api/chunks/{chunk_id}")
def chunk(chunk_id: int):
    row = db.conn().execute(
        "SELECT c.id, c.doc_id, c.header, c.text, c.label, c.page_start, c.page_end, d.title, d.collection "
        "FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=?", (chunk_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Passage not found")
    return dict(row)


# ── Library ──────────────────────────────────────────────────────────────────
@app.get("/api/library")
def library():
    out: dict[str, list] = {"bare_act": [], "draft": [], "judgment": []}
    for r in db.conn().execute(f"SELECT {DOC_FIELDS} FROM documents WHERE collection IN ('bare_act','draft','judgment') "
                               "ORDER BY title"):
        d = db.row_to_dict(r)
        out[d["collection"]].append(d)
    return out


@app.get("/api/library/lookup")
def lookup(q: str = "", act: str | None = None, limit: int = 12):
    """Instant provision lookup (no LLM): exact section/article/rule match, else keyword search."""
    q = q.strip()[:300]
    if not q:
        return {"results": []}
    query = f"{q} {act}" if act else q
    refs = citations.parse_refs(query)
    doc_ids = retrieval.act_doc_ids([act]) if act else None
    ids = retrieval.exact(refs, "library", None, doc_ids)
    if len(ids) < limit:
        ids += [i for i in retrieval.lexical(query, limit * 2, "library", None, doc_ids) if i not in ids]
    hits = [h for h in retrieval.load_hits(ids[:limit * 2]) if h.collection == "bare_act"][:limit]
    return {"results": [{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "label": h.label,
                         "header": h.header, "text": h.text, "page": h.page_start, "exact": h.chunk_id in ids[:len(refs) * 6]}
                        for h in hits], "refs": [r.key for r in refs]}


# ── Limitation ───────────────────────────────────────────────────────────────
class LimitationIn(BaseModel):
    cause: str = Field(max_length=60)
    start_date: str = Field(max_length=10)


@app.get("/api/limitation")
def limitation_categories():
    return {"categories": tools.categories(), "caveat": tools.CAVEAT}


@app.post("/api/limitation")
def limitation(body: LimitationIn):
    try:
        return tools.limitation(body.cause, body.start_date)
    except KeyError:
        raise HTTPException(400, "Unknown category")
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")


# ── Chats ────────────────────────────────────────────────────────────────────
@app.get("/api/chats")
def chats(case_id: str | None = None):
    if case_id:
        rows = db.conn().execute("SELECT * FROM chats WHERE case_id=? ORDER BY updated_at DESC LIMIT 50", (case_id,))
    else:
        rows = db.conn().execute("SELECT * FROM chats WHERE case_id IS NULL ORDER BY updated_at DESC LIMIT 50")
    return {"chats": [dict(r) for r in rows]}


@app.get("/api/chats/{chat_id}")
def chat_messages(chat_id: str):
    chat = db.conn().execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not chat:
        raise HTTPException(404, "Chat not found")
    msgs = [db.row_to_dict(r) for r in db.conn().execute(
        "SELECT id, role, content, sources, meta, created_at FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))]
    return {"chat": dict(chat), "messages": msgs}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str):
    with db.tx() as c:
        c.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    return {"ok": True}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=6000)
    chat_id: str | None = Field(None, max_length=40)
    case_id: str | None = Field(None, max_length=40)
    scope: Literal["all", "case", "library"] = "all"
    doc_id: str | None = Field(None, max_length=40)
    task: Literal["summary", "timeline", "issues", "parties", "arguments", "custom"] | None = None
    lang: Literal["en", "hi", "mr"] = "en"
    input_mode: Literal["text", "voice"] = "text"


@app.post("/api/chat", dependencies=[Depends(rate(config.CHAT_RATE_PER_MIN, "chat"))])
def chat(body: ChatIn):
    def events():
        try:
            for evt in agent.run(message=body.message, chat_id=body.chat_id, case_id=body.case_id,
                                 scope=body.scope, doc_id=body.doc_id, task=body.task, lang=body.lang):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception:
            log.exception("chat stream failed")
            yield 'data: {"type":"error","text":"Internal error"}\n\n'

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ── Background jobs ──────────────────────────────────────────────────────────
@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found (the server may have restarted)")
    return {k: v for k, v in j.items() if k != "key"}


class ConsentIn(BaseModel):
    """Whole-document AI tasks send document text to the configured LLM; the UI must obtain consent."""
    consent: bool = False
    lang: Literal["en", "hi", "mr"] = "en"


def _need_consent(body: ConsentIn) -> None:
    if not body.consent:
        raise HTTPException(400, "This sends the document text to the AI service. Please confirm to continue.")


# ── Document intelligence ────────────────────────────────────────────────────
@app.get("/api/documents/{doc_id}/intel")
def document_intel(doc_id: str):
    try:
        return docintel.overview(doc_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/documents/{doc_id}/translate", dependencies=[Depends(rate(10, "docai"))])
def document_translate(doc_id: str, body: ConsentIn):
    _need_consent(body)
    try:
        docintel._doc(doc_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return jobs.submit("translate", lambda p: docintel.translate(doc_id, p), key=f"translate:{doc_id}")


@app.post("/api/documents/{doc_id}/summarize", dependencies=[Depends(rate(10, "docai"))])
def document_summarize(doc_id: str, body: ConsentIn):
    _need_consent(body)
    try:
        docintel._doc(doc_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return jobs.submit("summarize", lambda p: docintel.summarize(doc_id, body.lang, p), key=f"summarize:{doc_id}:{body.lang}")


# ── Drafting, notices, forums ────────────────────────────────────────────────
@app.get("/api/drafting/config")
def drafting_config():
    return {"types": [{k: t[k] for k in ("id", "label", "kind", "categories")} for t in drafting.doc_types()],
            "forums": [{k: f[k] for k in ("id", "name", "level")} for f in drafting.forums()],
            "styles": drafting.styles(), "saved_style": db.get_pref("draft_style") or "formal"}


@app.get("/api/drafting/types/{type_id}")
def drafting_type(type_id: str):
    try:
        return drafting.type_profile(type_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/drafting/forums/{forum_id}")
def drafting_forum(forum_id: str):
    try:
        return drafting.forum_profile(forum_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class ForumReqIn(BaseModel):
    text: str = Field(min_length=3, max_length=500)
    url: str = Field(min_length=8, max_length=500, pattern=r"^https://")
    verified_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.post("/api/drafting/forums/{forum_id}/requirements")
def add_forum_requirement(forum_id: str, body: ForumReqIn):
    try:
        drafting.forum(forum_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    items = drafting.user_requirements(forum_id) + [body.model_dump()]
    db.set_pref(f"forum_reqs:{forum_id}", json.dumps(items[-50:], ensure_ascii=False))
    db.audit("forum.requirement_add", "forum", forum_id, url=body.url)
    return drafting.forum_profile(forum_id)


class DraftIn(BaseModel):
    doc_type: str = Field(max_length=40)
    forum: str = Field(max_length=40)
    style: str = Field("formal", max_length=20)
    lang: Literal["en", "hi", "mr"] = "en"
    case_id: str | None = Field(None, max_length=40)
    doc_ids: list[str] = Field(default_factory=list, max_length=6)
    known: dict[str, str] = Field(default_factory=dict)
    instructions: str = Field("", max_length=4000)
    template_doc_id: str | None = Field(None, max_length=40)
    base_doc_id: str | None = Field(None, max_length=40)
    base_text: str | None = Field(None, max_length=20000)
    title: str | None = Field(None, max_length=200)
    draft_id: str | None = Field(None, max_length=40)
    consent: bool = False
    save_style: bool = False


@app.post("/api/drafts/generate", dependencies=[Depends(rate(10, "draft"))])
def draft_generate(body: DraftIn):
    if body.doc_ids or body.base_doc_id:
        _need_consent(ConsentIn(consent=body.consent))
    try:
        drafting.doc_type(body.doc_type)
        drafting.forum(body.forum)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if len(body.known) > 40 or any(len(k) > 60 for k in body.known):
        raise HTTPException(400, "Too many details")
    if body.save_style:
        db.set_pref("draft_style", body.style)
    args = body.model_dump(exclude={"consent", "save_style", "doc_type", "forum"})
    return jobs.submit("draft", lambda p: drafting.generate(doc_type_id=body.doc_type, forum_id=body.forum,
                                                              progress=p, **args))


@app.get("/api/drafts")
def drafts_list(case_id: str | None = None, kind: str | None = None):
    return {"drafts": drafting.list_drafts(case_id, kind)}


@app.get("/api/drafts/{draft_id}")
def draft_get(draft_id: str):
    d = drafting.get(draft_id)
    if not d:
        raise HTTPException(404, "Draft not found")
    return d


class DraftEditIn(BaseModel):
    content: str = Field(max_length=200000)
    title: str | None = Field(None, max_length=200)


@app.put("/api/drafts/{draft_id}")
def draft_put(draft_id: str, body: DraftEditIn):
    d = drafting.update_content(draft_id, body.content, body.title)
    if not d:
        raise HTTPException(404, "Draft not found")
    return d


@app.get("/api/drafts/{draft_id}/versions/{v}")
def draft_version(draft_id: str, v: int):
    d = drafting.version(draft_id, v)
    if not d:
        raise HTTPException(404, "Version not found")
    return d


@app.delete("/api/drafts/{draft_id}")
def draft_delete(draft_id: str):
    if not drafting.delete(draft_id):
        raise HTTPException(404, "Draft not found")
    return {"ok": True}


@app.get("/api/drafts/{draft_id}/export")
def draft_export(draft_id: str, format: Literal["docx", "pdf"] = "docx"):
    d = drafting.get(draft_id)
    if not d:
        raise HTTPException(404, "Draft not found")
    if format == "docx":
        data, mime = export.to_docx(d["title"], d["content"], d["sources"]), security.MIME[".docx"]
    else:
        data, mime = export.to_pdf(d["title"], d["content"], d["sources"]), "application/pdf"
    name = security.safe_filename(f"{d['title'][:80]}_v{d['version']}.{format}")
    db.audit("draft.export", "draft", draft_id, format=format, version=d["version"])
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── Applicable provisions ───────────────────────────────────────────────────
class ProvSuggestIn(BaseModel):
    consent: bool = False
    extra: str = Field("", max_length=6000)
    doc_ids: list[str] | None = None


@app.post("/api/cases/{case_id}/provisions/suggest", dependencies=[Depends(rate(10, "prov"))])
def provisions_suggest(case_id: str, body: ProvSuggestIn):
    _need_consent(ConsentIn(consent=body.consent))
    if not cases.get_case(case_id):
        raise HTTPException(404, "Case not found")
    return jobs.submit("provisions", lambda p: provisions.suggest(case_id, doc_ids=body.doc_ids, extra=body.extra, progress=p),
                       key=f"prov:{case_id}")


@app.get("/api/cases/{case_id}/provisions")
def provisions_list(case_id: str):
    return {"provisions": provisions.list_for(case_id)}


class ProvUpdateIn(BaseModel):
    status: Literal["accepted", "rejected", "suggested", "edited"] | None = None
    note: str | None = Field(None, max_length=1000)
    ref: str | None = Field(None, max_length=120)


@app.patch("/api/provisions/{prov_id}")
def provisions_update(prov_id: str, body: ProvUpdateIn):
    p = provisions.update(prov_id, status=body.status, note=body.note, ref=body.ref)
    if not p:
        raise HTTPException(404, "Not found")
    return p


@app.get("/api/library/provision")
def library_provision(act: str, ref: str):
    from . import library
    return library.lookup_provision(act[:120], ref[:60])


# ── Case information search, imports, integrations ──────────────────────────
class CaseSearchIn(BaseModel):
    cnr_number: str | None = None
    case_number: str | None = None
    filing_number: str | None = None
    fir_number: str | None = None
    court: str | None = None
    case_type: str | None = None
    state: str | None = None
    district: str | None = None
    police_station: str | None = None
    filing_year: str | None = None
    petitioner: str | None = None
    respondent: str | None = None


@app.post("/api/casesearch", dependencies=[Depends(rate(30, "casesearch"))])
def case_search(body: CaseSearchIn):
    return casesearch.search(body.model_dump())


class OrderIn(BaseModel):
    date: str | None = Field(None, max_length=20)
    title: str | None = Field(None, max_length=300)
    url: str | None = Field(None, max_length=500)


class RecordImportIn(CaseSearchIn):
    source: str = Field(max_length=40)
    title: str | None = Field(None, max_length=300)
    category: str | None = Field(None, max_length=20)
    status: str | None = Field(None, max_length=120)
    stage: str | None = Field(None, max_length=120)
    judge: str | None = Field(None, max_length=200)
    next_hearing_date: str | None = Field(None, max_length=10)
    sections: str | None = Field(None, max_length=300)
    orders: list[OrderIn] = Field(default_factory=list, max_length=50)
    source_url: str | None = Field(None, max_length=500, pattern=r"^https://")
    case_id: str | None = Field(None, max_length=40)


@app.post("/api/records/import")
def record_import(body: RecordImportIn):
    data = body.model_dump(exclude={"source", "case_id"})
    data["orders"] = [o for o in data["orders"]]
    try:
        return casesearch.import_record(body.source, data, body.case_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/records/{record_id}/save-case")
def record_save_case(record_id: str):
    try:
        return casesearch.save_to_case(record_id)
    except cases.FieldError as exc:
        return _field_error(exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/cases/{case_id}/records")
def case_records(case_id: str):
    return {"records": casesearch.records_for_case(case_id)}


@app.get("/api/integrations")
def integrations_status():
    return {"integrations": integrations.status(), "scheduler": bool(_scheduler and _scheduler.is_alive())}


@app.post("/api/integrations/{iid}/sync", dependencies=[Depends(rate(6, "sync"))])
def integrations_sync(iid: str):
    try:
        return integrations.run_sync(iid, "manual")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class EnabledIn(BaseModel):
    enabled: bool


@app.post("/api/integrations/{iid}/enabled")
def integrations_enable(iid: str, body: EnabledIn):
    if iid not in integrations.ADAPTERS:
        raise HTTPException(404, "Unknown integration")
    integrations.set_enabled(iid, body.enabled)
    return {"ok": True}


@app.get("/api/news")
def news(q: str = "", limit: int = 50):
    return {"items": integrations.news(max(1, min(limit, 200)), q[:100])}


# ── Legal awareness, unified search ─────────────────────────────────────────
@app.get("/api/awareness")
def awareness_list(state: str = "MH", subject: str | None = None, q: str = ""):
    return awareness.listing(state[:20], subject, q[:100])


@app.get("/api/search")
def unified_search(q: str = "", types: str = "", court: str = "", state: str = "", year: str = "", case_type: str = "",
                   doc_type: str = "", source: str = ""):
    t = {x for x in types.split(",") if x} or None
    return search.search(q, types=t, court=court[:100], state=state[:60], year=year[:4], case_type=case_type[:20],
                         doc_type=doc_type[:20], source=source[:40])


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
