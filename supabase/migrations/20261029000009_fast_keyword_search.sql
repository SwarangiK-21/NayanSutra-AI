-- NyaySutra AI — fast keyword search under row-level security.
--
-- Why: with RLS on chunks/documents, Postgres must evaluate the row policies before any non-leakproof condition,
-- and the full-text match operator (@@) is not leakproof. So every keyword query scanned all ~62,000 passages
-- (the library + the Supreme Court catalogue) and took ~11 s, instead of using the GIN index (~0.1 s).
--
-- What: private.fts_chunk_ids() runs the keyword match with the owner's rights, so the GIN index is used, and applies
-- the SAME access rule as the documents/chunks policies inside the function:
--   * library documents (collection <> 'case'): readable by every app user;
--   * case documents: only for the case ids passed in AND accessible to the caller (private.accessible_case_ids()),
--     or for system work (private.is_system()).
-- It returns passage ids only; the app then reads those rows normally, under RLS. Callable only by nyaya_app.
-- Idempotent.

create or replace function private.fts_chunk_ids(
    p_filter text,               -- tsquery used to select passages (index-friendly, e.g. "(a & b) | (a & c)")
    p_rank text,                 -- tsquery used to rank them (e.g. "a | b | c")
    p_collections text[],        -- allowed collections, e.g. {bare_act,judgment,draft} or {case}
    p_case_ids text[],           -- case documents considered (still checked against the caller's access)
    p_doc_ids text[],            -- optional: only these documents
    p_jurisdiction text,         -- optional: central law + this state's acts
    p_limit integer
) returns table(id bigint)
language sql stable security definer set search_path = '' as $$
    select c.id
    from public.chunks c
    join public.documents d on d.id = c.doc_id,
         to_tsquery('english', p_filter) f,
         to_tsquery('english', p_rank) q
    where c.fts @@ f
      and d.status = 'ready'
      and d.collection = any (p_collections)
      and (d.collection <> 'case'
           or (d.case_id = any (coalesce(p_case_ids, '{}'))
               and ((select private.is_system())
                    or d.case_id in (select private.accessible_case_ids()))))
      and (p_doc_ids is null or d.id = any (p_doc_ids))
      and (p_jurisdiction is null or d.collection <> 'bare_act' or d.jurisdiction in ('', 'Central', p_jurisdiction))
    order by ts_rank(c.fts, q) desc
    limit least(greatest(p_limit, 1), 200)
$$;

revoke all on function private.fts_chunk_ids(text, text, text[], text[], text[], text, integer) from public, anon, authenticated;
grant execute on function private.fts_chunk_ids(text, text, text[], text[], text[], text, integer) to nyaya_app;

insert into public.meta(key, value) values ('schema_version', '6')
on conflict (key) do update set value = excluded.value;
