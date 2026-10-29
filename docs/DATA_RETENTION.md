# NyaySutra AI — Data Retention Policy

*Version 1 · 29 October 2026 · Applies to every NyaySutra installation unless its administrator changes the settings
below. Users can read the current periods in the app under **Privacy** (footer link, also before sign-in).*

## Why

NyaySutra holds advocates' research questions, client case files and account details. Keeping personal data only as
long as it is useful lowers the harm if anything ever goes wrong, and it is a duty under India's Digital Personal
Data Protection Act, 2023 and the DPDP Rules, 2025 (data must be erased once its purpose is served; most
obligations apply from May 2027).

## Retention periods

The defaults are the project owner's choice ("keep data longer"). Each is a setting in `.env`; `0` means keep until
deleted by a user.

| Data | Kept for | Counted from | Setting |
|---|---|---|---|
| Chats with the research assistant (questions and answers) | 24 months | last use of the chat | `RETENTION_CHAT_MONTHS` |
| Research (search) history | 24 months | the search | `RETENTION_SEARCH_MONTHS` |
| Account and personal workspace | 36 months | last sign-in or use | `RETENTION_INACTIVE_MONTHS` |
| — inactivity warning | at 33 months | last sign-in or use | `RETENTION_WARN_MONTHS` |
| Cases, case documents, drafts | until a user or administrator deletes them | — | — |
| Security audit log (who did what; never document contents) | 5 years | the event | `RETENTION_AUDIT_YEARS` |
| Sign-in sessions | 30 days | expiry | fixed |
| Notifications | 12 months once read, 24 months otherwise | the notification | fixed |
| Background job records | 90 days | the job's end | fixed |
| Integration sync log | 12 months | the sync | fixed |
| Sign-in throttle records | 30 days | the record | fixed |

## How deletion works

* **User deletions are immediate.** Deleting a chat, search, case or document in the app removes it at once and for
  good; there is no recycle bin.
* **Automatic clean-up** (`nyaya/retention.py`) runs once a day (first run 5 minutes after the app starts) and
  permanently deletes everything older than the periods above. Switch it off with `RETENTION_ENABLED=false`.
* **Inactive accounts.** After 33 months without use the user is warned in the app (and by e-mail when e-mail is
  configured). Using the app once — signing in counts — cancels the warning. After 36 months, and at least 30 days
  after the warning, the account is deleted with its personal data: chats, search history, calendar and reminders,
  preferences, notifications, sessions, recovery codes, memberships, and the sign-in record in Supabase Auth. Cases
  whose only member was that user are deleted with their documents; shared cases stay with their other members.
* **Audit log.** The application cannot edit or delete audit entries. Expired entries are removed only by a narrow
  database function that runs for the daily system job (`private.purge_audit_log`, migration 10).
* Every clean-up run that deletes something is itself recorded in the audit log (counts only, no contents).

## Where data is stored

* Database and files: a private Supabase project in Mumbai (ap-south-1); row-level security on every table.
* AI processing: questions and the passages used to answer them go to the configured AI model (Ollama Cloud);
  personal identifiers (Aadhaar, PAN, phone, e-mail, card and account numbers, passport) are replaced first.
* Web search: only search words are sent, with client and party names removed.

## Not yet covered (planned)

* A recovery bin (e.g. deleted items restorable for 90 days) — deletions are immediate today.
* Self-service "download my data" and "delete my account" buttons — today the administrator handles such requests.
* A breach-notification procedure (DPDP Rules: inform affected users and the Data Protection Board without delay).
