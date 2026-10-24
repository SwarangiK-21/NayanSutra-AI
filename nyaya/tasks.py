"""The background tasks the job queue may run, and the permission each one re-checks when it runs.

The same `authorize_*` function is used by the API before queueing (immediate 403/404) and by the worker before
executing, because the owner's access may have changed in between (removed from the case, role changed, disabled).
Tasks receive only their validated parameters and a context for progress, cancellation and checkpoints — no session,
no credentials, no direct database access beyond what the owner's identity allows.
"""
from __future__ import annotations

from . import authz, config, db, docintel, drafting, ingest, jobs, provisions


# ── Document ingestion (internal: runs in the system context after the upload was authorised) ──
def _ingest_finished(params: dict, state: str, error: str | None) -> None:
    """A document whose processing was cancelled or kept failing must not stay 'processing' forever."""
    status = {"cancelled": "cancelled", "dead": "failed", "failed": "failed"}.get(state)
    if not status:
        return
    msg = "Processing was cancelled." if status == "cancelled" else \
        f"Processing failed repeatedly ({error or 'unknown error'}). You can retry it from the case page."
    with db.tx() as c:
        c.execute("UPDATE documents SET status=?, error=? WHERE id=? AND status='processing'", (status, msg, params.get("doc_id")))


def _ingest_retry(params: dict) -> None:
    with db.tx() as c:
        c.execute("UPDATE documents SET status='processing', error=NULL WHERE id=? AND status IN ('failed','cancelled')",
                  (params.get("doc_id"),))


@jobs.handler("ingest", lane="ingest", max_attempts=config.INGEST_MAX_ATTEMPTS, timeout=config.JOB_TIMEOUT_SECONDS,
              on_finish=_ingest_finished, on_retry=_ingest_retry)
def ingest_task(params: dict, ctx: jobs.Ctx) -> dict:
    doc_id = params.get("doc_id")
    row = db.conn().execute("SELECT case_id, status FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return {"skipped": "The document was deleted."}
    if (row["case_id"] or None) != (params.get("case_id") or None):   # never trust a job's parameters blindly
        raise jobs.Rejected("The job does not match the document.")
    if row["status"] != "processing":
        return {"skipped": f"The document is {row['status']}."}
    res = ingest.process(doc_id, progress=ctx, raise_transient=True)
    return {"doc_id": doc_id, "status": res.get("status"), "passages": res.get("n_chunks"), "error": res.get("error")}


# ── Document intelligence ────────────────────────────────────────────────────
def authorize_doc_ai(p: authz.Principal, params: dict) -> None:
    collection, _ = authz.require_document(p, params["doc_id"], "documents.ai")
    if collection != "case":
        raise authz.Denied("Translation and summaries are for case documents.")


@jobs.handler("translate", authorize=authorize_doc_ai, timeout=config.JOB_TIMEOUT_SECONDS)
def translate_task(params: dict, ctx: jobs.Ctx) -> dict:
    return docintel.translate(params["doc_id"], ctx)


@jobs.handler("summarize", authorize=authorize_doc_ai, timeout=config.JOB_TIMEOUT_SECONDS)
def summarize_task(params: dict, ctx: jobs.Ctx) -> dict:
    return docintel.summarize(params["doc_id"], params.get("lang", "en"), ctx)


# ── Drafting ─────────────────────────────────────────────────────────────────
def authorize_draft(p: authz.Principal, params: dict) -> None:
    authz.require(p, "drafting.use")
    case_id = params.get("case_id")
    if case_id:
        authz.require_case(p, case_id, "drafts")
    for d in [*(params.get("doc_ids") or []), *([params["base_doc_id"]] if params.get("base_doc_id") else [])]:
        collection, doc_case = authz.require_document(p, d, "documents.ai")
        if collection != "case" or doc_case != case_id:
            raise authz.NotFound("A supporting document was not found in this case")
    if params.get("template_doc_id") and (authz.document_case(params["template_doc_id"]) or ("", None))[0] != "draft":
        raise authz.NotFound("Template not found")
    if params.get("draft_id"):
        with db.system():
            row = db.conn().execute("SELECT case_id, created_by FROM drafts WHERE id=?", (params["draft_id"],)).fetchone()
        ok = row and row["case_id"] == case_id and (
            (row["case_id"] and authz.can_case(p, row["case_id"], "drafts")) or
            (not row["case_id"] and row["created_by"] == p.user_id))
        if not ok:
            raise authz.NotFound("Draft not found")


@jobs.handler("draft", authorize=authorize_draft, timeout=config.JOB_TIMEOUT_SECONDS)
def draft_task(params: dict, ctx: jobs.Ctx) -> dict:
    args = {k: v for k, v in params.items() if k not in ("doc_type", "forum")}
    return drafting.generate(doc_type_id=params["doc_type"], forum_id=params["forum"], progress=ctx, **args)


# ── Applicable provisions ───────────────────────────────────────────────────
def authorize_provisions(p: authz.Principal, params: dict) -> None:
    authz.require_case(p, params["case_id"], "provisions")


@jobs.handler("provisions", authorize=authorize_provisions, timeout=config.JOB_TIMEOUT_SECONDS)
def provisions_task(params: dict, ctx: jobs.Ctx) -> dict:
    return provisions.suggest(params["case_id"], doc_ids=params.get("doc_ids"), extra=params.get("extra", ""), progress=ctx)
