-- =====================================================================
-- 0002_row_level_security.sql
-- Per-user Row Level Security for every table in the plant-readiness app.
--
-- The app signs in with Supabase Auth and uses the ANON key only: there is
-- no service-role key anywhere in the codebase. These policies are the
-- enforcement layer that guarantees one account can never read or write
-- another account's plants, observations, readiness events, transplants,
-- feedback or model metadata.
--
-- Verified (not assumed) by:
--   * scripts/verify_rls.py  - live two-account isolation check, and
--   * tests/test_supabase_repository.py - repository always scopes by
--     user_id and maps 42501 policy violations to DatabaseError.
-- =====================================================================

alter table public.user_profiles     enable row level security;
alter table public.plants            enable row level security;
alter table public.observations      enable row level security;
alter table public.readiness_events  enable row level security;
alter table public.transplant_events enable row level security;
alter table public.feedback_14d      enable row level security;
alter table public.feedback_corrections enable row level security;
alter table public.model_metadata    enable row level security;

-- ------------------------------------------------------------------
-- user_profiles: a user manages only the row keyed by their own id.
-- ------------------------------------------------------------------
drop policy if exists "own profile select" on public.user_profiles;
create policy "own profile select" on public.user_profiles
    for select using (user_id = auth.uid());

drop policy if exists "own profile insert" on public.user_profiles;
create policy "own profile insert" on public.user_profiles
    for insert with check (user_id = auth.uid());

drop policy if exists "own profile update" on public.user_profiles;
create policy "own profile update" on public.user_profiles
    for update using (user_id = auth.uid())
    with check (user_id = auth.uid());

drop policy if exists "own profile delete" on public.user_profiles;
create policy "own profile delete" on public.user_profiles
    for delete using (user_id = auth.uid());

-- ------------------------------------------------------------------
-- Helper: identical SELECT / INSERT / UPDATE / DELETE policy set for
-- every ownership-scoped table. Generated explicitly so each policy is
-- named and reviewable in the schema.
-- ------------------------------------------------------------------
do $$
declare
    tbl text;
    col text;
begin
    foreach tbl in array array['plants', 'observations', 'readiness_events',
                               'transplant_events', 'feedback_14d',
                               'feedback_corrections', 'model_metadata']
    loop
        execute format(
            'drop policy if exists "%1$s own rows" on public.%I',
            tbl, tbl);
        execute format(
            'create policy "%1$s own rows" on public.%I
             for select using (user_id = auth.uid())', tbl, tbl);

        execute format(
            'drop policy if exists "%1$s own rows insert" on public.%I',
            tbl, tbl);
        execute format(
            'create policy "%1$s own rows insert" on public.%I
             for insert with check (user_id = auth.uid())', tbl, tbl);

        execute format(
            'drop policy if exists "%1$s own rows update" on public.%I',
            tbl, tbl);
        execute format(
            'create policy "%1$s own rows update" on public.%I
             for update using (user_id = auth.uid())
             with check (user_id = auth.uid())', tbl, tbl);

        execute format(
            'drop policy if exists "%1$s own rows delete" on public.%I',
            tbl, tbl);
        execute format(
            'create policy "%1$s own rows delete" on public.%I
             for delete using (user_id = auth.uid())', tbl, tbl);
    end loop;
end;
$$;

-- ------------------------------------------------------------------
-- Defence in depth note:
-- The WITH CHECK clauses above already guarantee a row can never be
-- written with another user's user_id (that is exactly what
-- "for insert with check (user_id = auth.uid())" enforces), and RLS
-- cannot be bypassed by application code using the anon key.
--
-- We deliberately do NOT add a CHECK (user_id = auth.uid()) column
-- constraint: PostgreSQL requires functions in CHECK constraints to be
-- IMMUTABLE, and auth.uid() is STABLE, so such a constraint would be
-- rejected at DDL time. The RLS policies are the enforcement layer.
-- ------------------------------------------------------------------
comment on policy "plants own rows" on public.plants is
    'RLS: users see only their own plants (verified by scripts/verify_rls.py).';
