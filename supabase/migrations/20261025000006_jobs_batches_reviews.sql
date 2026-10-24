-- NyaySutra AI — durable background jobs, upload batches, draft review, document lifecycle states.
--
-- * jobs: every long task (document ingestion, translation, summaries, drafting, provision suggestions) is a row with
--   an explicit state, an owner, bounded attempts, a next-run time (exponential back-off), a checkpoint for resuming
--   and a cancellation flag. Workers claim rows with FOR UPDATE SKIP LOCKED, so a restart or a second app instance
--   never loses or double-runs a job. Jobs that keep failing end in the 'dead' state (dead letter) instead of looping.
-- * upload_batches / batch_items: bulk uploads with limits, per-file outcome, progress and cancellation.
-- * drafts: review status — a generated document stays a draft until an advocate approves a specific version.
-- * documents: new lifecycle states (quarantined, cancelled, superseded).
-- Idempotent: safe to re-run.

create table if not exists public.jobs (
    id               text primary key,
    kind             text not null,
    lane             text not null default 'ai' check (lane in ('ingest', 'ai')),
    owner_id         text,
    org_id           text,
    case_id          text,
    dedupe_key       text,
    status           text not null default 'queued'
                     check (status in ('queued', 'processing', 'partial', 'completed', 'failed', 'cancelled', 'dead')),
    params           jsonb default '{}'::jsonb,
    progress_done    integer not null default 0,
    progress_total   integer not null default 0,
    message          text default '',
    result           jsonb,
    error            text,
    attempts         integer not null default 0,
    max_attempts     integer not null default 3,
    next_run_at      text not null,
    locked_by        text,
    locked_until     text,
    cancel_requested integer not null default 0,
    checkpoint       jsonb default '{}'::jsonb,
    created_at       text not null,
    started_at       text,
    finished_at      text,
    updated_at       text not null
);
create index if not exists ix_jobs_queue on public.jobs(lane, status, next_run_at);
create index if not exists ix_jobs_owner on public.jobs(owner_id, created_at desc);
create index if not exists ix_jobs_dedupe on public.jobs(dedupe_key) where status in ('queued', 'processing');

create table if not exists public.upload_batches (
    id               text primary key,
    owner_id         text not null,
    org_id           text,
    case_id          text not null references public.cases(id) on delete cascade,
    total            integer not null,
    total_bytes      bigint not null default 0,
    cancel_requested integer not null default 0,
    created_at       text not null,
    updated_at       text not null
);
create index if not exists ix_upload_batches_owner on public.upload_batches(owner_id, created_at desc);

create table if not exists public.batch_items (
    id         text primary key,
    batch_id   text not null references public.upload_batches(id) on delete cascade,
    seq        integer not null,
    filename   text not null,
    bytes      bigint not null default 0,
    status     text not null default 'waiting'
               check (status in ('waiting', 'stored', 'duplicate', 'rejected', 'cancelled')),
    reason     text default '',
    doc_id     text,
    updated_at text not null,
    unique (batch_id, seq)
);
create index if not exists ix_batch_items_batch on public.batch_items(batch_id, seq);

alter table public.drafts add column if not exists review_status text not null default 'draft';
alter table public.drafts add column if not exists reviewed_by text;
alter table public.drafts add column if not exists reviewed_at text;
alter table public.drafts add column if not exists approved_version integer;
alter table public.drafts add column if not exists review_note text default '';
alter table public.drafts drop constraint if exists drafts_review_status_check;
alter table public.drafts add constraint drafts_review_status_check
    check (review_status in ('draft', 'changes_requested', 'approved'));

alter table public.documents drop constraint if exists documents_status_check;
alter table public.documents add constraint documents_status_check
    check (status in ('processing', 'ready', 'failed', 'quarantined', 'cancelled', 'superseded'));

grant select, insert, update, delete on public.jobs, public.upload_batches, public.batch_items to nyaya_app;
revoke all on public.jobs, public.upload_batches, public.batch_items from anon, authenticated;

-- ── Row Level Security ─────────────────────────────────────────────────────
do $$
declare t text;
begin
    foreach t in array array['jobs', 'upload_batches', 'batch_items']
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('drop policy if exists sys_all on public.%I', t);
        execute format('drop policy if exists app_read on public.%I', t);
        execute format('drop policy if exists app_insert on public.%I', t);
        execute format('drop policy if exists app_update on public.%I', t);
        execute format('create policy sys_all on public.%I for all to nyaya_app '
                       'using ((select private.is_system())) with check ((select private.is_system()))', t);
    end loop;
end $$;

-- A job or batch is visible to, and can be cancelled by, the person who started it. Workers run in the system
-- context and re-check the owner's current permissions before doing anything on their behalf.
create policy app_read on public.jobs for select to nyaya_app using (owner_id = (select private.uid()));
create policy app_insert on public.jobs for insert to nyaya_app with check (owner_id = (select private.uid()));
create policy app_update on public.jobs for update to nyaya_app
    using (owner_id = (select private.uid())) with check (owner_id = (select private.uid()));

create policy app_read on public.upload_batches for select to nyaya_app using (owner_id = (select private.uid()));
create policy app_insert on public.upload_batches for insert to nyaya_app
    with check (owner_id = (select private.uid()) and case_id in (select private.accessible_case_ids()));
create policy app_update on public.upload_batches for update to nyaya_app
    using (owner_id = (select private.uid())) with check (owner_id = (select private.uid()));

create policy app_read on public.batch_items for select to nyaya_app
    using (batch_id in (select b.id from public.upload_batches b));
create policy app_insert on public.batch_items for insert to nyaya_app
    with check (batch_id in (select b.id from public.upload_batches b));
create policy app_update on public.batch_items for update to nyaya_app
    using (batch_id in (select b.id from public.upload_batches b))
    with check (batch_id in (select b.id from public.upload_batches b));

insert into public.meta(key, value) values ('schema_version', '3')
on conflict (key) do update set value = excluded.value;
