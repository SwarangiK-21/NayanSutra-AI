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

from . import agent, cases, citations, config, db, embeddings, ingest, llm, retrieval, security, tools
from .vectorindex import index

log = logging.getLogger("nyaya")

_ingest_pool: ThreadPoolExecutor | None = None  # created per app lifespan
_state = {"models_ready": False, "llm": None, "llm_checked": 0.0}


def _warm():
    index.load()          # vectors from SQLite → RAM (background, so the UI is up immediately)
    embeddings.warm_up()  # load ONNX models
    _state["models_ready"] = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ingest_pool
    _ingest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
    db.init()
    cases.seed_if_empty()
    stored = db.get_meta("embed_model")
    if stored and stored != embeddings.model_name():
        log.warning("Index was built with %s but EMBED_MODEL is %s — run: python -m nyaya.ingest reembed",
                    stored, embeddings.model_name())
    # Re-queue uploads interrupted by a restart.
    for r in db.conn().execute("SELECT id FROM documents WHERE status='processing'").fetchall():
        _ingest_pool.submit(ingest.process, r["id"])
    threading.Thread(target=_warm, daemon=True).start()
    yield
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
    response.headers["Content-Security-Policy"] = security.CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
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


# ── Cases ────────────────────────────────────────────────────────────────────
class CaseIn(BaseModel):
    title: str | None = Field(None, max_length=200)
    case_type: str | None = Field(None, max_length=20)
    case_number: str | None = Field(None, max_length=100)
    court: str | None = Field(None, max_length=200)
    client_name: str | None = Field(None, max_length=200)
    client_role: str | None = Field(None, max_length=100)
    opposite_party: str | None = Field(None, max_length=200)
    filing_date: str | None = Field(None, max_length=10)
    next_hearing_date: str | None = Field(None, max_length=10)
    status: str | None = Field(None, max_length=40)
    acts_sections: str | None = Field(None, max_length=600)
    description: str | None = Field(None, max_length=6000)


class NoteIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    author: str = Field("Advocate", max_length=80)


@app.get("/api/cases")
def list_cases():
    return {"cases": cases.list_cases(), "types": cases.CASE_TYPES, "statuses": cases.CASE_STATUSES}


@app.post("/api/cases")
def create_case(body: CaseIn):
    try:
        return cases.create_case(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    c = cases.get_case(case_id)
    if not c:
        raise HTTPException(404, "Case not found")
    return c


@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, body: CaseIn):
    c = cases.update_case(case_id, body.model_dump(exclude_none=True))
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
        sql += " AND case_id=?"
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
        raise HTTPException(415, "Only PDF, DOCX, TXT and MD files are accepted.")
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
    return {"ok": True}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str):
    row = db.conn().execute("SELECT path, filename FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    path = Path(row["path"]).resolve()
    if config.DATA_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "File missing")
    ext = path.suffix.lower()
    disposition = "attachment" if ext == ".docx" else "inline"
    resp = FileResponse(path, media_type=security.MIME.get(ext, "application/octet-stream"),
                        filename=row["filename"], content_disposition_type=disposition)
    if ext != ".pdf":  # browsers' PDF viewers do not run under a sandbox CSP
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp


@app.get("/api/documents/{doc_id}/text")
def document_text(doc_id: str):
    row = db.conn().execute("SELECT path, collection, title FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    if row["collection"] == "draft":
        return {"title": row["title"], "text": Path(row["path"]).read_text("utf-8")}
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


@app.post("/api/chat", dependencies=[Depends(rate(config.CHAT_RATE_PER_MIN, "chat"))])
def chat(body: ChatIn):
    def events():
        try:
            for evt in agent.run(message=body.message, chat_id=body.chat_id, case_id=body.case_id,
                                 scope=body.scope, doc_id=body.doc_id, task=body.task):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception:
            log.exception("chat stream failed")
            yield 'data: {"type":"error","text":"Internal error"}\n\n'

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
