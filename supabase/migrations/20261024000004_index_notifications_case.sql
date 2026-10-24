-- Performance advisor 0001: deleting a case cascades to notifications(case_id); index the foreign key.
create index if not exists ix_notifications_case on public.notifications(case_id);
