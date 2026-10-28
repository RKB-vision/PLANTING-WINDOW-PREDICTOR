# Plant Readiness Countdown Prototype (species-aware, with 14-day feedback)

This project estimates **days remaining until a plant reaches its
transplant-ready stage**, using recorded **readiness events** as labels,
with a species-aware model fallback order and a 14-day post-transplant
feedback loop.

> **Honesty rules built into this code**
>
> - The historical Super Orange growth CSV has **no verified readiness dates**
>   and is not used as supervised countdown training data. Real models are
>   trained from recorded transplant/readiness events, such as the Marigold
>   feedback labels stored in the database.
> - Production predictions select validated real-data models only. The
>   synthetic demo workflow is separate from the production API and UI.
> - The old workflow (Grok API criteria -> hardcoded rule labels ->
>   classifier "accuracy" against its own rules) has been removed.

## Files

| File | Purpose |
|---|---|
| `model_train.py` | Training entry point for real species data; `--retrain` evaluates and conditionally promotes a new real candidate. `--mode demo` remains isolated for development. |
| `prediction.py` | Real-data prediction entry point: species-aware model selection and `--json`. |
| `plant_feedback.py` | CLI workflow: register, observe, predict, transplant, due reminders, submit feedback, history, species-status, list. |
| `demo_workflow.py` | Scripted end-to-end SYNTHETIC demonstration (steps 0-6, including retrain comparison). |
| `plantreadiness/db.py` | SQLite schema + validation + duplicate protection + date/growth arithmetic. |
| `plantreadiness/repository.py` | Repository layer: `SQLiteRepository` (local storage). UI/CLI never embed SQL. |
| `plantreadiness/services.py` | Shared business logic used by BOTH the CLI and the web app (register, observe, transplant, feedback, corrections, predictions, CSV import/export, dashboard summary). |
| `plantreadiness/csv_import.py` | Real CSV import (append-only, idempotent) + data-quality checks + growth analysis. |
| `plantreadiness/synthetic.py` | Reproducible synthetic generator (documented assumptions, not botanical facts). |
| `plantreadiness/model.py` | Countdown training, grouped validation, artefacts + metadata, fallback selection, retrain gate. |
| `plantreadiness/config.py` | Constants: paths, minimum data requirements, standard messages. |
| `nextjs/` | Next.js front‑end: species-level dashboard, API proxy routes to the Flask backend, green plant UI. |
| `tests/` | Automated tests (dates, growth, duplicates, missing data, fallback, separation, integration, repository, services, camera refresh). |
| `backend/` | Flask API used by the Next.js front-end (species catalog, species prediction, camera refresh, legacy plant prediction). |
| `plantreadiness/camera_refresh.py` | Auto-refresh: pulls new Cloudinary images, measures them with the vision pipeline and imports the observations (append-only, fail-open). |
| `AGENTS.md` | Read-first orientation for contributors; links to the canonical architecture. |
| `docs/ARCHITECTURE.md` | Canonical architecture (purpose, data flow, non-negotiable principles). |
| `docs/DEPLOYMENT.md` | Running the app locally: prerequisites, backend + front-end, local storage, backup/export. |
| `docs/REPORT.md` | Limitations report: what is demonstrated only with synthetic data. |

The ESP32-CAM / Cloudinary / OpenCV measurement pipeline
(`plant_measurement.py` -> `data/generated/plant_daily_measurements.csv`) is
extended with a
bounded, fail-open **camera refresh** (`plantreadiness/camera_refresh.py`) that
the backend runs before a species prediction. It stays single-pass and
append-only, and never invents growth.

## Camera refresh (ESP32-CAM -> Cloudinary)

Prediction reads the database, so live camera measurements must be imported
first. The backend does this automatically **before a species prediction** with
a single-pass, fail-open refresh (`plantreadiness/camera_refresh.py`):

```text
ESP32-CAM -> Cloudinary -> OpenCV measure -> data/generated/plant_daily_measurements.csv -> DB
```

It is a no-op until configured. Register each camera plant first (explicit
registration - camera images are never auto-assigned to a guessed species),
then put the variables in a `.env` at the project root (copy `.env.example` to
`.env`) or export them:

| Variable | Purpose |
|---|---|
| `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Server-side Cloudinary admin credentials (never on the ESP32). |
| `CAMERA_SOURCES` | `Prefix=external_ref` pairs, e.g. `Plant01_=Plant-01`. |
| `CAMERA_ROD_LENGTH_CM` | Reference-rod length (cm) for pixel->cm calibration. **Measure it**; the configured value stays UNVERIFIED until `python -m plantreadiness.verify_calibration` records it. Default `30` is only a documented fallback, not an assumed-correct value. |
| `CAMERA_REFRESH_ENABLED` | Optional override; otherwise enabled when credentials + sources exist. |
| `CAMERA_MIN_REFRESH_INTERVAL_SECONDS` | Throttle between automatic refreshes (default `60`). |

Manual refresh: `POST /api/camera/refresh` (optionally `{"species": "..."}`).

## Setup

```bash
git clone https://github.com/RKB-vision/PLANTING-WINDOW-PREDICTOR.git
cd PLANTING-WINDOW-PREDICTOR
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd nextjs && npm install && cd ..

cp .env.example .env              # then add your Cloudinary credentials
python -m plantreadiness.seed     # create the local database + reference baseline
```

`plant_readiness.db` and `models/` are rebuilt from the committed sources by
`python -m plantreadiness.seed`; nothing sensitive and no runtime database is
committed. See *Seeding the database* below.

## Quick start - one command

```bash
./run.sh            # installs missing Python/Node dependencies, starts Flask
                    # + Next.js, then opens the dashboard at http://localhost:3000
```

The script:

- installs Flask if it is missing from the active environment,
- installs the Next.js dependencies on first run,
- starts the Flask API in the background,
- starts the Next.js development server,
- cleans up the Flask API when the UI exits.

```bash
PORT=3001 ./run.sh       # change the Next.js UI port
PYTHON_BACKEND_URL=http://localhost:8000 ./run.sh
                          # explicit Flask URL (default)
```

If you want to run the API and UI separately:

```bash
# terminal 1 (use the project environment that has rembg + its CPU runtime)
PYTHONPATH=$(pwd) FLASK_APP=backend/app.py .venv/bin/python -m flask run --port 8000

# terminal 2
cd nextjs
npm install
npm run dev
```

## Quick start (manual / CLI)

## Quick start

### Mode A - synthetic demonstration (complete workflow)

```bash
# 1) train the demo models on generated synthetic cohorts
python model_train.py --mode demo

# 2) scripted end-to-end demo: register -> predict -> transplant ->
#    14-day feedback -> retrain -> compare on held-out synthetic plants
python demo_workflow.py

# 3) retrain later: new cohort, candidate vs deployed model on the SAME
#    held-out set (replaces only if strictly better)
python model_train.py --mode demo --retrain
```

Demo mode writes to its **own scratch locations** - `plant_readiness_demo.db`
and `models_demo/`, both git-ignored - so a synthetic run can never put
simulated plants into the real database or churn the committed `models/`
artefacts. Real mode uses `plant_readiness.db` and `models/`. Pass `--db` /
`--models-dir` to override either.

### Mode B - real data (species scoped)

```bash
# Train the configured default species and import its default CSV
python model_train.py --mode real --csv data/super_orange_growth_data.csv

# Train Marigold from readiness events already stored in plant_readiness.db
# (does not import the Super Orange CSV into Marigold)
python model_train.py --mode real --species "Marigold (Apsara Yellow)"

# Refit the deployed Marigold model from all accumulated real readiness labels
# and promote only if the grouped-CV checks support the update
python model_train.py --mode real --species "Marigold (Apsara Yellow)" --retrain
```

Only an **actual transplant** recorded in Feedback creates a readiness-date
training label. A readiness assessment and the later 14-day follow-up are
stored separately. Training remains manual; the command requires enough
independent labelled plants and uses grouped validation. `--retrain` fits a
fresh candidate from all accumulated labels and compares the old model type
and candidate on the same plant-grouped CV folds. A same-type refresh is
promoted only with additional evidence, a validated candidate, and no increase
in CV error. A different model type must strictly lower CV error. Older model
files remain available in the registry; the previous species-scoped artifact
is retired only after promotion. Predictions use a real model only when its
validation metadata marks it as validated.

### 14-day feedback workflow

```bash
# register / list / measure
python plant_feedback.py register --species "Super Orange" --variety Early \
    --date-started 2083-04-24 --ref PLANT-11
python plant_feedback.py list
python plant_feedback.py observe --plant 1 --date 2083-05-20 \
    --age-days 26 --height 5.5 --growth-rate 0.3

# real-data prediction (reports insufficient evidence when no validated model exists)
python plant_feedback.py predict --plant 1

# transplant -> feedback becomes due 14 days later
python plant_feedback.py transplant --plant 1 --date 2083-05-25 --height 6.4

# pending/overdue reminders are also printed every time the tool opens
python plant_feedback.py due

# submit the 14-day outcome (dead plant: drop --height-after, growth stays NULL)
python plant_feedback.py submit --feedback 1 --submitted-date 2083-06-08 \
    --survived 1 --height-after 7.6 --condition healthy --notes "strong growth"

# inspect history / how many usable readiness events exist for a species
python plant_feedback.py history --plant 1
python plant_feedback.py species-status --species "Super Orange"
```

### Prediction directly

```bash
python prediction.py --plant 1                      # refuses honestly today
python prediction.py --species "Super Orange" --variety Early \
    --age-days 20 --height-cm 4.2 --growth-rate 0.3 --json
```

## Model fallback order (species-aware)

1. **Validated species/variety model** (real labelled data),
2. **Broader real model** for the species (uncertainty + limitations shown),
3. Otherwise: `Readiness prediction unavailable: verified readiness-event
   data is not yet sufficient.` (the system is collecting data).

A model is only eligible if it is marked `validated`: minimum
`4` independent plants and `6` labelled readiness events
(`plantreadiness/config.py`), trained with plant-grouped GroupKFold
validation and reported MAE in days against a mean-baseline.

## Database

The CLI uses SQLite (`plant_readiness.db`, created on first use) with tables
`plants`, `observations`, `readiness_events`, `feedback_14d`,
`prediction_history` and `readiness_assessments`:
primary/foreign keys, CHECK constraints, duplicate protection
(one observation per plant per date, one feedback record per transplant,
feedback is immutable after submission), and `data_source` on every row to
keep `real` and `synthetic` separated. Historical observations, feedback and
prediction snapshots are never overwritten. New columns/tables are added by a
lightweight forward-migration in `db.init_db`, so an existing database keeps
working without a reset.

Storage is **local SQLite on this device** (`plant_readiness.db`), reached only
through the repository layer, so neither the CLI nor the UI embeds SQL. There
is no cloud database dependency.

### Seeding the database (collaborators)

`plant_readiness.db` is runtime state - it changes with every measurement or
feedback submission - so it is **not** committed. Rebuild the shared starting
point from the committed sources with:

```bash
python -m plantreadiness.seed            # text report
python -m plantreadiness.seed --json     # machine-readable report
```

The seed creates the schema, imports the historical cohort
(`data/super_orange_growth_data.csv`, tagged `cohort='reference'`), and rebuilds the
successful-cohort reference baseline under `models/reference/`. It is
**idempotent** and never invents data: re-running imports nothing new and
regenerates the same medians. Camera plants are never part of the seed - they
are always registered explicitly (see *Camera refresh*).

### Resetting the database

The seed is *additive*: it never removes data created afterwards (camera
plants, manual experiments, test fixtures). To wipe the runtime database and
rebuild it **purely from the committed CSV**, use `./reset`:

```bash
./reset                      # back up the old DB, then rebuild from the CSV
./reset --rebuild-baseline   # also force-rebuild the reference baseline
./reset --no-backup          # skip the safety backup
./reset --camera             # also clear untracked camera artefacts
```

It writes a timestamped copy of the previous database
(`plant_readiness.db.bak-<timestamp>`, git-ignored) before deleting anything, and
**refuses to run if the CSV is missing** so a wipe can never destroy data that
cannot be rebuilt. Missing values stay missing and no readiness events are
invented. Restart the API/UI (`./run.sh`) afterwards so it reopens the database.

### Removing synthetic test data

Experiments belong in a scratch database (the demo tools use one automatically).
If synthetic rows do reach a database, they can be removed without touching a
single real record - synthetic data is always tagged `data_source='synthetic'`:

```bash
python -m plantreadiness.purge            # dry run: show what would be deleted
python -m plantreadiness.purge --yes      # delete the synthetic rows
```

It removes synthetic plants, observations, readiness events, feedback,
predictions and assessments, and **never** deletes `data_source='real'` rows:
real history is append-only (see `AGENTS.md`). If an experiment would create
*real* records, point it at a separate `--db` rather than deleting history
afterwards.

## Web app (Next.js + Flask API)

The main front-end is a **species-level dashboard**: choose a species, use the
latest cloud observation date, and the backend aggregates the observations for
that species/date into a representative cohort.

```bash
cd nextjs
npm install
npm run dev
```

The Next.js app talks to the Flask backend in `backend/app.py`. Key endpoints:

- `GET /api/species`
- `POST /api/predict/species`
- `POST /predict` (legacy, plant-specific)
- `GET /api/reference` · `POST /api/reference/build` (successful-cohort baseline)
- `GET|POST /api/assessments` (readiness judgement, separate from prediction)
- `GET|POST /api/transplant` (transplant record, incl. "did not occur")
- `GET|POST /api/feedback` (pending / submit the 14-day follow-up)
- `GET /api/predictions` (append-only prediction history)
- `GET /api/evaluation` (predictions vs actual transplants)

The dashboard shows current cloud data, representative cohort metrics, real
readiness-event evidence, and an honest “prediction unavailable” state when
verified readiness labels are insufficient. When no model exists but a
successful-cohort reference baseline does, the species endpoint may show a
clearly-labelled **provisional** estimate instead. Synthetic predictions are
only shown when explicitly enabled and are always labelled as a synthetic demo.

A separate `/feedback` page records the readiness assessment, the transplant
record, the 14-day follow-up, and shows prediction history and the
feedback-driven evaluation.

### Reference baseline + feedback loop

- The **successful-cohort reference baseline** (`plantreadiness/reference.py`)
  summarises the historical Super Orange cohort: per-plant growth (calendar
  elapsed days, never invented), medians across independent plants by age and by
  days-before-transplant, and the contributing-plant count for every median.
- The historical cohort is imported with `cohort='reference'`. Operational
  screens (feedback) therefore list only the plants you actually monitor, while
  the cohort is still used to build the baseline. A plant ID is always one
  *individual* plant — several plants of the same species have different IDs.
- The transplant reference uses the **recorded** date when one exists, otherwise
  the day after the final monitoring day — explicitly flagged as an
  **assumption**. The historical date is never claimed to be optimal.
- The baseline is persisted under `models/reference/` and is separate from
  synthetic or future feedback records. It powers a **provisional** prediction
  path used only when no validated model is available and explicitly enabled.
- Feedback is read-only with respect to training: assessments, transplants and
  14-day outcomes are recorded, prediction snapshots are preserved append-only,
  and `evaluate_predictions` reports prediction error vs actual transplant date
  and 14-day survival without ever retraining or promoting a model.

## Tests

```bash
python -m unittest discover -s tests -v
```

Covers: date arithmetic (readiness target, +14-day due dates), growth
calculation (including missing/deceased plants), duplicate feedback and
observation protection, species fallback levels, real/synthetic data-source
separation, CSV import, and the end-to-end demo workflow; plus repository
duplicate/validation behaviour (local SQLite), the service layer, and the
camera refresh (explicit registration, idempotent import, fail-open). The
reference baseline,
provisional estimates, prediction snapshots, readiness assessments,
transplant records, feedback-driven evaluation (with no-future-leakage and a
worse-model safeguard) and the Flask feedback/reference API are covered by
`tests/test_reference.py`, `tests/test_feedback_learning.py` and
`tests/test_backend_api.py`.

## Retraining policy

Feedback is **saved and validated first**; it is not automatically turned
into readiness labels. Retraining only happens when minimum data requirements
are met and the candidate passes grouped validation. Real retraining compares
the current model type and candidate with the same grouped-CV folds; see Mode B
above for the promotion rules. Synthetic demo retraining retains its isolated
holdout workflow.

The real training command accepts `--species` to select which species' stored
readiness events are evaluated. Real retraining is opt-in with `--retrain` and
uses only real readiness-event data. Synthetic demo tools remain isolated from
the real database and are not part of the production prediction interface.
