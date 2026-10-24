# Supabase migration — NyaySutra AI

Status (2026-10-24, 13:50): **migration complete and verified. The app now runs on Supabase Postgres + Storage.**
All data and files were copied and verified, the app was switched over, and every workflow was tested live
(see section 10). The local SQLite file and uploads are kept, unchanged, as the rollback copy.

## 1. Original storage architecture
A single-user FastAPI app; the browser talks only to the backend.

| Mechanism | Contents | Volume (2026-10-24) |
|---|---|---|
| SQLite `data/nyaysutra.db` | 17 tables + FTS5 keyword index: cases, notes, documents, 9,531 chunks with float32 embeddings, chats, messages, drafts (+versions), provisions, notifications, integrations + sync log, 209 news/source records, document derivatives, audit log, prefs, meta | 40 MB |
| `data/uploads/<case>/<doc>.<ext>` | client uploads | 2 files, 8 KB |
| `data/library/` | 30 bare acts, 3 judgments, 22 templates + manifests (public texts, in git) | 35 MB |
| `data/drafting/*.json`, `data/awareness/*.json` | application configuration | small |
| `~/.nyaysutra/` | ONNX models (cache), session signing key | ~100 MB |
| temp dirs | OCR page images, upload staging | transient |
| `.env` | API keys | — |
| browser `localStorage` | font size, contrast, language, last case/scope/state | per device |
| Auth | signed HttpOnly session cookie + CSRF; optional shared `APP_PASSWORD`; **no user accounts or roles** | — |

## 2. Classification and decisions
| Data | Decision | Why |
|---|---|---|
| All 17 tables | **Supabase Postgres** (`public`) | structured application data |
| Keyword index (FTS5) | Postgres generated `tsvector` column `chunks.fts` + GIN index | native Postgres full-text search |
| Embeddings | `bytea` (same float32 bytes) | the app keeps its exact in-memory search (tested, fast at this size); pgvector adds nothing until the corpus is far larger |
| Client uploads | **Supabase Storage**, private bucket `case-files` | files belong in object storage, not Postgres |
| Library PDFs/templates, JSON config | stay in the repository | public texts / configuration shipped with the code, re-fetchable from India Code |
| Generated Word/PDF exports | generated on request, not stored | always reflect the current draft version (versions are in Postgres) |
| Temp files, model cache, session key | stay local | transient or machine-specific; not application data |
| Secrets | environment variables (`.env`, git-ignored) | never in the database or browser |
| Browser `localStorage` | stays | per-device conveniences only |
| **Auth** | **unchanged for now — decision needed** | Supabase Auth only fits once the app has user accounts and roles (it has neither today); adding them changes how everyone signs in, so it is your decision (see section 11) |

## 3. New architecture
```
Browser ──HTTPS/cookie──► FastAPI backend ──psycopg pool (role nyaya_app, session pooler)──► Supabase Postgres (RLS on)
                                   └────────────── Storage REST API (secret key, server-side) ──► bucket case-files (private)
```
* No Supabase key, URL or signed link is ever sent to the browser; files are streamed through
  `/api/documents/{id}/file` after the app's own checks (strict CSP preserved).
* The backend uses the dedicated role **`nyaya_app`**: DML on the app tables only — no DDL, not superuser,
  no BYPASSRLS, cannot update or delete the audit log.
* SQLite remains as the **offline mode / rollback path** (used when `SUPABASE_DB_URL` is empty) and for the
  offline test suite. When Supabase is configured, nothing is read from or written to SQLite.

## 4. Schema, indexes, bucket (`supabase/migrations/`)
| File | Contents |
|---|---|
| `20261024000001_initial_schema.sql` | 17 tables mirroring SQLite schema v1: same columns, PKs, FKs (`ON DELETE CASCADE / SET NULL`), unique constraints; check constraints on `documents.collection/status`, `drafts.kind`, `case_provisions.status`; JSON → `jsonb`; identity ids "by default" (existing ids are kept); indexes: cases(cnr, updated_at, next_hearing), documents(case, collection, derived_from, unique (case, sha256)), chunks(doc+ord, section, GIN fts), chats, messages, notifications (incl. partial unread), sync_log, source_records, drafts; `meta.schema_version = 1` |
| `20261024000002_security_and_storage.sql` | role `nyaya_app` + grants; privileges revoked from `anon`/`authenticated`; RLS enabled on all 17 tables with one policy each `backend_all … TO nyaya_app`; audit log append-only; private bucket `case-files` (40 MB limit, allowed MIME types) |
| `20261024000003_lock_rls_helper.sql` | closes Data-API access to the project's `rls_auto_enable()` event-trigger function (advisor 0028/0029) |
| `20261024000004_index_notifications_case.sql` | index on `notifications(case_id)` (performance advisor 0001: unindexed foreign key) |

Timestamps stay ISO-8601 `text` (the code slices and compares them as strings). All three migrations were applied
to project `wjdiosxsgpcblzrenkes` on 2026-10-24; the Supabase security advisor reports **no findings**.

## 5. Code changes
| File | Change |
|---|---|
| `nyaya/db.py` | backend switch; Postgres connection pool, transactions (nested calls join), `?`→`%s` translation, sqlite-compatible rows, JSON as text, read-only retry on dropped connections, schema-version check (the app never runs DDL on Postgres) |
| `nyaya/storage.py` (new) | Storage client: upload (fixed keys, upsert, retries that resend the whole file), streamed download, temporary local copy for parsing, idempotent delete, signed URLs (server-side), health |
| `nyaya/server.py` | upload staged in a temp file → validated → stored → recorded (object removed if the record fails); downloads streamed from Storage; status shows database/storage backend; pool closed on shutdown |
| `nyaya/ingest.py` | parse from storage via temporary copy; metadata-first delete then file delete (failures logged as `storage.orphan`); portable SQL |
| `nyaya/retrieval.py`, `nyaya/search.py` | keyword search per engine (`tsvector`/`ts_rank_cd` vs FTS5/bm25), injection-proof token query |
| `nyaya/agent.py`, `integrations.py`, `provisions.py` | `RETURNING id`, `ON CONFLICT DO NOTHING`, `->>` JSON |
| `nyaya/cases.py` | never seeds demo data into Supabase; warns if Supabase is empty while local data exists |
| `nyaya/vectorindex.py` | Supabase mode: chunk metadata from Postgres, embeddings from a local cache (`~/.nyaysutra/vector_cache_<model>.npz`, atomic writes) with incremental download in batches of 400 for ids not yet cached (a single 15 MB query exceeded Supabase's 2-minute statement timeout on this network) |
| `nyaya/search.py`, `nyaya/awareness.py`, `nyaya/library.py` | removed an N+1 query in unified search; search no longer resolves statute text it doesn't show; found statute lookups cached per process (not-found never cached) |
| `tests/conftest.py` | the offline suite forces Supabase settings empty, so tests can never touch the real project |
| `nyaya/config.py`, `.env.example`, `requirements.txt`, `.gitignore` | Supabase settings, `psycopg[binary]` + `psycopg-pool`, report/backups ignored |
| `scripts/migrate_to_supabase.py` (new) | idempotent data + file copy with verification |
| `tests/test_supabase.py` (new) | offline layer tests + opt-in live test |

## 6. Owner actions (completed 2026-10-24 — repeat only for a new project)
1. **Set a password for the app role** — Supabase dashboard → SQL Editor:
   `alter role nyaya_app with password '<a long random password>';`
2. **Connection string** — dashboard → *Connect* → *Session pooler*: copy the URI, then replace the user
   `postgres.wjdiosxsgpcblzrenkes` with `nyaya_app.wjdiosxsgpcblzrenkes` and the password with the one from step 1.
3. **Secret key** — dashboard → *Settings → API Keys* → secret key (`sb_secret_…`).
4. Put them in `.env` (already prepared, `SUPABASE_URL` is filled in):
   `SUPABASE_DB_URL=…` and `SUPABASE_SECRET_KEY=…` — **do not paste them in chat or commit them**.
5. Do **not** restart the app before step 7.

## 7. Migrating the data
```powershell
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" scripts\migrate_to_supabase.py --dry-run
& "$env:USERPROFILE\.nyaysutra\venv\Scripts\python.exe" scripts\migrate_to_supabase.py
```
Done on 2026-10-24 (see section 10). To repeat (e.g. for a new region): copies tables parent-first in batches of 500 with `ON CONFLICT DO NOTHING` (re-runnable), uploads each client
file and checks its SHA-256 **before** writing its row, advances identity sequences, then verifies: row counts per
table, 200 random chunks (text + embedding bytes), every case/document/draft/message field-by-field, orphan
chunks, documents still pointing at local files. Report: `data/migration_report.json`. Exit code 0 only if
everything matched. Then restart the app; the masthead/About page shows "Supabase Postgres" and "Supabase Storage".

## 8. Environment variables
`SUPABASE_DB_URL`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY` (or `SUPABASE_SERVICE_ROLE_KEY`), optional
`SUPABASE_BUCKET` (default `case-files`), `DB_POOL_MAX` (default 8; the free-tier pooler allows 30 per role/mode).

## 9. Security
* RLS on every table; the only policies are for `nyaya_app`; `anon`/`authenticated` have no table privileges,
  so the auto-generated Data API exposes nothing. No table or bucket is public.
* Storage: bucket private, no `storage.objects` policies for client roles; secret key used only by the backend.
* Keyword input is reduced to `[a-z0-9]` tokens before `to_tsquery`; all other SQL is parameterised.
* Upload validation unchanged (allow-list, magic bytes, 40 MB) plus the bucket's own size/MIME limits.
* Matter isolation and the audit log are enforced in the backend as before.

## 10. Tests and results (executed 2026-10-24)
* **Offline suite: 50 passed, 1 skipped** (the opt-in live test) — the original 42 + 8 new: SQL translation, rows,
  engine-specific keyword syntax and injection, migration coverage of every table, Storage client against a
  **mock** (retry resends the whole file, headers, streaming, signed URL, idempotent delete, MIME rejection, network
  failure), upload→Storage→download, rollback of orphaned objects, storage-outage path.
* **Live test against the real project: 9 passed** (`NYAYA_LIVE_SUPABASE=1 pytest tests/test_supabase.py`) — schema
  check, case create/read/update/delete as `nyaya_app`, file upload + hash-checked download, anonymous access to
  the private bucket refused, synthetic data removed.
* **Connection checks:** `nyaya_app` logs in; DDL refused (`InsufficientPrivilege`); cannot bypass RLS.
* **Data migration:** dry run clean; real run inserted 6 cases, 5 notes, 57 documents, 9,531 chunks, 14 chats,
  32 messages, 2 drafts / 3 versions, 212 source records, 28 audit entries, 10 notifications, sync state and prefs;
  **verification OK** — counts equal for all 17 tables, 200 random chunks identical (text + embedding bytes), every
  case/document/draft/message identical field by field, 0 orphans, both client files uploaded and hash-verified,
  0 case documents left on local paths. **Second run inserted 0 rows and uploaded 0 files (idempotent).**
* **Application on Supabase (browser + API):** dashboard, cases, notifications, drafts, news (LiveLaw) and settings
  read from Postgres; client file streamed from private Storage with the sandbox CSP; exact section lookup and
  keyword search; a synthetic upload → stored in Storage → processed → found by search → duplicate detected → fake
  PDF rejected (415); app restart → data and the new passage still present (index 9,532); case deletion removed
  the row, its passages and the stored object; a research question returned NI Act s.138/141/142 sources and the
  chat was saved in Postgres (test chat deleted afterwards).
* **Supabase advisors:** security — no findings; performance — the one real item (unindexed FK) fixed; three
  "unused index" notes are expected on a one-hour-old database.
* **Network finding:** Postgres executes the keyword query in ~1 ms, but throughput from this computer to the
  Seoul pooler measured **5–23 KB/s** (session and transaction mode) versus ~1.6 MB/s to a CDN and ~460 KB/s to the
  same project's Storage over HTTPS. Pages that move a lot of text (search ~5–9 s, first statute lookups) are slow
  for this reason; the database itself is not. A region closer to the users (Mumbai) is the fix — see section 11.
* **Not tested:** OCR on Supabase (Tesseract still not installed), multi-user concurrency, files larger than a few
  KB through the app (the 1 MB Storage speed test passed separately).

## 11. Remaining decisions / limitations
* **Authentication & RBAC:** recommended next step is Supabase Auth (email + MFA) with roles and case membership;
  RLS policies would then move from "backend role" to ownership/membership rules. Needs your decisions on roles
  and who may sign up.
* **Region (recommended):** the project is in `ap-northeast-2` (Seoul). Both for speed (section 10) and for client
  data under Indian confidentiality / DPDP expectations, create the production project in `ap-south-1` (Mumbai).
  Moving is cheap now: apply `supabase/migrations/*`, set the new URL/keys in `.env`, run the migration script
  (it is idempotent and reads only the local copy), delete the vector cache file if the model changed.
* Library files stay in git (not Storage) by design.

## 11a. Region move: Seoul -> Mumbai (done 2026-10-24)
* **The app now uses project `asjypvftqzxhdomjefzx` ("NyaySutra-AI Mumbai", `ap-south-1`)** — created at $0/month
  (free plan), all four migrations applied, security advisor: no findings.
* Moved with `scripts/move_supabase_project.py --switch`: pass 1 bulk-loaded the local verified copy, pass 2
  reconciled with Seoul as the source of truth (brought over 10 audit entries, 1 notification, 1 sync-log entry and
  2 news records written after the first migration). **Verification: identical** row sets in all 17 tables,
  identical content checksums (cases, documents, drafts, messages, notifications, integrations, source records),
  100-chunk sample identical (text + embeddings), both files identical (SHA-256). `.env` switched; the Seoul values
  are kept as `OLD_SUPABASE_*`.
* **Speed, same tests from this computer:**

  | | Seoul | Mumbai |
  |---|---|---|
  | DB round trip | 162 ms | 12 ms |
  | 200 KB download | 11–13 s (18 KB/s) | 0.2–0.4 s (1,054 KB/s) |
  | Unified search | 5.2–8.7 s | 1.3 s |
  | Section lookup | 1.9–3.0 s | 0.6 s |
  | Dashboard / library | 0.97 / 1.5 s | 0.61 / 0.66 s |
  | Upload → searchable | 14 s | 3.6 s |
  | Research answer (incl. LLM) | 16 s | 9 s |
* After the move: live tests 9/9 on Mumbai, offline 50 passed; upload/search/delete and research verified in the
  app. **Owner decision pending:** delete or pause the Seoul project (`wjdiosxsgpcblzrenkes`) so client data is not
  kept in two places; until then it is an untouched snapshot and `OLD_SUPABASE_*` lets you roll back.

## 12. Rollback
Set `SUPABASE_DB_URL=` (empty) and `SUPABASE_SECRET_KEY=` (empty) in `.env` and restart: the app uses the untouched
local SQLite file and `data/uploads/` again. A verified copy taken before migration is in
`data/backups/pre-supabase-20261024-124206/`. To clear Supabase and re-run: delete rows from the tables (children
first) and the objects in `case-files`, then run the script again.

## 13. Local storage that remains
Library texts and JSON configuration (shipped with the code), model cache and session key (`~/.nyaysutra`), temp
files during upload/OCR, browser `localStorage` preferences, and the SQLite file + `data/uploads` kept as the
rollback copy (delete them only after you are satisfied with Supabase).

## 14. Checklist
| Stage | Status |
|---|---|
| Audit and plan | ✅ done |
| Postgres schema + indexes (version-controlled, applied) | ✅ done |
| RLS, least-privilege role, private bucket, advisor clean | ✅ done |
| Backend database layer + Storage client | ✅ done, tested offline |
| All modules ported (no SQLite-only SQL left outside the fallback) | ✅ done |
| Idempotent migration script with verification | ✅ done, run twice (second run: 0 inserts) |
| Backups before migration | ✅ done |
| Data + file migration, verification | ✅ done — all checks OK |
| Switch-over (app on Supabase) and live tests | ✅ done |
| Restart without data loss | ✅ verified |
| Advisors (security / performance) | ✅ security clean, FK index added |
| Retire local SQLite / uploads | ⏸ kept on purpose as the rollback copy — delete only when you are satisfied |
| Region move to Mumbai | ✅ done — Seoul project kept until you delete/pause it |
| Supabase Auth / RBAC | ❓ needs your decision |
