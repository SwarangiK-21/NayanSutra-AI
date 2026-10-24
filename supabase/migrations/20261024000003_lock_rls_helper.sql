-- Some projects are created with the event-trigger helper public.rls_auto_enable() (auto-enables RLS on new
-- tables). It was callable through the Data API by anon/authenticated (Supabase advisor 0028/0029). Event triggers
-- do not need EXECUTE for client roles, so the direct call path is closed; the trigger keeps working.
-- Conditional: projects created without the helper are unaffected.
do $$
begin
    if exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
               where n.nspname = 'public' and p.proname = 'rls_auto_enable') then
        revoke execute on function public.rls_auto_enable() from public, anon, authenticated;
    end if;
end $$;
