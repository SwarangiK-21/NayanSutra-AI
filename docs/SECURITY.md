# NyaySutra AI — Security foundation

What was built in stages C–G (audit and risk register: [SECURITY_AUDIT.md](SECURITY_AUDIT.md)), what is reused from
Supabase, how it was tested, and what is still open. Nothing below claims more than the code and tests show.

## 1. Architecture in one picture

```
Browser ── HttpOnly session cookie + CSRF header ──► FastAPI backend (the only thing the browser talks to)
                                                     │  principal = session → user, organisation, role, MFA level
                                                     │  authz.py: permission matrix + case access, checked on every endpoint
                                                     │  guardrails.py: request screening, tool allow-list, source delimiting,
                                                     │                 citation verification, output filter
                                                     ├──► Supabase Auth (GoTrue)  sign-in, e-mail links, TOTP MFA, refresh, sign-out
                                                     ├──► Supabase Postgres as nyaya_app, with request.jwt.claims = the caller
                                                     │      → row-level security policies using auth.uid()
                                                     ├──► Supabase Vault        the session's Supabase tokens (encrypted at rest)
                                                     ├──► Supabase Storage      private bucket, server-side key only
                                                     └──► Ollama Cloud (LLM)    prompts with identity numbers masked
```

The browser never receives a Supabase key or token. Without Supabase settings the app runs in **local single-user
mode** (loopback only, the person at the computer is the administrator) — kept for offline use.

## 2. Roles and permissions (`nyaya/authz.py`)

Two personas: **Lawyer** (`advocate`) and **Administrator** (`admin`). Anyone may register (`SIGNUP_MODE=open`,
the default) and becomes a Lawyer after verifying their e-mail; only an administrator can make someone an
administrator (invitation or role change). A user cannot choose or change their own role. `SIGNUP_MODE=invite_only`
restores invitation-only access. The former Client and Security-auditor roles were retired in migration 07 (their
accounts became Lawyers — what public sign-up gives anyone — and per-case client access was removed).

| Permission | Guest | Lawyer | Administrator |
|---|:-:|:-:|:-:|
| Public library: bare acts, India Code catalogue, awareness, limitation, news | ✓ | ✓ | ✓ |
| Own account, sessions, MFA, notifications, own calendar and research history | | ✓ | ✓ |
| AI legal research (library + cases they can access), choose jurisdiction | | ✓ | ✓ |
| Create cases, drafting, case search / record import, add an India Code act to the library | | ✓ | ✓ |
| See the organisation's case list (metadata of cases they are not on) | | | ✓ |
| Manage users, roles, invitations, organisation settings, integrations | | | ✓ |
| Read audit log (actions only, never document contents) | | | ✓ |

Case access (per matter, `case_members`):

| Action on a case | Owner | Lawyer on the case | Admin not on the case |
|---|:-:|:-:|:-:|
| See case details and documents, upload, AI on documents, chat, notes, drafts, provisions | ✓ | ✓ | metadata only |
| Manage people on the case | ✓ | | ✓ |
| Delete case | ✓ | | ✓ |

Calendar entries, their reminders and search history are private to their owner — administrators included — in the
API and in row-level security; an entry can only be linked to a matter its owner can open.

An administrator who needs to work on a case adds themselves; that is recorded as `case.member_add` with
`self_assigned: true`. Organisations (tenants) are isolated: a case's organisation must match the caller's, even if a
stray membership row existed.

## 3. What Supabase does and what the app adds

| Requirement | Reused from Supabase | Implemented in the app |
|---|---|---|
| Password storage | Auth: bcrypt hashing | Password rules before Supabase (≥12 chars, ≤72 bytes for bcrypt, common-password list, not the e-mail name) |
| E-mail verification | Auth: confirmation e-mail + single-use link (PKCE) | Refuses sign-in with an unconfirmed address even if confirmation is switched off; invitation applied only to verified e-mail |
| Password reset | Auth: single-use, expiring recovery link (PKCE, must be opened in the same browser) | Reset session can only set a password; afterwards every session is signed out (`scope=global`) |
| MFA | Auth: TOTP enrol / challenge / verify, AAL1→AAL2 | MFA required for admin and lawyer (`MFA_REQUIRED_ROLES`); 10 recovery codes (keyed HMAC-SHA256, single use — Supabase has none); fresh TOTP needed to disable MFA or regenerate codes; 5 wrong codes end the sign-in attempt |
| Sessions | Auth: rotating refresh tokens, sign-out scopes, bans | Server-side sessions (only SHA-256 of the cookie stored), 12 h absolute + 60 min idle expiry, token rotation on every privilege change, list/revoke own sessions, revalidation with Supabase each access-token lifetime (revocation or ban at Supabase ends the app session) |
| Token storage | Vault (encrypted at rest) | SECURITY DEFINER wrappers usable only in the system context |
| Brute force | Auth: per-IP rate limits | Account lockout in Postgres (5 failures / 15 min per e-mail, across IPs and instances); per-IP limits on sign-in, sign-up, reset, MFA, chat, upload, export |
| Authorization in the database | Postgres RLS + `auth.uid()` / `auth.jwt()` | Membership-based policies on all 27 tables; backend passes the caller as `request.jwt.claims` |
| Security e-mails | Auth security notifications (enable in dashboard) | In-app security notifications: new device, MFA on/off, recovery code used, password changed/reset, role changed |
| Encryption at rest / TLS | Supabase-managed disk encryption, TLS endpoints | `sslmode=require` enforced for Postgres; Secure cookies + HSTS when `PUBLIC_URL` is https |

## 4. Database enforcement (`supabase/migrations/20261024000005_identity_and_access.sql`)

* Every statement runs with the caller's identity (`db.Identity` → `request.jwt.claims`, set on the pooled
  connection before any transaction). Default identity is anonymous: forgotten context sees only public data.
* Policies: members see their cases; documents/chunks/derived text follow the case; chats are personal; drafts belong to
  the author or the case team; notifications and preferences are per user; memberships can be changed only by an
  administrator and never on their own row; the audit log is insert-only (UPDATE/DELETE revoked) and readable by
  the organisation's administrators; calendar entries, reminders and search history are visible only to their owner.
* Sessions, recovery codes, throttle counters and Vault tokens are reachable only in the explicit system context used by
  sign-in, ingestion, sync and retention code (`db.system()`).
* **Limit:** the system context is a claim set by backend code on a shared role. RLS therefore protects against
  missing checks in application code, not against an attacker who can run arbitrary SQL as `nyaya_app`. SQL injection
  is prevented by parameterised queries everywhere.

## 5. AI guardrails (`nyaya/guardrails.py`, `nyaya/privacy.py`)

| Threat | Control (deterministic code) |
|---|---|
| Hidden instructions (zero-width, bidi, Unicode tag characters) | `normalise()` before anything else |
| Harmful requests (forging documents/evidence, tampering, witness/judge intimidation or bribery, absconding, false complaints, hacking, violence) | `screen_request()` — refused before any model call, with lawful alternatives; questions about the law, rights and defences pass (tested both ways, English and Hindi) |
| Direct prompt injection, system-prompt/secret extraction | `screen_request()` refuses override and extraction attempts; no secret is ever placed in a prompt |
| Indirect prompt injection in documents | Detection at ingestion (`instruction_like_text`, shown to users) and on every retrieved passage (`injection_suspected`); each source wrapped in `<source>` delimiters the text cannot close; flagged passages carry a warning for the model |
| Unauthorised tool use / cross-user data | The planner may only choose an allow-listed tool with schema-validated arguments; the case, documents and chat are authorised by the server before the agent runs and never come from model output; a tool the caller may not use is downgraded to research |
| Fabricated citations | Every section/article/order reference and case citation in an answer must appear in the retrieved sources (or the user's question / case dossier); otherwise flagged and the answer marked not grounded — in addition to the model-based audit |
| Leakage in output | `scan_output()` removes configured secrets, key/JWT/database-URL patterns and verbatim system-prompt text before the answer is stored; the UI replaces the streamed text |
| Personal data sent to the LLM | Aadhaar (Verhoeff-checked), PAN, phone, e-mail, UPI, card (Luhn), bank account and passport numbers replaced by placeholders at the single LLM boundary and restored in the reply |

## 6. Data and providers

| Provider | Receives | Notes |
|---|---|---|
| Supabase (Mumbai, ap-south-1) | All stored data: accounts, cases, documents, passages, embeddings, chats, audit log, files | Free plan. Backups on the free plan are not downloadable — schedule your own `pg_dump` + storage export before relying on it |
| Ollama Cloud | Questions, selected passages, and (after consent) whole-document text, with identity numbers masked; names and facts are sent | This project has **not verified** Ollama's retention or training terms. Treat anything sent as disclosed to a third party; for the most sensitive matters use a local Ollama server (`OLLAMA_HOST`) |
| Browser speech service (Chrome/Edge) | Audio, only when the user uses voice input | Provided by the browser, not by NyaySutra |
| LiveLaw RSS, India Code | Nothing about users (outbound fetches only) | |

Retention: research chats are purged after the organisation's retention period (administrator setting). The audit
log is append-only; its retention is an operator decision. Logs carry ids, not file names, and pass through a filter
that removes secrets, tokens and e-mail addresses.

## 7. Setup and operations

1. **First administrator** (operator, once): `python scripts/bootstrap_admin.py --email you@example.org`, then sign in and
   set up the authenticator. This account takes ownership of matters created before accounts existed.
2. **Supabase dashboard** (Authentication):
   * URL Configuration → Site URL `http://127.0.0.1:8000` (or your `PUBLIC_URL`); Redirect URLs: add
     `http://127.0.0.1:8000/auth/callback**`. Without this, sign-up confirmation and password-reset links cannot return
     to the app.
   * Emails → set up custom SMTP before real use (the built-in sender allows only a few e-mails per hour).
   * Emails → Security notifications: enable "password changed" and "MFA method added/removed".
   * Sign In / Providers → Email: keep "Confirm email" on; set minimum password length 12.
3. **Inviting people**: Administration → Invite someone (e-mail + role) → send them the sign-up link.
4. **Leaving staff**: Administration → Disable (signs them out everywhere and bans the Supabase account).
5. **Key rotation**: rotate the Supabase secret key in the dashboard and update `.env`; set `APP_SECRET_KEY` when running
   more than one instance (changing it invalidates recovery codes and CSRF values — regenerate codes afterwards).

## 8. Test evidence

* `tests/test_security.py` — 35 offline tests (synthetic accounts, fake Supabase Auth server `tests/fake_auth.py`):
  guest limits; identical errors for wrong password vs unknown account; unverified e-mail; roles only from invitations;
  full permission matrix; no self-escalation; role change / disable effective immediately; 26 object operations refused
  to another user (IDOR); lists and search never cross users; client sharing; admin metadata-only + audited
  self-assignment; tenant isolation incl. a stray membership row; private notifications and jobs; cookie flags,
  logout, revocation, replay; idle and absolute expiry; Supabase-side revocation; CSRF/Origin incl. login CSRF and stale
  cookies; account lockout across IPs; rate limits; password policy; e-mail link single use; reset link single use,
  same-browser only, signs out everywhere; MFA enrolment, challenge, rotation, recovery codes (hashed, single use),
  MFA brute force; password change; audit actor/IP/outcome without contents; log redaction and generic 500s; malicious
  PDF/Word uploads; harmful/extraction requests refused without a model call; injection flagged and delimited; secret
  and fabricated-citation filtering; planner cannot pick unlisted tools; identifiers masked before the LLM.
* `tests/test_formats.py`: every accepted format (PDF scans via OCR, Word, Excel, CSV, PowerPoint, OpenDocument, RTF,
  images); refusal of macros, embedded objects and external links in Excel / PowerPoint / OpenDocument / RTF, and
  of renamed or binary files (content signature checked, not the file name). Office files are always downloaded,
  never rendered by the app's origin.
* `tests/test_live_security.py` (opt-in, real Supabase project, synthetic accounts deleted afterwards): RLS isolation
  with real claims (other user, anonymous, self-escalation, forged audit actor, append-only audit, Vault access) and the
  real Supabase Auth flow (password sign-in, TOTP enrolment and challenge to AAL2, tokens in Vault, sign-out).
  It found one real defect (Supabase returns the QR code as SVG markup, not a data URI), which was fixed.
* All earlier tests still pass (regression). Supabase security advisor: no findings after the migration.

## 9. Remaining risks and incomplete items

| Item | Status / recommendation |
|---|---|
| CAPTCHA | Not implemented. Account lockout and rate limits cover brute force; enable Supabase CAPTCHA (Turnstile) and add the widget if the app is exposed publicly |
| Sign-up confirmation and reset e-mails | Code paths tested against the fake Auth server; not exercised live (they send real e-mail and need the redirect-URL setting above) |
| Multi-instance deployment | Per-IP limits and the 15 s session cache are per process; use a shared limiter and set `APP_SECRET_KEY`. Supabase Auth's own per-IP limits see the server's IP for every user |
| Shared database role | See §4: RLS does not stop SQL run directly as `nyaya_app` |
| Output filter timing | Streamed tokens reach the browser before the final filtered text replaces them; secrets are never in the model's context, so this is defence in depth |
| PII minimisation | Structured identifiers only; names, addresses and facts still reach Ollama Cloud (provider terms unverified) |
| Malware scanning | Active-content checks always run; ClamAV only when installed (it is not installed now) |
| Harmful-request policy | Pattern-based (English + Hindi): paraphrases can slip through (the model's own safety and prompts remain); Marathi not covered |
| Case-law citation check | Heuristic on party names and reporter citations |
| Backups / audit retention | Not automated; see §6 |
| Account self-service deletion and data export | Not implemented |
| Old Seoul project | Still holds a copy of the data under the old schema; delete or pause it (owner decision) |
| Legacy migration scripts | `scripts/migrate_to_supabase.py` and `move_supabase_project.py` were written for schema v1 |
| Performance advisor | "Multiple permissive policies" (system policy + user policy per table) by design; the system check is evaluated once per query |
