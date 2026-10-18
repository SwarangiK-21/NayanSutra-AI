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

## What it does

| Area | Features |
|---|---|
| Case files | Civil / Criminal / Family / Revenue matters, dossier, notes, hearing dates, per-matter documents |
| Documents | PDF / DOCX / TXT / MD upload → parsed, chunked and embedded **on this computer**; auto document-type detection |
| Legal research | Streaming answers with `[n]` citations, source drawer that opens the original PDF at the cited page, citation audit |
| Document analysis | Summary, timeline, issues, parties, arguments over a whole document (map-reduce for very long files) |
| Drafting | 22 model templates (bail, 138 NI notice/complaint, plaint, written statement, injunction, s.11 arbitration, divorce, 13B, maintenance, DV, custody, MLRC appeal/revision/mutation objection, vakalatnama, affidavit…) adapted to the case dossier |
| Bare acts | 29 official texts (Constitution, BNS, BNSS, BSA, IPC, CPC, Contract, Specific Relief, Limitation, NI, Arbitration, TPA, Registration, Stamp, HMA, SMA, HSA, HAMA, GWA, DV, POCSO, MLRC, …) with instant section lookup |
| Limitation | 24 deterministic limitation periods (Limitation Act Schedule, NI Act, Arbitration Act, HMA, MLRC, CPA) |

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
 db.py  SQLite: cases · documents · chunks · FTS5 · embeddings · chats      vectorindex.py (NumPy, exact cosine)
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
* Binds to `127.0.0.1`; refuses to listen on a network interface unless `APP_PASSWORD` is set.
* HttpOnly SameSite=Strict session cookie, CSRF header, Host allow-list (DNS-rebinding defence), Origin check.
* Uploads: extension allow-list, magic-byte check, size limit, server-generated storage names, zip-bomb guard,
  PDFium parser. Strict CSP; all UI text escaped; no third-party scripts or fonts.
* Rate limits on chat, upload and login. Generic error messages; secrets never reach the browser; `.env` is git-ignored.
* Per-matter isolation: a case's documents are only ever searched for that case.
* Prompt-injection hardening: document text is passed as data and every prompt instructs the model to ignore
  instructions inside sources.
* Only the question and selected passages are sent to the LLM (Ollama Cloud); documents, embeddings and chats stay local.

## Tests
```powershell
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" -m pytest      # offline, ~3 s
```

## Data sources
* Bare acts: India Code (indiacode.gov.in), Legislative Department, Ministry of Law & Justice. IPC (repealed
  1 July 2024, kept for pending matters): eCourts district court portal copy of the India Code text.
  Provenance and checksums: `data/library/bare_acts/manifest.json`.
* Sample judgments: Indian Kanoon.
* Draft templates: original model drafts written for this project — verify against current law and local practice.

*AI-generated research aid, not legal advice. Verify every citation against the primary source.*
