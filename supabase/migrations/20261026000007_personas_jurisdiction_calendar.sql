-- NyaySutra AI — two personas, all-India jurisdiction, research history, calendar and reminders.
--
-- * Personas: only Lawyer ('advocate') and Administrator ('admin') remain. Former 'client' and 'auditor'
--   memberships and invitations become 'advocate' — exactly what public sign-up gives anyone — and per-case
--   'client' access is removed (no one gains access to a matter through this migration). The audit log is kept;
--   only administrators read it.
-- * documents.jurisdiction: 'Central' or the state / Union Territory whose legislature made the act
--   (from India Code's own act id / state metadata). Research can be limited to central law + one state.
-- * search_history: each user's own searches (personal; reopened from Legal Research → Recent History).
-- * calendar_events / event_reminders: each lawyer's hearings, deadlines and tasks, with reminders computed in
--   UTC from the event's own time zone and delivered by a background dispatcher (FOR UPDATE SKIP LOCKED).
-- Idempotent: safe to re-run.

-- ── Personas ─────────────────────────────────────────────────────────────
update public.memberships set role = 'advocate' where role in ('client', 'auditor');
update public.invitations set role = 'advocate' where role in ('client', 'auditor');
delete from public.case_members where access = 'client';

alter table public.memberships drop constraint if exists memberships_role_check;
alter table public.memberships add constraint memberships_role_check check (role in ('advocate', 'admin'));
alter table public.invitations drop constraint if exists invitations_role_check;
alter table public.invitations add constraint invitations_role_check check (role in ('advocate', 'admin'));
alter table public.case_members drop constraint if exists case_members_access_check;
alter table public.case_members add constraint case_members_access_check check (access in ('owner', 'advocate'));

create or replace function private.case_access(p_case text) returns text
language sql stable security definer set search_path = '' as $$
    select case when m.role in ('advocate', 'admin') then cm.access end
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
    where cm.user_id = auth.uid()::text and p.status = 'active' and m.role in ('advocate', 'admin')
$$;
revoke all on function private.case_access(text), private.accessible_case_ids() from public, anon, authenticated;

drop policy if exists app_read on public.audit_log;
create policy app_read on public.audit_log for select to nyaya_app
    using (private.org_role(org_id) = 'admin');

-- ── Jurisdiction of library documents ────────────────────────────────────
alter table public.documents add column if not exists jurisdiction text not null default '';
update public.documents set jurisdiction = 'Central' where collection = 'bare_act' and jurisdiction = '';
update public.documents set jurisdiction = 'Maharashtra'
    where collection = 'bare_act' and title ilike 'The Maharashtra %';
create index if not exists ix_documents_jurisdiction on public.documents(collection, jurisdiction);

-- ── Research history ─────────────────────────────────────────────────────
create table if not exists public.search_history (
    id          text primary key,
    user_id     text not null,
    org_id      text,
    query       text not null check (length(query) <= 500),
    filters     text default '{}',
    results     integer,
    created_at  text not null
);
create index if not exists ix_search_history_user on public.search_history(user_id, created_at desc);

-- ── Calendar ─────────────────────────────────────────────────────────────
create table if not exists public.calendar_events (
    id               text primary key,
    org_id           text,
    owner_id         text not null,
    case_id          text references public.cases(id) on delete set null,
    kind             text not null check (kind in ('hearing', 'deadline', 'follow_up', 'submission', 'meeting', 'task')),
    title            text not null check (length(title) between 1 and 200),
    description      text default '',
    event_date       text not null,              -- YYYY-MM-DD in the event's time zone
    event_time       text,                       -- HH:MM, or null for an all-day item
    timezone         text not null,              -- IANA name, e.g. Asia/Kolkata
    starts_at_utc    text not null,              -- YYYY-MM-DDTHH:MM:SS (UTC)
    priority         text not null default 'normal' check (priority in ('low', 'normal', 'high')),
    status           text not null default 'scheduled' check (status in ('scheduled', 'completed', 'cancelled')),
    reminders        text default '[]',          -- rule names, e.g. ["day_of", "1d", "7d"]
    email_reminders  integer not null default 0,
    completed_at     text,
    created_at       text not null,
    updated_at       text not null
);
create index if not exists ix_events_owner_date on public.calendar_events(owner_id, event_date);
create index if not exists ix_events_case on public.calendar_events(case_id);

create table if not exists public.event_reminders (
    id             text primary key,
    event_id       text not null references public.calendar_events(id) on delete cascade,
    owner_id       text not null,
    rule           text not null,
    channel        text not null check (channel in ('in_app', 'email')),
    remind_at_utc  text not null,
    status         text not null default 'pending' check (status in ('pending', 'sending', 'sent', 'failed', 'skipped')),
    attempts       integer not null default 0,
    locked_until   text,
    sent_at        text,
    error          text,
    unique (event_id, rule, channel)
);
create index if not exists ix_reminders_due on public.event_reminders(status, remind_at_utc);

grant select, insert, update, delete on public.search_history, public.calendar_events, public.event_reminders to nyaya_app;
revoke all on public.search_history, public.calendar_events, public.event_reminders from anon, authenticated;

do $$
declare t text;
begin
    foreach t in array array['search_history', 'calendar_events', 'event_reminders']
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('drop policy if exists sys_all on public.%I', t);
        execute format('drop policy if exists app_read on public.%I', t);
        execute format('drop policy if exists app_insert on public.%I', t);
        execute format('drop policy if exists app_update on public.%I', t);
        execute format('drop policy if exists app_delete on public.%I', t);
        execute format('create policy sys_all on public.%I for all to nyaya_app '
                       'using ((select private.is_system())) with check ((select private.is_system()))', t);
    end loop;
end $$;

-- Personal records: only their owner reads or changes them (administrators included — calendars are private).
create policy app_read on public.search_history for select to nyaya_app using (user_id = (select private.uid()));
create policy app_insert on public.search_history for insert to nyaya_app with check (user_id = (select private.uid()));
create policy app_delete on public.search_history for delete to nyaya_app using (user_id = (select private.uid()));

-- An event may be linked only to a matter its owner can access.
create policy app_read on public.calendar_events for select to nyaya_app using (owner_id = (select private.uid()));
create policy app_insert on public.calendar_events for insert to nyaya_app
    with check (owner_id = (select private.uid())
                and org_id in (select private.my_org_ids())
                and (case_id is null or case_id in (select private.accessible_case_ids())));
create policy app_update on public.calendar_events for update to nyaya_app
    using (owner_id = (select private.uid()))
    with check (owner_id = (select private.uid())
                and (case_id is null or case_id in (select private.accessible_case_ids())));
create policy app_delete on public.calendar_events for delete to nyaya_app using (owner_id = (select private.uid()));

create policy app_read on public.event_reminders for select to nyaya_app using (owner_id = (select private.uid()));
create policy app_insert on public.event_reminders for insert to nyaya_app
    with check (owner_id = (select private.uid())
                and event_id in (select e.id from public.calendar_events e));
create policy app_update on public.event_reminders for update to nyaya_app
    using (owner_id = (select private.uid())) with check (owner_id = (select private.uid()));
create policy app_delete on public.event_reminders for delete to nyaya_app using (owner_id = (select private.uid()));

insert into public.meta(key, value) values ('schema_version', '4')
on conflict (key) do update set value = excluded.value;
