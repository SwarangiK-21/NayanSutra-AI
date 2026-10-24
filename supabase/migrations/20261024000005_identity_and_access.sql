-- NyaySutra AI — identities, roles, case membership and row-level security based on the signed-in user.
--
-- Identity comes from Supabase Auth. The browser never talks to Supabase: the backend signs users in through the
-- Auth API, keeps their tokens server-side, and for every database statement passes the signed-in user to Postgres
-- the same way PostgREST does — the `request.jwt.claims` setting — so policies can use Supabase's own auth.uid().
-- Internal workers (ingestion, scheduled sync, retention) run with the claim {"nyaya_system": true}.
--
-- RLS here is the second line of defence: the backend checks every permission first (nyaya/authz.py). RLS stops a
-- missed check from becoming a data leak. It does not defend against someone able to run arbitrary SQL as
-- nyaya_app (they could set the claims); SQL injection is prevented by parameterised queries throughout.
-- Idempotent: safe to re-run.

-- ── Helper schema (not exposed through the Data API) ──────────────────────
create schema if not exists private;
revoke all on schema private from public, anon, authenticated;
grant usage on schema private to nyaya_app;

-- ── Tables ────────────────────────────────────────────────────────────────
create table if not exists public.organizations (
    id         text primary key,
    name       text not null,
    created_at text not null
);

-- One row per person. user_id = auth.users.id for Supabase accounts (the local single-user operator uses a fixed id).
create table if not exists public.profiles (
    user_id      text primary key,
    email        text not null,
    display_name text default '',
    status       text not null default 'active' check (status in ('active', 'disabled')),
    mfa_enabled  integer not null default 0,
    created_at   text not null,
    updated_at   text not null
);
create unique index if not exists ux_profiles_email on public.profiles(lower(email));

-- The role lives here, never in the user's own (editable) metadata, and only administrators can change it.
create table if not exists public.memberships (
    org_id     text not null references public.organizations(id) on delete cascade,
    user_id    text not null references public.profiles(user_id) on delete cascade,
    role       text not null check (role in ('client', 'advocate', 'admin', 'auditor')),
    created_at text not null,
    created_by text,
    primary key (org_id, user_id)
);
create index if not exists ix_memberships_user on public.memberships(user_id);

-- An administrator pre-authorises an e-mail address with a role; it is applied when that person signs in with a
-- verified e-mail. Nobody chooses their own role.
create table if not exists public.invitations (
    id          text primary key,
    org_id      text not null references public.organizations(id) on delete cascade,
    email       text not null,
    role        text not null check (role in ('client', 'advocate', 'admin', 'auditor')),
    invited_by  text,
    created_at  text not null,
    expires_at  text not null,
    accepted_at text,
    accepted_by text,
    meta        jsonb default '{}'::jsonb
);
create unique index if not exists ux_invitations_open on public.invitations(org_id, lower(email)) where accepted_at is null;

create table if not exists public.case_members (
    case_id    text not null references public.cases(id) on delete cascade,
    user_id    text not null references public.profiles(user_id) on delete cascade,
    access     text not null check (access in ('owner', 'advocate', 'client')),
    added_by   text,
    created_at text not null,
    primary key (case_id, user_id)
);
create index if not exists ix_case_members_user on public.case_members(user_id);

create table if not exists public.user_prefs (
    user_id    text not null,
    key        text not null,
    value      text not null,
    updated_at text not null,
    primary key (user_id, key)
);

create table if not exists public.org_settings (
    org_id     text not null references public.organizations(id) on delete cascade,
    key        text not null,
    value      text not null,
    updated_at text not null,
    updated_by text,
    primary key (org_id, key)
);

-- Server-side sessions. The cookie carries a random token; only its SHA-256 is stored, so a database leak does not
-- give working sessions. Supabase tokens for the session are kept in Supabase Vault (token_ref), never here.
create table if not exists public.app_sessions (
    id                text primary key,
    public_id         text not null unique,
    user_id           text not null,
    org_id            text,
    state             text not null default 'active' check (state in ('active', 'mfa_pending', 'mfa_enroll', 'recovery')),
    aal               text not null default 'aal1',
    created_at        text not null,
    last_seen_at      text not null,
    expires_at        text not null,
    ip                text default '',
    user_agent        text default '',
    sb_session_id     text,
    token_ref         text,
    access_expires_at text,
    mfa_attempts      integer not null default 0,
    revoked_at        text,
    meta              jsonb default '{}'::jsonb
);
create index if not exists ix_app_sessions_user on public.app_sessions(user_id);

-- MFA recovery codes (Supabase MFA has none). Codes are random (50 bits); only a keyed hash is stored.
create table if not exists public.recovery_codes (
    user_id    text not null references public.profiles(user_id) on delete cascade,
    code_hash  text not null,
    created_at text not null,
    used_at    text,
    primary key (user_id, code_hash)
);

-- Account-level brute-force protection (per e-mail, across IPs and app instances).
create table if not exists public.auth_throttle (
    key          text primary key,
    failures     integer not null default 0,
    window_start text not null,
    locked_until text
);

-- ── Ownership columns on existing tables ──────────────────────────────────
alter table public.cases          add column if not exists org_id text references public.organizations(id);
alter table public.cases          add column if not exists created_by text;
alter table public.documents      add column if not exists uploaded_by text;
alter table public.chats          add column if not exists user_id text;
alter table public.chats          add column if not exists org_id text;
alter table public.drafts         add column if not exists created_by text;
alter table public.drafts         add column if not exists org_id text;
alter table public.source_records add column if not exists created_by text;
alter table public.source_records add column if not exists org_id text;
alter table public.notifications  add column if not exists user_id text;
alter table public.audit_log      add column if not exists actor_id text;
alter table public.audit_log      add column if not exists actor_role text;
alter table public.audit_log      add column if not exists org_id text;
alter table public.audit_log      add column if not exists ip text;
alter table public.audit_log      add column if not exists outcome text default 'ok';

create index if not exists ix_cases_org on public.cases(org_id);
create index if not exists ix_chats_user on public.chats(user_id, updated_at);
create index if not exists ix_drafts_owner on public.drafts(created_by, updated_at);
create index if not exists ix_notifications_user on public.notifications(user_id, created_at);
create index if not exists ix_audit_org on public.audit_log(org_id, id);

-- A user's own imported court records are separate from another user's import of the same record.
alter table public.source_records drop constraint if exists source_records_source_external_id_key;
create unique index if not exists ux_source_records_owner
    on public.source_records(source, external_id, coalesce(created_by, ''));

-- ── Backfill: everything that existed belongs to the default organisation ─
insert into public.organizations(id, name, created_at)
values ('org_default', 'NyaySutra workspace', to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS'))
on conflict (id) do nothing;
update public.cases set org_id = 'org_default' where org_id is null;
update public.chats set org_id = coalesce((select c.org_id from public.cases c where c.id = chats.case_id), 'org_default')
where org_id is null;
update public.drafts set org_id = coalesce((select c.org_id from public.cases c where c.id = drafts.case_id), 'org_default')
where org_id is null;
update public.source_records set org_id = 'org_default' where org_id is null and kind = 'case_record';
alter table public.cases alter column org_id set not null;

-- Organisation settings that used to be global preferences.
insert into public.org_settings(org_id, key, value, updated_at)
select 'org_default', key, value, updated_at from public.prefs
where key = 'chat_retention_days' or key like 'forum_reqs:%'
on conflict do nothing;

-- ── Identity helpers (SECURITY DEFINER: they read membership tables without recursive RLS) ──
-- auth.uid() is Supabase's own function; nyaya_app has no usage on schema auth, so it is called through this wrapper.
create or replace function private.uid() returns text
language sql stable security definer set search_path = '' as $$
    select auth.uid()::text
$$;

create or replace function private.is_system() returns boolean
language sql stable security definer set search_path = '' as $$
    select coalesce((auth.jwt() ->> 'nyaya_system')::boolean, false)
$$;

-- The caller's role in an organisation (only for an active profile).
create or replace function private.org_role(p_org text) returns text
language sql stable security definer set search_path = '' as $$
    select m.role from public.memberships m join public.profiles p on p.user_id = m.user_id
    where m.org_id = p_org and m.user_id = auth.uid()::text and p.status = 'active'
$$;

create or replace function private.my_org_ids() returns setof text
language sql stable security definer set search_path = '' as $$
    select m.org_id from public.memberships m join public.profiles p on p.user_id = m.user_id
    where m.user_id = auth.uid()::text and p.status = 'active'
$$;

-- Effective access to a case: the membership level, capped by the organisation role
-- (a client is never more than 'client'; auditors and guests get nothing).
create or replace function private.case_access(p_case text) returns text
language sql stable security definer set search_path = '' as $$
    select case when m.role = 'client' then 'client'
                when m.role in ('advocate', 'admin') then cm.access end
    from public.case_members cm
    join public.cases c on c.id = cm.case_id
    join public.memberships m on m.org_id = c.org_id and m.user_id = cm.user_id
    join public.profiles p on p.user_id = cm.user_id
    where cm.case_id = p_case and cm.user_id = auth.uid()::text and p.status = 'active'
$$;

create or replace function private.accessible_case_ids() returns setof text
language sql stable security definer set search_path = '' as $$
    select cm.case_id
    from public.case_members cm
    join public.cases c on c.id = cm.case_id
    join public.memberships m on m.org_id = c.org_id and m.user_id = cm.user_id
    join public.profiles p on p.user_id = cm.user_id
    where cm.user_id = auth.uid()::text and p.status = 'active' and m.role in ('client', 'advocate', 'admin')
$$;

create or replace function private.case_org(p_case text) returns text
language sql stable security definer set search_path = '' as $$
    select org_id from public.cases where id = p_case
$$;

-- True when the caller administers an organisation the given user belongs to.
create or replace function private.administers_user(p_user text) returns boolean
language sql stable security definer set search_path = '' as $$
    select exists (select 1 from public.memberships target
                   where target.user_id = p_user and private.org_role(target.org_id) = 'admin')
$$;

create or replace function private.shares_org(p_user text) returns boolean
language sql stable security definer set search_path = '' as $$
    select exists (select 1 from public.memberships target
                   where target.user_id = p_user and target.org_id in (select private.my_org_ids()))
$$;

-- The creator of a case is its first owner (enforced in the database, not left to application code).
create or replace function private.case_owner_on_insert() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
    if new.created_by is not null then
        insert into public.case_members(case_id, user_id, access, added_by, created_at)
        values (new.id, new.created_by, 'owner', new.created_by, new.created_at)
        on conflict do nothing;
    end if;
    return new;
end $$;
drop trigger if exists trg_case_owner on public.cases;
create trigger trg_case_owner after insert on public.cases for each row execute function private.case_owner_on_insert();

-- Session tokens in Supabase Vault (encrypted at rest by Supabase). Only callable in the system context.
create or replace function private.token_put(p_name text, p_secret text) returns uuid
language plpgsql security definer set search_path = '' as $$
begin
    if not private.is_system() then raise exception 'not permitted'; end if;
    return vault.create_secret(p_secret, p_name, 'NyaySutra session tokens');
end $$;

create or replace function private.token_get(p_id uuid) returns text
language plpgsql security definer set search_path = '' as $$
declare v text;
begin
    if not private.is_system() then raise exception 'not permitted'; end if;
    select decrypted_secret into v from vault.decrypted_secrets where id = p_id;
    return v;
end $$;

create or replace function private.token_update(p_id uuid, p_secret text) returns void
language plpgsql security definer set search_path = '' as $$
begin
    if not private.is_system() then raise exception 'not permitted'; end if;
    perform vault.update_secret(p_id, p_secret);
end $$;

create or replace function private.token_delete(p_id uuid) returns void
language plpgsql security definer set search_path = '' as $$
begin
    if not private.is_system() then raise exception 'not permitted'; end if;
    delete from vault.secrets where id = p_id;
end $$;

revoke all on all functions in schema private from public, anon, authenticated;
grant execute on all functions in schema private to nyaya_app;

-- ── Privileges for the new tables (same model as migration 2) ─────────────
grant select, insert, update, delete on all tables in schema public to nyaya_app;
grant usage, select, update on all sequences in schema public to nyaya_app;
revoke all on all tables in schema public from anon, authenticated;
revoke update, delete on public.audit_log from nyaya_app;

-- ── Row Level Security: replace the blanket backend policies ──────────────
do $$
declare t text;
begin
    foreach t in array array[
        'meta', 'cases', 'case_notes', 'documents', 'chunks', 'chats', 'messages', 'prefs', 'audit_log',
        'notifications', 'integrations', 'sync_log', 'source_records', 'doc_derivatives', 'drafts',
        'draft_versions', 'case_provisions', 'organizations', 'profiles', 'memberships', 'invitations',
        'case_members', 'user_prefs', 'org_settings', 'app_sessions', 'recovery_codes', 'auth_throttle']
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('drop policy if exists backend_all on public.%I', t);
        execute format('drop policy if exists sys_all on public.%I', t);
        execute format('drop policy if exists app_read on public.%I', t);
        execute format('drop policy if exists app_insert on public.%I', t);
        execute format('drop policy if exists app_update on public.%I', t);
        execute format('drop policy if exists app_delete on public.%I', t);
        -- Internal workers (ingestion, sync, retention, sign-in) — explicit system context only.
        execute format('create policy sys_all on public.%I for all to nyaya_app '
                       'using ((select private.is_system())) with check ((select private.is_system()))', t);
    end loop;
end $$;

-- Shared reference data: readable by everyone using the app (including signed-out guests for the library).
create policy app_read on public.meta for select to nyaya_app using (true);
create policy app_read on public.integrations for select to nyaya_app using ((select private.uid()) is not null);
create policy app_read on public.sync_log for select to nyaya_app using ((select private.uid()) is not null);

-- Organisations, profiles, memberships, invitations
create policy app_read on public.organizations for select to nyaya_app
    using (id in (select private.my_org_ids()));
create policy app_read on public.profiles for select to nyaya_app
    using (user_id = (select private.uid()) or private.shares_org(user_id));
create policy app_update on public.profiles for update to nyaya_app
    using (user_id = (select private.uid()) or private.administers_user(user_id))
    with check (user_id = (select private.uid()) or private.administers_user(user_id));
create policy app_read on public.memberships for select to nyaya_app
    using (org_id in (select private.my_org_ids()));
-- Administrators manage memberships of their organisation, but never their own row (no self-escalation or lock-out).
create policy app_insert on public.memberships for insert to nyaya_app
    with check (private.org_role(org_id) = 'admin' and user_id <> (select private.uid()));
create policy app_update on public.memberships for update to nyaya_app
    using (private.org_role(org_id) = 'admin' and user_id <> (select private.uid()))
    with check (private.org_role(org_id) = 'admin' and user_id <> (select private.uid()));
create policy app_delete on public.memberships for delete to nyaya_app
    using (private.org_role(org_id) = 'admin' and user_id <> (select private.uid()));
create policy app_read on public.invitations for select to nyaya_app using (private.org_role(org_id) = 'admin');
create policy app_insert on public.invitations for insert to nyaya_app with check (private.org_role(org_id) = 'admin');
create policy app_update on public.invitations for update to nyaya_app
    using (private.org_role(org_id) = 'admin') with check (private.org_role(org_id) = 'admin');
create policy app_delete on public.invitations for delete to nyaya_app using (private.org_role(org_id) = 'admin');

-- Cases: members see their cases; administrators see the organisation's case list (metadata) to assign people.
create policy app_read on public.cases for select to nyaya_app
    using (private.case_access(id) is not null or private.org_role(org_id) = 'admin');
create policy app_insert on public.cases for insert to nyaya_app
    with check (created_by = (select private.uid()) and private.org_role(org_id) in ('advocate', 'admin'));
create policy app_update on public.cases for update to nyaya_app
    using (private.case_access(id) in ('owner', 'advocate'))
    with check (private.case_access(id) in ('owner', 'advocate'));
create policy app_delete on public.cases for delete to nyaya_app
    using (private.case_access(id) = 'owner' or private.org_role(org_id) = 'admin');

create policy app_read on public.case_members for select to nyaya_app
    using (private.case_access(case_id) is not null or private.org_role(private.case_org(case_id)) = 'admin');
create policy app_insert on public.case_members for insert to nyaya_app
    with check (private.case_access(case_id) = 'owner' or private.org_role(private.case_org(case_id)) = 'admin');
create policy app_update on public.case_members for update to nyaya_app
    using (private.case_access(case_id) = 'owner' or private.org_role(private.case_org(case_id)) = 'admin')
    with check (private.case_access(case_id) = 'owner' or private.org_role(private.case_org(case_id)) = 'admin');
create policy app_delete on public.case_members for delete to nyaya_app
    using (private.case_access(case_id) = 'owner' or private.org_role(private.case_org(case_id)) = 'admin');

-- Internal advocate work product: case team only (not clients).
create policy app_read on public.case_notes for select to nyaya_app using (private.case_access(case_id) in ('owner', 'advocate'));
create policy app_insert on public.case_notes for insert to nyaya_app with check (private.case_access(case_id) in ('owner', 'advocate'));
create policy app_read on public.case_provisions for select to nyaya_app using (private.case_access(case_id) in ('owner', 'advocate'));
create policy app_insert on public.case_provisions for insert to nyaya_app with check (private.case_access(case_id) in ('owner', 'advocate'));
create policy app_update on public.case_provisions for update to nyaya_app
    using (private.case_access(case_id) in ('owner', 'advocate')) with check (private.case_access(case_id) in ('owner', 'advocate'));

-- Documents: the shared law library is public; a matter's files need case access.
create policy app_read on public.documents for select to nyaya_app
    using (collection <> 'case' or case_id in (select private.accessible_case_ids()));
create policy app_insert on public.documents for insert to nyaya_app
    with check (collection = 'case' and case_id in (select private.accessible_case_ids()));
create policy app_update on public.documents for update to nyaya_app
    using (collection = 'case' and case_id in (select private.accessible_case_ids()))
    with check (collection = 'case' and case_id in (select private.accessible_case_ids()));
create policy app_delete on public.documents for delete to nyaya_app
    using (collection = 'case' and private.case_access(case_id) in ('owner', 'advocate'));

-- Passages and derived text follow their document (the subquery is itself subject to the documents policy).
create policy app_read on public.chunks for select to nyaya_app using (doc_id in (select d.id from public.documents d));
create policy app_insert on public.chunks for insert to nyaya_app
    with check (doc_id in (select d.id from public.documents d where d.collection = 'case'));
create policy app_read on public.doc_derivatives for select to nyaya_app using (doc_id in (select d.id from public.documents d));
create policy app_insert on public.doc_derivatives for insert to nyaya_app
    with check (doc_id in (select d.id from public.documents d where d.collection = 'case'));
create policy app_update on public.doc_derivatives for update to nyaya_app
    using (doc_id in (select d.id from public.documents d where d.collection = 'case'))
    with check (doc_id in (select d.id from public.documents d where d.collection = 'case'));

-- Research chats are personal; a chat about a case also needs access to that case.
create policy app_read on public.chats for select to nyaya_app
    using (user_id = (select private.uid()) and (case_id is null or case_id in (select private.accessible_case_ids())));
create policy app_insert on public.chats for insert to nyaya_app
    with check (user_id = (select private.uid()) and (case_id is null or case_id in (select private.accessible_case_ids())));
create policy app_update on public.chats for update to nyaya_app
    using (user_id = (select private.uid())) with check (user_id = (select private.uid()));
create policy app_delete on public.chats for delete to nyaya_app using (user_id = (select private.uid()));
create policy app_read on public.messages for select to nyaya_app using (chat_id in (select ch.id from public.chats ch));
create policy app_insert on public.messages for insert to nyaya_app with check (chat_id in (select ch.id from public.chats ch));

-- Drafts and notices: the author's own, or the case team's for a case draft.
create policy app_read on public.drafts for select to nyaya_app
    using ((case_id is null and created_by = (select private.uid()))
           or (case_id is not null and private.case_access(case_id) in ('owner', 'advocate')));
create policy app_insert on public.drafts for insert to nyaya_app
    with check (created_by = (select private.uid())
                and (case_id is null or private.case_access(case_id) in ('owner', 'advocate')));
create policy app_update on public.drafts for update to nyaya_app
    using ((case_id is null and created_by = (select private.uid()))
           or (case_id is not null and private.case_access(case_id) in ('owner', 'advocate')))
    with check ((case_id is null and created_by = (select private.uid()))
                or (case_id is not null and private.case_access(case_id) in ('owner', 'advocate')));
create policy app_delete on public.drafts for delete to nyaya_app
    using ((case_id is null and created_by = (select private.uid()))
           or (case_id is not null and private.case_access(case_id) in ('owner', 'advocate')));
create policy app_read on public.draft_versions for select to nyaya_app using (draft_id in (select d.id from public.drafts d));
create policy app_insert on public.draft_versions for insert to nyaya_app with check (draft_id in (select d.id from public.drafts d));

-- External records: public news/act updates for everyone; a user's own court-record imports; the case team's linked records.
create policy app_read on public.source_records for select to nyaya_app
    using ((kind <> 'case_record' and case_id is null)
           or (case_id is null and created_by = (select private.uid()))
           or (case_id is not null and private.case_access(case_id) in ('owner', 'advocate')));
create policy app_insert on public.source_records for insert to nyaya_app
    with check (created_by = (select private.uid()));
create policy app_update on public.source_records for update to nyaya_app
    using (created_by = (select private.uid()) or (case_id is not null and private.case_access(case_id) in ('owner', 'advocate')))
    with check (created_by = (select private.uid()) or (case_id is not null and private.case_access(case_id) in ('owner', 'advocate')));

-- Personal data
create policy app_read on public.notifications for select to nyaya_app using (user_id = (select private.uid()));
create policy app_update on public.notifications for update to nyaya_app
    using (user_id = (select private.uid())) with check (user_id = (select private.uid()));
create policy app_read on public.user_prefs for select to nyaya_app using (user_id = (select private.uid()));
create policy app_insert on public.user_prefs for insert to nyaya_app with check (user_id = (select private.uid()));
create policy app_update on public.user_prefs for update to nyaya_app
    using (user_id = (select private.uid())) with check (user_id = (select private.uid()));
create policy app_read on public.org_settings for select to nyaya_app using (org_id in (select private.my_org_ids()));
create policy app_insert on public.org_settings for insert to nyaya_app
    with check (private.org_role(org_id) in ('admin', 'advocate'));
create policy app_update on public.org_settings for update to nyaya_app
    using (private.org_role(org_id) in ('admin', 'advocate')) with check (private.org_role(org_id) in ('admin', 'advocate'));

-- Audit log: append-only; anyone signed in may record their own actions; admins and auditors read their organisation's.
create policy app_insert on public.audit_log for insert to nyaya_app
    with check (actor_id is not distinct from (select private.uid()));
create policy app_read on public.audit_log for select to nyaya_app
    using (private.org_role(org_id) in ('admin', 'auditor'));

-- app_sessions, recovery_codes, auth_throttle, prefs: system context only (no app_* policies).

insert into public.meta(key, value) values ('schema_version', '2')
on conflict (key) do update set value = excluded.value;
