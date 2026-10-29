-- NyaySutra AI — data retention.
--
-- * profiles.last_active_at: the date a user last used the app (kept on the profile; login sessions expire and are
--   removed, so they cannot show long inactivity). profiles.retention_warned_at: when the user was warned that the
--   account will be deleted for inactivity.
-- * private.purge_audit_log(before): the app role cannot delete audit entries (the log is append-only, see
--   migration 05). Retention needs to remove entries older than the retention period; this function does only that,
--   and only for system work (the daily retention job), never for a signed-in user.
-- Idempotent.

alter table public.profiles add column if not exists last_active_at text;
alter table public.profiles add column if not exists retention_warned_at text;
update public.profiles p
set last_active_at = coalesce((select max(s.last_seen_at) from public.app_sessions s where s.user_id = p.user_id),
                              p.created_at)
where p.last_active_at is null;
create index if not exists ix_profiles_last_active on public.profiles(last_active_at);
create index if not exists ix_chats_updated on public.chats(updated_at);
create index if not exists ix_search_history_created on public.search_history(created_at);
create index if not exists ix_audit_at on public.audit_log(at);

create or replace function private.purge_audit_log(p_before text) returns integer
language plpgsql security definer set search_path = '' as $$
declare n integer;
begin
    if not (select private.is_system()) then
        raise exception 'not permitted';
    end if;
    delete from public.audit_log where at < p_before;
    get diagnostics n = row_count;
    return n;
end
$$;
revoke all on function private.purge_audit_log(text) from public, anon, authenticated;
grant execute on function private.purge_audit_log(text) to nyaya_app;

insert into public.meta(key, value) values ('schema_version', '7')
on conflict (key) do update set value = excluded.value;
