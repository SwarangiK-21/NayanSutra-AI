-- NyaySutra AI — access control and file storage.
--
-- Architecture: the browser talks ONLY to the NyaySutra FastAPI backend. No Supabase key is ever sent to the
-- browser, so the Supabase Data API (anon / authenticated roles) is given NO access to application data:
--   * every table has Row Level Security enabled and NO policy for anon/authenticated  -> deny by default;
--   * table privileges are revoked from anon/authenticated as a second layer.
-- The backend connects as the dedicated role `nyaya_app` (least privilege: DML on the app tables only, no DDL,
-- not a superuser, cannot bypass RLS except through the explicit nyaya_app policies below).
-- Files live in the PRIVATE bucket `case-files`; only the backend (secret key, server-side) can read or write.
-- Idempotent: safe to re-run.

-- ── Backend role ───────────────────────────────────────────────────────────
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'nyaya_app') then
        -- LOGIN without a password: it cannot sign in until the project owner sets one (see docs/SUPABASE_MIGRATION.md).
        create role nyaya_app login noinherit;
    end if;
end $$;

grant usage on schema public to nyaya_app;
grant select, insert, update, delete on all tables in schema public to nyaya_app;
grant usage, select, update on all sequences in schema public to nyaya_app;
alter default privileges in schema public grant select, insert, update, delete on tables to nyaya_app;
alter default privileges in schema public grant usage, select, update on sequences to nyaya_app;

-- ── Close the Data API for client roles ───────────────────────────────────
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;

-- ── Row Level Security on every application table ─────────────────────────
-- Policies grant access to the backend role only. Per-user / per-matter rules are enforced by the backend today
-- (single-user workspace, matter isolation in retrieval); when user accounts are added these policies are where
-- ownership and membership rules will be expressed.
do $$
declare t text;
begin
    foreach t in array array[
        'meta', 'cases', 'case_notes', 'documents', 'chunks', 'chats', 'messages', 'prefs', 'audit_log',
        'notifications', 'integrations', 'sync_log', 'source_records', 'doc_derivatives', 'drafts',
        'draft_versions', 'case_provisions']
    loop
        execute format('alter table public.%I enable row level security', t);
        if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = t and policyname = 'backend_all') then
            execute format('create policy backend_all on public.%I for all to nyaya_app using (true) with check (true)', t);
        end if;
    end loop;
end $$;

-- The audit log is append-only for the backend: no updates or deletes of history.
revoke update, delete on public.audit_log from nyaya_app;

-- ── Private bucket for client documents ───────────────────────────────────
-- 40 MB per file (matches MAX_UPLOAD_MB); only the document types the app accepts.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('case-files', 'case-files', false, 41943040, array[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'text/plain', 'text/markdown',
    'image/png', 'image/jpeg', 'image/tiff', 'image/webp'])
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit,
                               allowed_mime_types = excluded.allowed_mime_types;
-- No storage.objects policies are created for anon/authenticated: the bucket is unreachable from browsers.
-- The backend uses the secret (service) key, which Supabase Storage authorises server-side.
