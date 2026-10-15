# Deployment Guide - Plant Readiness Web App

This guide takes a fresh checkout to a working, isolated, multi-user
deployment. It covers Supabase setup, migrations, authentication, secrets,
local development, GitHub, Streamlit Community Cloud, production secrets,
a two-account isolation test, and backup/export.

Everything here uses the **anon (public) key only**. There is no
service-role key anywhere in this repository, and the app refuses to start
if one is supplied (see [Production secrets](#6-production-secrets)).

---

## 1. What gets deployed

| Piece | Where it runs | Notes |
|---|---|---|
| Streamlit UI (`webapp/`) | Streamlit Community Cloud or any host running `streamlit run webapp/app.py` | Reads server-side secrets; no secrets reach the browser |
| Database | Supabase (PostgreSQL) | Schema from `supabase/migrations/`, RLS in `0002_row_level_security.sql` |
| Authentication | Supabase Auth (email + password) | Sessions carry the user JWT; RLS scopes every query |
| Model artefacts | Filesystem (`models/`), gitignored | Trained locally; the dashboard reports "no artefacts" honestly when absent |
| CLI (`prediction.py`, `plant_feedback.py`) | Local machine | Unchanged; reads/writes the SQLite prototype database |

The CLI prototype keeps working untouched: it uses `plant_readiness.db`
(SQLite) via the same service functions the web app calls through its
repository layer.

---

## 2. Supabase project setup

1. Create a project at <https://supabase.com> (free tier is fine).
2. Note two values from **Project Settings > Data API** (formerly *API*).
   - **Project URL**: `https://<PROJECT_REF>.supabase.co`
   - **anon public key**: starts with `eyJ...`; safe to publish because RLS
     protects the data.
3. Do **not** copy the service-role key. It will not be used, and putting
   it in the app is actively refused.

### Apply the migrations

**Option A - Supabase CLI (versioned, repeatable):**

```bash
npm install -g supabase          # once
supabase login                   # once
supabase link --project-ref <PROJECT_REF>

# applies supabase/migrations/0001..., 0002... in order
supabase db push
```

**Option B - SQL editor (no CLI):** open **SQL Editor** in the dashboard
and run the files in order, top to bottom:

1. `supabase/migrations/0001_initial_schema.sql` - tables, keys, FKs,
   unique constraints, `updated_at` triggers, signup trigger.
2. `supabase/migrations/0002_row_level_security.sql` - enables RLS on all
   eight tables and installs per-user (`user_id = auth.uid()`) policies for
   SELECT / INSERT / UPDATE / DELETE.

Both files are idempotent (`create table if not exists`,
`drop policy if exists ... create policy`), so re-running is safe.

> **Why no `CHECK (user_id = auth.uid())`?** PostgreSQL requires functions
> in column CHECK constraints to be IMMUTABLE, and `auth.uid()` is STABLE.
> The RLS `WITH CHECK` policies are the enforcement layer instead.

### Enable email auth

**Authentication > Providers > Email** should be enabled (it is by default).
For quick local testing you may toggle "Confirm email" off; for production
leave it on.

---

## 3. Local secrets

Copy the template and fill in real values:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

`.streamlit/secrets.toml` is **gitignored**. Never commit it.

For Supabase:

```toml
backend = "supabase"

[supabase]
url = "https://YOUR_PROJECT_REF.supabase.co"
anon_key = "YOUR_ANON_PUBLIC_KEY"
```

For local development against the SQLite prototype database (single-user,
no auth - deliberately chosen, never a silent fallback):

```toml
backend = "sqlite"

[sqlite]
path = "./plant_readiness.db"
```

If secrets are missing or invalid, the app **stops and shows setup
instructions**. It will not fall back to a hidden in-memory, synthetic, or
wrong database.

---

## 4. Local run

```bash
./run.sh            # one command: creates .venv, installs deps, writes a
                    # local SQLite secrets file if none exists, initialises
                    # the database, then starts the web app on :8501
```

Other modes: `./run.sh demo` (train demo models first), `./run.sh test`
(run all tests), `./run.sh cli ...` (CLI passthrough), `./run.sh help`.

Manual equivalent:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

streamlit run webapp/app.py
```

Open <http://localhost:8501>. With `backend = "supabase"` you will see the
sign-up / sign-in screen; sign up with your own email to create the first
account.

### Run the tests

```bash
python -m unittest discover -s tests
```

The suite (207 tests) covers the domain logic, both repositories (Supabase
client fully mocked - no network), migration planning/apply/rerun safety,
secrets handling, and the Streamlit forms via `AppTest`.

---

## 5. GitHub

The repository is safe to push:

- `.gitignore` excludes `.streamlit/secrets.toml`, `*.db`, `models/`,
  `models_demo/`, `exports/`, `private/`, `*.private.csv`, `*.bak-*`,
  `supabase/.temp/`, `supabase/.branches/`.
- A static test (`tests/test_migration.py::
  test_no_service_role_key_anywhere_in_repo_sources`) fails the build if any
  file in `webapp/`, `plantreadiness/`, `scripts/`, `supabase/`,
  `.streamlit/`, or `tests/` contains a JWT whose payload claims a
  privileged role.
- Local databases (`plant_readiness.db`, `demo_workflow.db`) and trained
  artefacts (`models/`, `models_demo/`) are runtime data and were untracked;
  they remain on disk but are no longer committed.

```bash
git add -A
git commit -m "Add Streamlit + Supabase web app with RLS, migrations, tests"
git push
```

---

## 6. Streamlit Community Cloud + production secrets

1. Push the repository to GitHub.
2. Open <https://share.streamlit.io>, **Create app**, select the repo,
   branch, and `webapp/app.py` as the main file path.
3. In **App settings > Secrets**, paste the same content as your local
   `.streamlit/secrets.toml` (the `[supabase]` section with the project URL
   and anon key).
4. Deploy. The app boots, shows the auth screen, and every session is
   isolated by RLS.

### Production secret rules (enforced, not just documented)

- The **service-role key is rejected at settings load** with a loud error:
  `webapp/settings.py::privileged_key_reason` inspects the key's decoded
  JWT `role` claim and known markers. Only `anon` / `authenticated` /
  `public` roles are accepted.
- Secrets are server-side only. Streamlit never ships `st.secrets` to the
  browser, so the anon key (public by design) is the only key that could
  ever transit, and RLS - not key secrecy - protects the data.
- Rotate keys from the Supabase dashboard if a secret is ever exposed;
  rotation requires updating the secrets entry only.

---

## 7. Two-account isolation test (verify, don't assume)

RLS must be *proven*, not trusted. `scripts/verify_rls.py` writes rows as
one account and verifies a second account can neither read nor overwrite
them, using only the anon key and two ordinary user sessions:

```bash
python scripts/verify_rls.py \
  --url https://YOUR_PROJECT_REF.supabase.co \
  --anon-key YOUR_ANON_PUBLIC_KEY \
  --email-a alice@example.com --password-a 'alice-password' \
  --email-b bob@example.com   --password-b 'bob-password'
```

Exit code `0` = isolation verified; `1` = a policy is missing or broken.
Run it once after setup and again after any migration.

---

## 8. Migrating the SQLite prototype data

`scripts/migrate_sqlite_to_supabase.py` copies your local prototype
database into Supabase **as the destination user** (no service-role key;
RLS stamps `user_id` on every row).

```bash
# 1. Dry run - shows what would move, per real/synthetic source, and which
#    rows already exist in the target (idempotency). Writes nothing.
python scripts/migrate_sqlite_to_supabase.py \
  --sqlite ./plant_readiness.db \
  --url https://YOUR_PROJECT_REF.supabase.co \
  --anon-key YOUR_ANON_PUBLIC_KEY \
  --email you@example.com --password '...'

# 2. Apply - makes a timestamped .bak-<ts> backup of the SQLite file FIRST,
#    then inserts. Re-running skips rows already present (safe).
python scripts/migrate_sqlite_to_supabase.py ... --apply

# Synthetic demo rows are NOT migrated unless explicitly requested:
python scripts/migrate_sqlite_to_supabase.py ... --apply --include-synthetic
```

The report lists inserted / skipped / failed per table. Real and synthetic
rows stay distinguishable (`data_source` column) and are never merged.

---

## 9. Backup and export

**Database (Supabase):** Dashboard > **Database > Backups** for automatic
daily backups on paid tiers, or create a manual backup before bulk
operations. Point-in-time recovery is available on Pro.

**App-level export (any backend):** the **Species & data** page exports any
table as CSV (Plants, Observations, Readiness events, Transplants,
Feedback, Corrections). Downloads are per-user - RLS means you can only
export your own rows. Store exports outside the repo (`exports/` and
`*.private.csv` are gitignored).

**SQLite backup (local CLI data):** the migration script itself makes a
`<name>.bak-<timestamp>` copy before writing; you can also copy the file
directly while the CLI is not running.

---

## 10. Operations cheat-sheet

| Task | Command / action |
|---|---|
| Start locally | `streamlit run webapp/app.py` |
| Run all tests | `python -m unittest discover -s tests` |
| Apply migrations | `supabase db push` (or SQL editor, in order) |
| Verify RLS | `python scripts/verify_rls.py ...` |
| Migrate SQLite data | `python scripts/migrate_sqlite_to_supabase.py ... --apply` |
| Export data | Web app > Species & data > Download CSV |
| Add a user | Web app sign-up screen (or Supabase dashboard) |
| Rotate keys | Supabase dashboard > Project Settings > API; update secrets |

### Troubleshooting

- **"Database not configured" screen** - expected when secrets are absent
  or contain placeholders; follow the on-screen steps.
- **"This looks like a SERVICE-ROLE key"** - you pasted the wrong key;
  use the anon/public key.
- **Permission denied / empty results after migrating** - you are probably
  signed in as a different account than the one used for the migration.
  Sign in as the destination user, or re-check with `verify_rls.py`.
- **Prediction unavailable message** - by design: readiness prediction
  needs real verified readiness-event data (>= 6 events across >= 4
  plants per species). The app never fabricates labels for real plants.
