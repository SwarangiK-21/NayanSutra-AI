# NyaySutra AI — feature expansion plan (Phase 1 findings)

## Existing architecture (inspected 2026-10-22)
* **Backend**: FastAPI (`nyaya/server.py`), single-user local app on 127.0.0.1, optional `APP_PASSWORD`.
  Signed session cookie + CSRF header + Host/Origin checks (`nyaya/security.py`).
* **Database**: one SQLite file (`data/nyaysutra.db`): `cases`, `case_notes`, `documents`, `chunks` (+FTS5),
  `chats`, `messages`, `meta`. No migrations framework — tables created with `CREATE IF NOT EXISTS`.
* **AI**: Ollama Cloud (`gpt-oss:120b` answers, `gpt-oss:20b` routing/verification) via `nyaya/llm.py`;
  local fastembed embeddings + cross-encoder reranker. Agentic RAG in `nyaya/agent.py`.
* **Frontend**: vanilla JS single-page app (`web/app.js`), strict CSP (no inline script, no third-party origins).
* **Library**: 29 bare acts (India Code), 3 judgments, 22 draft templates; 9,090 indexed passages.
* **Tests**: 13 offline tests with fake LLM/embeddings.

## External sources — what actually exists (checked 2026-10-22)
| Source | Access found | Integration mode |
|---|---|---|
| LiveLaw | Official RSS `https://www.livelaw.in/google_feeds.xml` (advertised via `<link rel="alternate">`, WebSub hub listed) | **Scheduled polling** of the RSS feed (headlines + links only). WebSub push needs a public callback URL, which a 127.0.0.1 app does not have. |
| LatestLaws | No advertised RSS/API; robots.txt disallows `/api/` and feed paths | **Manual access only** (Open official source) |
| eCourts Services | Case status by CNR / party / FIR requires CAPTCHA; no public API | **Manual access** + import of details/orders the user downloads |
| Supreme Court of India | Case status behind CAPTCHA; no public API | **Manual access** + import |
| Bombay High Court | `bombayhighcourt.gov.in` case status behind CAPTCHA | **Manual access** + import |
| FIR search (Maharashtra Police citizen portal) | Portal requires CAPTCHA / login; no API; not reachable from this machine | **Manual access**; FIR identification needs state + district + police station + year + FIR no. |
| India Code | Public DSpace REST API (already used by `scripts/fetch_bare_acts.py`) | **Scheduled polling** for bare-act updates (checksum change detection) |

No source offers webhooks usable by a local app, so no webhook receiver is built (it would be dead code).
CAPTCHAs, logins and rate limits are never bypassed.

## Local capabilities
* OCR: **Tesseract is not installed**. OCR is implemented as an optional engine (Tesseract CLI with `eng`, `hin`,
  `mar` language data); without it, scanned files fail with a clear message instead of a silent guess.
* `.doc` (legacy Word): extracted with `antiword` when it is on PATH; otherwise rejected with a message.
* Hindi/Marathi PDF export: fpdf2 + uharfbuzz text shaping with the Windows *Nirmala UI* font (verified correct conjuncts).

## Feature → file map
| Module | Backend | Frontend / data |
|---|---|---|
| 1 Case information fetching | `nyaya/casesearch.py`, `nyaya/reference.py` | `web/js/casesearch.js` |
| 2 Sync & updates | `nyaya/integrations.py` (adapters, scheduler, retries, change detection), `notifications` table | `web/js/updates.js` |
| 3 Voice chat | Permissions-Policy `microphone=(self)`; `lang` in chat API | `web/js/voice.js` (Web Speech API, feature-detected) |
| 4 EN/HI toggle | `prefs` table, `/api/prefs` | `web/js/i18n.js`, `web/i18n/{en,hi,mr}.json` |
| 5 Multilingual documents | `nyaya/parsing.py` (images, OCR, `.doc`), `nyaya/langdetect.py`, `nyaya/docintel.py` | `web/js/docs.js` (document viewer) |
| 6, 9, 11 Drafting, notices, forums | `nyaya/drafting.py`, `nyaya/export.py`, `data/drafting/*.json` | `web/js/drafting.js` |
| 7 New case form | `nyaya/cases.py`, `nyaya/reference.py` | `web/app.js` (case form) |
| 8 Provision identification | `nyaya/provisions.py` | case page panel |
| 10 Legal awareness | `data/awareness/*.json`, Motor Vehicles Act added to the library | `web/js/awareness.js` |
| 12 Integrations | `nyaya/integrations.py` | `web/js/updates.js` (status page) |
| 13 Unified search | `nyaya/search.py` | `web/js/search.js` |
| 14 DB / security / audit | `nyaya/db.py` migrations, `audit_log` | — |
| 16 Tests | `tests/test_features.py` | — |

## Known gaps (by design or environment)
* Multi-user accounts / role-based access control: the app is a single-user local workspace; RBAC needs a user
  model and is out of scope for this iteration (documented, not faked).
* Encryption at rest: rely on OS disk encryption (BitLocker); SQLite file is not encrypted by the app.
