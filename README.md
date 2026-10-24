# NyaySutra AI — Agentic RAG Legal Assistant for Indian Advocates

Upload a matter's documents, then research, summarise, build timelines and draft — every answer is
grounded in your files and in official bare acts, with numbered citations and an automatic citation audit.

## Quick start (Windows)

```powershell
# one-time: create the environment on the SSD (fast imports) and install dependencies
py -3.12 -m venv "$env:USERPROFILE\.nyaysutra\venv"
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" -m pip install -r requirements.txt

# one-time: download official bare acts and build the law library index (~25 min on a laptop CPU)
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" scripts\fetch_bare_acts.py
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" -m nyaya.ingest library

# run
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" run.py      # opens http://127.0.0.1:8000
```

Or just double-click **`start.bat`**. Configuration lives in `.env` (see `.env.example`).

**Accounts.** With Supabase configured (`SUPABASE_URL` + `SUPABASE_PUBLISHABLE_KEY`) every person signs in with their
own account. Create the first administrator once, then sign in and set up two-step verification:

```powershell
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" scripts\bootstrap_admin.py --email you@example.org
```

Administrators invite everyone else (Administration → Invite someone) and assign people to cases. Without Supabase
the app runs in local single-user mode, as before. Details, roles and the Supabase dashboard settings needed for
e-mail links: [docs/SECURITY.md](docs/SECURITY.md).

## What it does

| Area | Features |
|---|---|
| Case files | Civil / Criminal / Family / Revenue / Consumer / Writ matters; party roles that follow the category; CNR, case, filing and FIR numbers, police station, state → district, court (searchable), filing year; save incomplete cases as drafts |
| Case search | Any combination of identifiers; validates CNR / FIR requirements, infers jurisdiction, searches your workspace, lists the official portal and exact values to enter, imports what you find (change-detected) |
| Documents | PDF (incl. scans via optional OCR), DOC/DOCX, TXT/MD, images; language + script detection (English, Hindi, Marathi, other Indian scripts); original, extracted text, English translation and summaries side by side; batch upload with progress |
| Document intelligence | English translation (indexed, so English questions find Hindi/Marathi files), structured summary — parties, dates, case numbers, facts, claims, relief, provisions, deadlines, action items — each linked to its page and checked against the text; summaries in Hindi / Marathi |
| Legal research | Streaming answers with `[n]` citations and citation audit, in English or Hindi; voice input (hi-IN / mr-IN / en-IN) with editable transcription; read-aloud; focus on one case document |
| Applicable provisions | Suggests acts / sections / articles chosen only from the official library, with page links, temporal checks (IPC→BNS on 1 July 2024), accept / reject / edit |
| Drafting studio | Document types × courts/forums × styles; facts with provenance (file / your input / case), missing-information questions, placeholders instead of guesses, side-by-side editor, versions, Word and PDF export (Hindi/Marathi shaped correctly) |
| Legal notices | Structured form; statutory periods only from the provision text; special formalities flagged |
| Updates & sources | LiveLaw RSS (hourly) and India Code act records (daily) synced with retries, back-off, conditional GET and logs; manual-only portals shown as such; notifications; activity log |
| Legal awareness | Maharashtra first: licences, registration, road safety, challans, accident claims — with official statutory text (Motor Vehicles Act), official links and verification dates |
| Search | One search across cases, documents, bare acts, judgments, drafts, news and awareness, with filters |
| Bare acts | 30 official texts (Constitution, BNS, BNSS, BSA, IPC, CPC, … MLRC, Motor Vehicles Act) with instant section lookup; precedent search over the judgment library |
| Languages | English / हिंदी toggle (JSON dictionaries in `web/i18n/`; Marathi dictionary started) |
| Limitation | 24 deterministic limitation periods (Limitation Act Schedule, NI Act, Arbitration Act, HMA, MLRC, CPA) |

## External sources — what is automated and what is not

| Source | Access | How NyaySutra uses it |
|---|---|---|
| LiveLaw | Official RSS feed | Polled hourly (conditional GET); headlines + links only |
| India Code | Public DSpace API | Daily check for changed act records → notification to re-fetch |
| eCourts, Supreme Court, Bombay High Court | CAPTCHA-protected, no API | **Manual**: open official source, then import the record; re-imports are change-detected |
| Maharashtra Police FIR portal | CAPTCHA / login, no API | **Manual** (FIR needs state + district + police station + year + number) |
| LatestLaws | No public feed / API advertised | **Manual** link only |

No source offers webhooks to a local app, so nothing is labelled "live". CAPTCHAs, logins and rate limits are never bypassed.

## Optional: OCR for scanned documents
Install Tesseract with **eng, hin and mar** language data (Windows: UB-Mannheim build, tick Hindi and Marathi), then
restart. It is detected automatically, or set `TESSERACT_CMD` in `.env`. Without it, scanned PDFs and images are rejected
with a clear message — text is never guessed. Low-confidence OCR words are shown as `[?word]`.

## Database migrations
`nyaya/db.py` applies versioned, additive migrations at start-up (`meta.schema_version`); new columns are added only when
missing. Migration 1 adds case identifiers/jurisdiction columns and the tables `prefs`, `audit_log`, `notifications`,
`integrations`, `sync_log`, `source_records`, `doc_derivatives`, `drafts`, `draft_versions`, `case_provisions`.
Back up `data/nyaysutra.db` before upgrading.

## Scheduled jobs
A background thread (`nyaya/integrations.py`, `SYNC_ENABLED=true`) checks every `SYNC_TICK_SECONDS` which sources are
due. Failures back off 5 → 10 → 20 min (capped at the source interval), are logged in `sync_log`, and notify after 3
failures. Document processing and long AI tasks (translation, summaries, drafting, provisions) are durable jobs in the
database: they survive restarts, retry temporary failures a bounded number of times, can be cancelled, and are listed
on the **Jobs** page. Details: [docs/RELIABILITY.md](docs/RELIABILITY.md).

## Known limitations
* Security limits and open items are listed in [docs/SECURITY.md](docs/SECURITY.md) (section "Remaining risks").
* OCR requires Tesseract (not bundled). Legacy `.doc` needs `antiword` on PATH.
* Court-specific formatting rules (paper, margins, fonts) are not assumed — forums list official sources and "confirm"
  items; you can add requirements you verified (with source URL and date).
* Legal-awareness content and backend notices are in English; the interface is English / Hindi; Marathi is partial.
* Speech recognition is provided by the browser (Chrome/Edge send audio to their speech service; NyaySutra never receives audio).

## Architecture

```
web/ (vanilla JS, gov-portal theme) ──SSE──► nyaya/server.py (FastAPI, security middleware)
                                               │
                                               ▼
                                         nyaya/agent.py   plan → route
     ┌──────────────┬───────────────┬─────────┴───────┬──────────────────┐
  research       drafting      analyze_document   calculation        chitchat
     │              │               │                 │
 retrieval.py   template +      whole doc /       tools.py (Python
 exact + BM25 + retrieval       map-reduce        date arithmetic)
 dense + act-focus → RRF → cross-encoder rerank → weak? rewrite → abstain
     │
 llm.py (Ollama: gpt-oss:120b answers, gpt-oss:20b plan/verify) → stream → citation audit
     │
 db.py  Supabase Postgres + RLS (or SQLite offline): cases · documents · chunks · embeddings · chats   vectorindex.py
 authz.py (roles, case access) · accounts.py + sessions.py (Supabase Auth, server-side sessions) · guardrails.py
 ingest.py  parsing.py (PDFium) → chunking.py (section / paragraph / clause aware) → embeddings.py (fastembed ONNX)
```

### Chunking strategy
* **Bare acts** — one chunk per Section / Article / Order-Rule (e.g. `Order XXXIX Rule 1`), with act, chapter,
  number and title in a context header. Table-of-contents lines and footnotes are filtered; long provisions are split
  at sub-section boundaries and re-merged at answer time. Lookup keys (`S138`, `A21`, `O39R1`) enable exact retrieval.
* **Judgments** — numbered paragraphs grouped into ~1.1k-char chunks labelled `paras 12–15`, with page numbers.
* **Contracts / pleadings** — numbered clauses grouped the same way.
* **Other** — sentence-aware windows with overlap.

### Why these components
* **SQLite + FTS5 + NumPy** instead of a vector-DB server: one local file, ACID, BM25 for free, exact (not approximate)
  vector search in milliseconds for tens of thousands of chunks, no cloud account.
* **fastembed (ONNX)** instead of torch: ~25× smaller install, fast cold start, embeddings stay private.
* **Cross-encoder reranker** decides relevance far better than either retriever and gates weak evidence.

## Security
Full description, test evidence and remaining risks: [docs/SECURITY.md](docs/SECURITY.md) (audit and risk register:
[docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md)). In short:
* Per-user accounts on Supabase Auth (verified e-mail, reset links, TOTP two-step verification + recovery codes),
  used server-side; the browser holds only an HttpOnly session cookie. Server-side sessions with idle/absolute expiry,
  revocation and rotation; account lockout; CSRF header; Host allow-list; Origin check.
* Roles (client, advocate, administrator, security auditor) assigned only by an administrator; a central permission
  matrix (`nyaya/authz.py`) and per-case membership checked on every request; the same rules enforced again by
  Postgres row-level security.
* Uploads: extension allow-list, magic-byte check, active-content check (PDF JavaScript, Word macros/remote
  templates), optional ClamAV, size limit, server-generated names. Strict CSP; all UI text escaped.
* AI guardrails in code: request screening, delimited and injection-flagged sources, allow-listed tools with
  validated arguments, deterministic citation verification, output secret filter, identity numbers masked before
  text is sent to the LLM (Ollama Cloud).
* Append-only audit log with actor, role, IP and outcome (no document contents); redacted application logs.

## Tests
```powershell
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" -m pytest      # offline suite incl. 35 security tests
```
All tests use synthetic records, a fake LLM, a fake Supabase Auth server (`tests/fake_auth.py`) and
`httpx.MockTransport` for feeds. Opt-in live tests against the real Supabase project (synthetic accounts, deleted
afterwards): set `NYAYA_LIVE_SUPABASE=1`.

## Data sources
* Bare acts: India Code (indiacode.gov.in), Legislative Department, Ministry of Law & Justice. IPC (repealed
  1 July 2024, kept for pending matters): eCourts district court portal copy of the India Code text.
  Provenance and checksums: `data/library/bare_acts/manifest.json`.
* Sample judgments: Indian Kanoon.
* Draft templates: original model drafts written for this project — verify against current law and local practice.

*AI-generated research aid, not legal advice. Verify every citation against the primary source.*
