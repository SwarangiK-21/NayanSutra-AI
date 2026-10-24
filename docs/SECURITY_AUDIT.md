# NyaySutra AI — Security audit and risk register

Stages A (audit) and B (risk register) of the security foundation work. Written **before** any security change
was made, against commit `7e553e1` (tag `pre-security-baseline`); work continues on branch `security/foundation`.
How each risk was addressed, the tests and what remains open: [SECURITY.md](SECURITY.md).

| Risk | Status after the work |
|---|---|
| R1–R4, R6–R8, R16, R20 | Addressed — accounts, roles, case membership, object checks, private jobs and notifications, attributed audit log (SECURITY.md §2–3, tests) |
| R5 | Addressed — membership-based RLS on all tables; limit of a shared database role documented (§4) |
| R9–R13 | Addressed in code; pattern-based limits documented (§5, §9) |
| R14 | Addressed (active-content checks); ClamAV only when installed |
| R15, R18, R19 | Addressed (redacting log filter, ids in logs, `sslmode=require`, Secure cookies/HSTS with `PUBLIC_URL`) |
| R17 | Addressed for accounts (DB lockout); per-IP limiter still per process (§9) |

## A. What was inspected

| Area | Files / systems |
|---|---|
| HTTP layer, sessions, CSRF, headers | `nyaya/server.py`, `nyaya/security.py`, `run.py` |
| Every API endpoint (≈75) | `nyaya/server.py` |
| Data access in every module | `cases.py`, `agent.py`, `drafting.py`, `provisions.py`, `docintel.py`, `casesearch.py`, `search.py`, `integrations.py`, `ingest.py`, `jobs.py`, `retrieval.py`, `vectorindex.py`, `library.py` |
| AI pipeline | `agent.py`, `prompts.py`, `llm.py`, `docintel.py`, `drafting.py`, `provisions.py` |
| Files | `storage.py`, upload path in `server.py`, `parsing.py`, `ocr.py` |
| Database | `nyaya/db.py`, `supabase/migrations/*.sql`, live Supabase project `asjypvftqzxhdomjefzx` (Mumbai) |
| Logging | every `log.*` call |
| Frontend | `web/js/core.js`, `web/app.js`, `web/index.html` (session handling, rendering) |
| Tests | `tests/*.py` (50 offline tests, 9 live) |

### Supabase configuration found (live, queried 2026-10-24)

* Postgres 17, TLS on. Extensions: `pgcrypto`, `uuid-ossp`, `pg_stat_statements`, `supabase_vault`.
* 17 application tables, **RLS enabled on all**, each with one policy `backend_all FOR ALL TO nyaya_app USING (true)`.
* `anon` / `authenticated` have **no** table privileges (the Data API cannot read application data).
* Backend role `nyaya_app`: not superuser, no `BYPASSRLS`, no usage on schema `auth`; `audit_log` UPDATE/DELETE revoked.
* Private bucket `case-files` (40 MB, MIME allow-list); only the server-side secret key reaches it.
* **Supabase Auth: not used.** 0 users, 0 MFA factors, 0 sessions. TOTP MFA is enabled by default on every project.
* Security advisors: no findings.
* `auth.uid()` / `auth.jwt()` read the `request.jwt.claims` setting (standard PostgREST contract).
* Vault (`vault.create_secret`, `vault.decrypted_secrets`) is available to the `postgres` owner role.

### Controls that already exist (kept)

* Strict CSP (no inline or third-party script), `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, COOP,
  `Permissions-Policy`; `no-store` on API responses.
* Host allow-list (DNS rebinding), Origin check, CSRF header bound to the session (HMAC), SameSite=Strict HttpOnly cookie.
* Upload hardening: extension allow-list, magic-byte check, size limit, server-generated storage names, staging before
  storage, compensating delete, duplicate detection; PDFium parser; zip-bomb guard for DOCX.
* Non-PDF originals served with a `sandbox` CSP; PDFs only frameable by this origin.
* Generic error messages; secrets never sent to the browser; the browser never talks to Supabase.
* Per-matter isolation inside retrieval (vector, keyword and exact lookups all filter case documents by `case_id`).
* Consent prompt before whole-document AI tasks; chat-retention purge; append-only audit log.
* Prompts tell the model that sources are data; citation audit (out-of-range numbers + LLM support check);
  limitation deadlines computed by deterministic code, not the LLM.
* Parameterised SQL everywhere (no string-built SQL with user input; dynamic column lists come from code constants).

## B. Risk register (prioritised)

Severity: **Critical** = direct confidential-data exposure across users; **High** = exposure or privilege gain under
realistic conditions; **Medium** = defence-in-depth gap or limited exposure; **Low** = hardening.

| ID | Vulnerability | Severity | Impact | Component | Remediation |
|---|---|---|---|---|---|
| R1 | No user identity. Any browser that obtains a session (or knows the shared `APP_PASSWORD`) can read and change every case, document, chat, draft, setting and the audit log. No roles exist. | Critical | Complete disclosure of privileged client material; no accountability | `server.py`, `security.py` | Per-user accounts on Supabase Auth; central role/permission matrix; deny by default |
| R2 | Insecure direct object references: documents, files, extracted text, chunks, chats, drafts, versions, exports, provisions, source records, jobs are fetched by id with no ownership check | Critical | Any user reads any matter by guessing or seeing an id | `server.py` object endpoints | Object-level checks through the case → membership chain on every id |
| R3 | Listing/search endpoints return everything: `/api/documents` (no case filter returns all case files), `/api/search`, `/api/dashboard`, `/api/drafts`, `/api/chats`, case-record matching | Critical | Cross-matter disclosure through search and lists | `server.py`, `search.py`, `casesearch.py`, `drafting.py` | Filter every list by the caller's accessible cases / own items |
| R4 | Background job results (translation, summary, draft, provisions) readable by anyone holding the job id; jobs run with no identity | High | Cross-user leakage of AI output about a document | `jobs.py`, `/api/jobs/{id}` | Bind each job to its owner; run it under the owner's identity |
| R5 | Database policies `USING (true)` — no tenant/owner isolation in the database; one missing `WHERE` in code leaks everything | High | Authorization bugs become data breaches | Supabase RLS | Ownership columns + RLS policies based on the authenticated user (via `auth.uid()` claims), system context only for internal workers |
| R6 | Shared-password login: one secret for everyone, compared with plain SHA-256, no lockout beyond 5/min/IP, no MFA, no reset; stateless 12 h tokens cannot be revoked (logout only clears the cookie, a copied cookie stays valid) | High | Credential sharing, brute force, stolen-cookie replay | `security.py`, `/api/login` | Supabase Auth (bcrypt, email verification, reset, TOTP MFA) behind the backend; server-side revocable sessions with idle + absolute expiry and rotation |
| R7 | Privileged operations open to any session: integrations enable/sync, retention setting, forum requirement edits, library-wide data, audit log | High | Privilege escalation, tampering with shared settings | `server.py` | Admin / auditor permissions |
| R8 | Audit log has no actor, IP or outcome; any session can read it | High | No accountability; audit data exposure | `db.audit`, `/api/audit` | Actor, role, organisation, IP, outcome; readable only by admins and auditors; details redacted |
| R9 | Indirect prompt injection defended only by prompt wording; no detection of instruction-like text in uploads or retrieved passages; no delimiter hardening | Medium | Manipulated answers, misleading drafts | `agent.py`, `prompts.py`, `docintel.py`, `drafting.py` | Deterministic injection detection, flagging, spotlighted delimiters |
| R10 | LLM planner output selects the branch and parameters; validated only partly (intent list, limitation key) | Medium | Unexpected tool use if the model is manipulated | `agent.plan` | Allow-listed tools with schema validation; identity and scope never taken from model output |
| R11 | Output checks: fabricated statutory citations not detected deterministically; no scan for leaked secrets or system prompts in model output | Medium | Fabricated law presented as sourced; leakage | `agent.py` | Deterministic citation verification against source text; output secret/system-prompt filter |
| R12 | Harmful-request handling left to the model | Medium | Assistance with evidence tampering, forgery, witness intimidation | `agent.py` | Deterministic policy for clearly harmful intents that leaves legitimate defence and rights education untouched |
| R13 | Identifiers (Aadhaar, PAN, phone, e-mail, bank account, card numbers) sent to the external LLM (Ollama Cloud) unredacted | Medium | Unnecessary third-party disclosure of personal data | `llm.py` | Reversible pseudonymisation at the single LLM boundary |
| R14 | Malicious files: no detection of PDF JavaScript / launch actions / embedded files, DOCX macros or external templates; no malware scan | Medium | Weaponised file stored and served back to other users | upload path, `parsing.py` | Active-content checks; optional ClamAV scan when installed |
| R15 | Logs contain uploaded file names (often client names); no redaction of secrets in log output | Medium | Personal data / secrets in log files | `ingest.py`, logging setup | Log ids instead of names; redacting log filter for secrets, tokens, e-mails |
| R16 | Preferences are global: one user's language, voice consent and drafting style apply to everyone | Medium | Consent recorded for the wrong person | `prefs` | Per-user preferences; organisation settings for admins |
| R17 | Rate limiting is in-memory per process and per IP only | Medium | Brute force across IPs; limits reset on restart | `security.RateLimiter` | Account-level lockout stored in the database; per-IP limits kept |
| R18 | Postgres client does not require TLS explicitly (libpq default `prefer`) | Medium | Downgrade on a hostile network | `db._get_pool` | `sslmode=require` unless the URL says otherwise |
| R19 | Session cookie lacks `Secure`; no HSTS — fine on 127.0.0.1, unsafe behind a public HTTPS proxy | Low | Cookie over plain HTTP if misdeployed | `server.py` | `Secure` + HSTS when `PUBLIC_URL` is https |
| R20 | Notifications are global (no recipient) | Low | Users see notices about other users' matters | `db.notify` | Per-recipient notifications (case members / admins) |

### Not vulnerabilities (checked)

* SQL injection: all user values are bound parameters; dynamic SQL uses code-defined column names only.
* XSS: frontend escapes every interpolated value; Markdown renderer escapes input before formatting; CSP forbids inline script.
* Path traversal: stored names are server-generated; local file serving is confined to the app folders.
* Supabase Data API exposure: closed (no grants for `anon`/`authenticated`).

## Design decisions (presented before the architectural change)

1. **Identity = Supabase Auth, used server-side (backend-for-frontend).** The browser still talks only to the FastAPI
   backend and holds an opaque, HttpOnly session cookie. The backend calls Supabase Auth for sign-up, e-mail
   verification, password sign-in, password reset (single-use, expiring links), TOTP MFA enrolment/challenge/verify,
   token refresh and sign-out. Supabase stores and hashes passwords (bcrypt) — no password hashing is re-implemented.
   Supabase access/refresh tokens never reach the browser; refresh tokens are kept in **Supabase Vault**.
2. **What the backend adds** (not provided by Supabase Auth in this architecture): server-side revocable sessions with
   idle and absolute expiry and rotation, CSRF, account lockout, stricter password policy, recovery codes for MFA (Supabase has none),
   roles/permissions, case membership, invitations (no self-assigned roles), security notifications in the app,
   session listing and revocation.
3. **Tenancy**: organisation (firm) → membership with one role → case membership with an access level. Roles: Guest,
   Client, Advocate, Administrator, Security Auditor. Administrators manage users and settings but do not read case
   contents unless they are on the case (adding themselves is audited).
4. **Database enforcement**: the backend passes the signed-in user to Postgres the way PostgREST does
   (`request.jwt.claims`), so policies use Supabase's own `auth.uid()`. Policies are membership-based and replace
   `USING (true)`. Internal workers (ingestion, scheduler, retention) run in an explicit, audited system context.
5. **AI guardrails** are deterministic code around the model (normalisation, harmful-intent policy, injection
   detection, tool allow-list with schemas, citation verification, output secret filter, PII pseudonymisation) —
   no second LLM decides what is authorised.
6. **Local single-user mode is preserved** (no Supabase Auth configured; loopback only) for offline use, with the
   same session store and authorization code path (the local operator is the organisation's administrator and
   owner of local matters).
