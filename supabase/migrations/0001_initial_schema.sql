-- =====================================================================
-- 0001_initial_schema.sql
-- Plant readiness web app - initial schema (Supabase / PostgreSQL)
--
-- Mirrors the SQLite prototype's semantics:
--   * primary keys, foreign keys, CHECK constraints, UNIQUE duplicate
--     protection on every history table,
--   * append-only history: observations / readiness / feedback rows are
--     never deleted by the app (corrections go to feedback_corrections),
--   * every row carries data_source ('real' | 'synthetic') so the two
--     worlds can never mix,
--   * every owned row carries user_id for per-user Row Level Security.
--
-- Run via:  supabase db push   (or paste into the SQL editor, in order).
-- =====================================================================

-- ------------------------------------------------------------------
-- 1. User profiles (1:1 with auth.users)
-- ------------------------------------------------------------------
create table if not exists public.user_profiles (
    user_id      uuid primary key references auth.users(id) on delete cascade,
    display_name text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

-- Auto-create a profile row whenever an auth user is created.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.user_profiles (user_id, display_name)
    values (new.id, split_part(new.email, '@', 1))
    on conflict (user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- ------------------------------------------------------------------
-- 2. Plants
-- ------------------------------------------------------------------
create table if not exists public.plants (
    plant_id           bigint generated always as identity primary key,
    user_id            uuid not null references auth.users(id) on delete cascade,
    species            text not null check (length(trim(species)) > 0),
    variety            text,
    date_started       date,
    data_source        text not null check (data_source in ('real', 'synthetic')),
    growing_conditions text,
    external_ref       text,
    cohort             text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

-- Duplicate protection (NULL-aware: Postgres UNIQUE treats NULLs as
-- distinct, so COALESCE folds a missing ref/variety into '').
create unique index if not exists plants_unique_identity
    on public.plants (user_id, data_source, species,
                      coalesce(variety, ''), coalesce(external_ref, ''));
create index if not exists plants_species_idx
    on public.plants (species, variety);

-- ------------------------------------------------------------------
-- 3. Observations (append-only; one per plant per date)
-- ------------------------------------------------------------------
create table if not exists public.observations (
    observation_id        bigint generated always as identity primary key,
    user_id               uuid not null references auth.users(id) on delete cascade,
    plant_id              bigint not null references public.plants(plant_id) on delete cascade,
    observation_date      date not null,
    age_days              integer check (age_days is null or age_days >= 0),
    height_cm             double precision check (height_cm is null or height_cm >= 0),
    -- growth rate may legitimately be negative (measurement noise)
    growth_rate_cm_per_day double precision,
    canopy_width_cm       double precision check (canopy_width_cm is null or canopy_width_cm >= 0),
    visible_true_leaf_count integer check (visible_true_leaf_count is null or visible_true_leaf_count >= 0),
    data_source           text not null check (data_source in ('real', 'synthetic')),
    recorded_at           timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    unique (plant_id, observation_date)
);
create index if not exists observations_plant_date_idx
    on public.observations (plant_id, observation_date);

-- ------------------------------------------------------------------
-- 4. Readiness events (the ONLY source of readiness labels)
-- ------------------------------------------------------------------
create table if not exists public.readiness_events (
    readiness_event_id         bigint generated always as identity primary key,
    user_id                    uuid not null references auth.users(id) on delete cascade,
    plant_id                   bigint not null references public.plants(plant_id) on delete cascade,
    readiness_observed_date    date not null,
    readiness_observed_age_days integer check (readiness_observed_age_days is null
                                               or readiness_observed_age_days >= 0),
    readiness_definition_version text not null check (length(trim(readiness_definition_version)) > 0),
    assessment_source          text not null check (length(trim(assessment_source)) > 0),
    data_source                text not null check (data_source in ('real', 'synthetic')),
    notes                      text,
    recorded_at                timestamptz not null default now(),
    unique (plant_id, readiness_definition_version)
);

-- ------------------------------------------------------------------
-- 5. Transplant events (separate table, task section 4)
-- ------------------------------------------------------------------
create table if not exists public.transplant_events (
    transplant_event_id       bigint generated always as identity primary key,
    user_id                   uuid not null references auth.users(id) on delete cascade,
    plant_id                  bigint not null references public.plants(plant_id) on delete cascade,
    transplant_date           date not null,
    height_at_transplant_cm   double precision
                              check (height_at_transplant_cm is null
                                     or height_at_transplant_cm >= 0),
    data_source               text not null check (data_source in ('real', 'synthetic')),
    created_at                timestamptz not null default now(),
    updated_at                timestamptz not null default now(),
    -- duplicate protection: one transplant per plant per date
    unique (plant_id, transplant_date)
);

-- ------------------------------------------------------------------
-- 6. Fourteen-day feedback (one per transplant event)
-- ------------------------------------------------------------------
create table if not exists public.feedback_14d (
    feedback_id             bigint generated always as identity primary key,
    user_id                 uuid not null references auth.users(id) on delete cascade,
    plant_id                bigint not null references public.plants(plant_id) on delete cascade,
    transplant_event_id     bigint not null unique
                            references public.transplant_events(transplant_event_id) on delete cascade,
    feedback_due_date       date not null,
    feedback_submitted_date date,
    survived_after_14d      smallint check (survived_after_14d is null
                                            or survived_after_14d in (0, 1)),
    height_after_14d_cm     double precision check (height_after_14d_cm is null
                                                    or height_after_14d_cm >= 0),
    growth_after_14d_cm     double precision,  -- NULL when not measurable
    visible_condition       text check (visible_condition is null
                                        or visible_condition in
                                           ('healthy', 'stressed_wilting', 'dead')),
    user_notes              text,
    data_source             text not null check (data_source in ('real', 'synthetic')),
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);
create index if not exists feedback_due_idx
    on public.feedback_14d (feedback_due_date, feedback_submitted_date);

-- ------------------------------------------------------------------
-- 7. Feedback corrections (audit history for explicit edits)
-- ------------------------------------------------------------------
create table if not exists public.feedback_corrections (
    correction_id   bigint generated always as identity primary key,
    user_id         uuid not null references auth.users(id) on delete cascade,
    feedback_id     bigint not null references public.feedback_14d(feedback_id) on delete cascade,
    previous_values jsonb not null,
    reason          text not null check (length(trim(reason)) > 0),
    corrected_at    timestamptz not null default now()
);
create index if not exists feedback_corrections_feedback_idx
    on public.feedback_corrections (feedback_id, corrected_at);

-- ------------------------------------------------------------------
-- 8. Model metadata (artefact registry mirrored for the dashboard)
-- ------------------------------------------------------------------
create table if not exists public.model_metadata (
    model_metadata_id bigint generated always as identity primary key,
    user_id           uuid not null references auth.users(id) on delete cascade,
    model_name        text not null,
    data_source       text not null check (data_source in ('real', 'synthetic')),
    scope_species     text,
    scope_variety     text,
    validated         boolean not null default false,
    metrics           jsonb,
    artifact_path     text,
    trained_at        timestamptz,
    created_at        timestamptz not null default now(),
    unique (user_id, model_name)
);

-- ------------------------------------------------------------------
-- updated_at maintenance
-- ------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

do $$
declare
    t text;
begin
    foreach t in array array['user_profiles', 'plants', 'observations',
                             'transplant_events', 'feedback_14d']
    loop
        execute format(
            'drop trigger if exists set_updated_at_%I on public.%I', t, t);
        execute format(
            'create trigger set_updated_at_%I before update on public.%I
             for each row execute function public.set_updated_at()', t, t);
    end loop;
end;
$$;
