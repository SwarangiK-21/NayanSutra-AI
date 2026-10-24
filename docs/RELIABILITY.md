# NyaySutra AI — Reliable AI workflows and document pipeline (Part 2)

Scope agreed with the owner: the seven "worth adding" items from the Part 2 review. Not built, on purpose: separate AI
agents per role, an AI supervisor or safety agent, and new infrastructure (Celery, Redis, Kafka, a vector database).
The reasons are given in §5. Everything here keeps the Part 1 controls (`docs/SECURITY.md`) in force.

## 1. Problems found in the existing code

| # | Problem | Effect |
|---|---|---|
| 1 | Background jobs (translation, summaries, drafts, provision suggestions) lived in memory | A restart lost them; no queued/cancelled states; no cancel |
| 2 | Ingestion re-queued every "processing" document on each restart, with no attempt counter | A file that crashes the parser was retried forever; no dead letter; no quarantine; no check for blank, garbled or missing pages |
| 3 | Bulk upload was a loop of single uploads | No limits on count or total size, no batch status, no error report, no cancel |
| 4 | LLM calls: 2 fixed retries, no jitter, no circuit breaker, no fallback | Provider hiccups failed answers outright, or made every request wait for timeouts |
| 5 | No record of how an answer was produced | No way to see which step failed, which model answered, or whether verification ran |
| 6 | Drafts were labelled for review but had no review state | Nothing distinguished an approved document from an unreviewed one |
| 7 | Replacing a bare-act PDF added a second document and kept the old one "ready" | Old and new text were both searched; the old record pointed at the new file |

## 2. What was built

**1. Durable jobs** (`nyaya/jobs.py`, `nyaya/tasks.py`, migration 6)
- Every long task is a `jobs` row with a state: queued, processing, completed, partial, failed, cancelled or dead.
- Workers claim jobs with `FOR UPDATE SKIP LOCKED`, so jobs survive restarts and are never run twice, even with several app instances.
- Two lanes with their own limits on parallel work: `INGEST_WORKERS` (1, CPU-bound) and `JOB_WORKERS` (2, waiting on the LLM).
- Only the owner can read, cancel or retry a job (row-level security plus API checks).
- **Before running, the worker re-checks the owner's current permissions.** Someone removed from a case after queueing gets "access withdrawn", and no document text reaches the model.
- Time limit per job (`JOB_TIMEOUT_SECONDS`). Cancellation and the time limit take effect at each progress report. Long tasks save checkpoints: translations and summaries resume from the last finished part.
- Temporary errors are retried with random, growing waits. After `max_attempts` the job ends as `dead` and the owner is notified, instead of retrying forever. Permanent errors fail immediately.
- An interrupted job (expired lock) goes back to the queue. Finished jobs are deleted after 30 days.

**2. Ingestion hardening** (`nyaya/ingest.py`, `nyaya/quality.py`, `nyaya/parsing.py`)
- Steps: validate, extract, quality check, chunk, embed, index, publish (`ready`). Each step reports progress and can be cancelled. A repeated run replaces passages instead of duplicating them.
- Content problems (unreadable, needs OCR, mostly garbled) **quarantine** the document: it is kept and the reason is shown, but it is never indexed or sent to the AI. Other errors are retried, then the document is marked failed with a "Process again" button.
- Quality report per document:
  - blank pages
  - garbled pages (replacement characters, too few letters)
  - **missing pages**, detected from gaps in the printed page numbers ("Page 4 of 8")
  - scanned pages that still need OCR
- Each document keeps a `uploaded_by` value; ingestion jobs belong to the uploader.

**3. Bulk upload** (`/api/cases/{id}/batches`, `/api/batches/…`, upload page)
- Limits are checked before any file is sent: `BATCH_MAX_FILES` (50) and `BATCH_MAX_TOTAL_MB` (500), plus the existing per-file `MAX_UPLOAD_MB`. Each file gets a slot.
- An upload names its slot. The server checks that the slot is the caller's, for this case, still waiting, and for this file.
- Files are streamed to disk in 1 MB pieces, sent one at a time by the browser, and processed in the background.
- Per-file outcome: waiting, processing, ready, failed, rejected or cancelled. Batch states: queued, processing, partial, completed, failed, cancelling, cancelled.
- Cancel stops files not yet sent and stops queued processing; finished files are kept.
- The downloadable CSV report has file names and reasons only, never contents. Cells that could start a spreadsheet formula are escaped. It is available to the uploader only.
- Leftover temporary upload folders are swept at start-up.

**4. LLM resilience** (`nyaya/llm.py`)
- Bounded retries (`LLM_RETRIES`) with random, capped waits on overload, server errors and network errors.
- A per-model circuit breaker: after `LLM_BREAKER_FAILURES` consecutive failures, that model is skipped for `LLM_BREAKER_SECONDS`.
- An approved fallback model (`LLM_FALLBACK_MODEL`, default gpt-oss:20b) for the main answer model. It uses the same provider and the same, already authorised prompt, so authorization and PII masking are unchanged.
- No fallback for a rejected API key, and never in the middle of a streamed answer: a cut-off answer is marked **incomplete** and not verified.
- Every call reports which model answered, and answers say when the fallback was used.

**5. Workflow trace** (`nyaya/agent.py`, research page)
- Every answer stores the steps it went through, with status and model, but never document text: access check, request screening, understanding the request, tool policy, search, search refinement, answer, citation check, output check.
- Optional steps that fail are skipped, not fatal. If the AI citation audit could not run, the answer says "citations checked by code only". The page shows "How this answer was produced".

**6. Draft review** (`nyaya/drafting.py`, `/api/drafts/{id}/review`, draft editor)
- States: draft, changes requested, approved. Approval is recorded for one version, by a named advocate on the case team.
- Approval is refused while placeholders, invalid citations or unknown fact tags remain. Any later edit makes a new version that needs a new approval.
- Exports of the approved version say "Approved by … on … (version n)". Every other export keeps "DRAFT — FOR REVIEW". Approvals are in the activity log.

**7. Library versions** (`ingest.supersede_older`, Bare Acts page)
- A changed act or judgment becomes version n+1. Earlier versions become `superseded`: kept and readable, but removed from search and from the library list.
- Retrieval never returns superseded text. The Bare Acts page shows the version and a list of versions.

## 3. Files

| New | Changed |
|---|---|
| `nyaya/tasks.py`, `nyaya/quality.py`, `supabase/migrations/20261025000006_jobs_batches_reviews.sql`, `web/js/jobs.js`, `tests/test_reliability.py`, `docs/RELIABILITY.md` | `nyaya/jobs.py` (rewritten), `nyaya/llm.py`, `nyaya/agent.py`, `nyaya/ingest.py`, `nyaya/parsing.py`, `nyaya/docintel.py`, `nyaya/drafting.py`, `nyaya/export.py`, `nyaya/retrieval.py`, `nyaya/accounts.py`, `nyaya/config.py`, `nyaya/db.py`, `nyaya/server.py`, `web/app.js`, `web/js/core.js`, `web/js/docs.js`, `web/js/drafting.js`, `web/i18n/en.json`, `web/i18n/hi.json`, `tests/test_live_security.py`, tests updated for the new states |

## 4. Dependencies, cost and tests

- **Dependencies:** none added. The job queue uses the existing Supabase Postgres (`SKIP LOCKED`), or SQLite offline.
- **Cost:** no new paid services. A few more database rows (jobs, batches), with finished jobs deleted after 30 days. Model calls are not increased: the fallback replaces failed calls, and checkpoints avoid re-paying for finished translation parts.
- **Offline tests:** 102 pass, 17 of them new in `tests/test_reliability.py`:
  - job durability, ownership and de-duplication
  - poison file → bounded retries → dead letter + notification → retry
  - quarantine; quality checks (blank, garbled, missing printed pages)
  - cancelling queued and running jobs; time limits, permanent errors and stale-lock recovery
  - translation resuming from its checkpoint
  - **re-authorization: a job stops when access is withdrawn before it runs**
  - batch limits, slot checks, progress, cancel and CSV report (formula escaping, no contents)
  - LLM: retry then success; fallback and the circuit opening; no fallback for a bad key; stream fallback only before the first token
  - trace without document text; cut-off answer labelled incomplete
  - draft approval per version, with the export header
  - library versions leave search
- **Live tests** against the Supabase project, with synthetic accounts removed afterwards: 4 pass, including the Postgres job queue (single claim, owner-only under RLS).
- **Browser check** with a temporary QA account (deleted afterwards): batch upload (2 ready, 1 rejected, partial), Jobs page, and research with the trace showing gpt-oss:20b and gpt-oss:120b.

## 5. Deliberately not built

| Suggested | Why not |
|---|---|
| AI supervisor plus separate AI agents for research, documents, drafting, verification and safety | Each agent means extra model calls: more cost and delay, Ollama plan limits reached sooner, more prompt-injection surface. The existing planner routes requests already; specialists are code modules with fixed tools, and the supervisor's job (limits, retries, states, verification) is done by code |
| Celery, Redis, Kafka, a vector database | One server and about 9,500 passages. Postgres holds the queue; revisit with several servers or about 200,000 passages |

## 6. Remaining risks

| Item | Note |
|---|---|
| Cancellation and time limits are cooperative | They act at the next progress report. A single model call (up to `LLM_TIMEOUT`) or a long PDF parse is not interrupted mid-way |
| Per-IP rate limits and the circuit breaker are per process | With several app instances each keeps its own counters (the job queue itself is shared) |
| Fallback model quality | gpt-oss:20b is weaker. Answers say so; important research should be re-run when the main model is back |
| Missing-page detection | Needs at least three printed page numbers; documents without page numbers are not checked |
| Quarantine thresholds | Garbled-text detection is heuristic; borderline scans may pass with warnings only |
| Batch report | Plain CSV for the uploader; it is not encrypted beyond the HTTPS/loopback connection |
| Approval is self-approval when one advocate works alone | Recorded with name and version; a second-reviewer rule could be added if the firm wants it |
| Leaked-password protection | Supabase's advisor now reports it disabled. It is a dashboard setting, and may need a paid plan |
