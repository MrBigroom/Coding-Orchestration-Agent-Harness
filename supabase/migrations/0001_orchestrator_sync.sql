-- Coding Orchestrator: local-first, multi-device synchronization envelopes.
-- Apply only to a dedicated Supabase project. No existing application tables are changed.

create table public.orchestrator_sync_records (
    user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
    entity_type text not null,
    entity_id uuid not null,
    version bigint not null check (version > 0),
    payload jsonb not null,
    origin_device text not null,
    device_modified_at timestamptz not null,
    server_updated_at timestamptz not null default now(),
    primary key (user_id, entity_type, entity_id)
);

create index orchestrator_sync_records_pull_idx
    on public.orchestrator_sync_records (user_id, server_updated_at, entity_type, entity_id);

alter table public.orchestrator_sync_records enable row level security;

create policy "Users can read their own orchestrator records"
    on public.orchestrator_sync_records for select
    to authenticated
    using ((select auth.uid()) = user_id);

create policy "Users can insert their own orchestrator records"
    on public.orchestrator_sync_records for insert
    to authenticated
    with check ((select auth.uid()) = user_id);

create policy "Users can update their own orchestrator records"
    on public.orchestrator_sync_records for update
    to authenticated
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

grant select, insert, update on public.orchestrator_sync_records to authenticated;

-- Atomic compare-and-swap push. expected_version is the last version observed by the
-- device (0 for a new record). A concurrent remote edit returns a conflict instead of
-- being overwritten.
create or replace function public.orchestrator_sync_push(
    p_entity_type text,
    p_entity_id uuid,
    p_expected_version bigint,
    p_version bigint,
    p_payload jsonb,
    p_origin_device text,
    p_device_modified_at timestamptz
)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    result public.orchestrator_sync_records%ROWTYPE;
begin
    if auth.uid() is null then
        raise exception 'authentication required';
    end if;
    if p_expected_version < 0 or p_version != p_expected_version + 1 then
        raise exception 'invalid version transition: % -> %', p_expected_version, p_version;
    end if;

    insert into public.orchestrator_sync_records (
        user_id, entity_type, entity_id, version, payload,
        origin_device, device_modified_at
    ) values (
        auth.uid(), p_entity_type, p_entity_id, p_version, p_payload,
        p_origin_device, p_device_modified_at
    )
    on conflict (user_id, entity_type, entity_id) do nothing
    returning * into result;

    if found then
        return jsonb_build_object('status', 'ok', 'record', to_jsonb(result));
    end if;

    update public.orchestrator_sync_records
    set version = p_version,
        payload = p_payload,
        origin_device = p_origin_device,
        device_modified_at = p_device_modified_at,
        server_updated_at = now()
    where user_id = auth.uid()
      and entity_type = p_entity_type
      and entity_id = p_entity_id
      and version = p_expected_version
    returning * into result;

    if found then
        return jsonb_build_object('status', 'ok', 'record', to_jsonb(result));
    end if;

    select * into result
    from public.orchestrator_sync_records
    where user_id = auth.uid()
      and entity_type = p_entity_type
      and entity_id = p_entity_id;

    return jsonb_build_object('status', 'conflict', 'record', to_jsonb(result));
end;
$$;

revoke all on function public.orchestrator_sync_push(
    text, uuid, bigint, bigint, jsonb, text, timestamptz
) from public, anon;

grant execute on function public.orchestrator_sync_push(
    text, uuid, bigint, bigint, jsonb, text, timestamptz
) to authenticated;
