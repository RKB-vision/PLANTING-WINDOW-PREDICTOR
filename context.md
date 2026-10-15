# Project Context – Plant Readiness System

## High‑level goal
The repository implements a **plant‑readiness prediction platform**.  Users register plants, record growth observations, and (eventually) obtain a predicted transplant‑readiness date based on a machine‑learning model.  The system also supports a feedback loop (14‑day transplant outcome) for model evaluation.

## Core repository layout (relevant to the task)
```
EMBEDDED/
├─ plantreadiness/                # Core domain logic, services, repository, config
│   ├─ __init__.py
│   ├─ repository.py             # DB abstraction (Supabase or local SQLite)
│   ├─ services.py                # Business logic (growth, age, model selection)
│   └─ config.py                  # Settings, secrets handling
│
├─ webapp/                        # Streamlit front‑end
│   ├─ app.py                    # Entry point, sidebar navigation, DB init
│   ├─ app_pages/                 # **Renamed** from legacy `pages/` to avoid
│   │   ├─ __init__.py           #   Streamlit's auto‑discovered page list.
│   │   ├─ dashboard.py           #   Main overview page
│   │   ├─ register.py            #   Plant registration page
│   │   ├─ observation.py         #   Manual observation entry page
│   │   ├─ predictions.py        #   Prediction UI (model selection, display)
│   │   ├─ feedback.py            #   14‑day outcome feedback page
│   │   ├─ species_data.py       #   Evidence tables, model‑plan details
│   │   └─ export.py              #   CSV export page (if present)
│   ├─ components/                # Re‑usable UI components (error/info boxes)
│   └─ settings.py                # Settings handling (models_dir, secrets)
│
├─ scripts/                       # Migration, verification utilities
│   └─ migrate_sqlite_to_supabase.py
│
├─ plant_measurement.py          # ESP32‑CAM / OpenCV image processing script
├─ plant_daily_measurements.csv   # Output of the measurement script (sensor data)
├─ run.sh                         # Convenience wrapper (creates local secrets, runs DB init, launches app)
├─ tests/                         # 211 unit / integration tests (AppTest, DB fixtures, service tests)
│   └─ ...
├─ README.md
└─ DEPLOYMENT.md
```

## Recent changes (the context we have built so far)
1. **`species_data.py` bug fixed** – the `render()` function now passes the `models_dir` argument to `_species_tab()`, preventing a `NameError` and allowing the model‑selection panel to display.
2. **Sidebar navigation duplication resolved** – the legacy `webapp/pages/` directory caused Streamlit to auto‑generate a second, non‑functional page list in the sidebar.  The directory was renamed to `webapp/app_pages/` and the import in `app.py` was updated accordingly.  The stray auto‑discovered list is now gone, leaving only the intended radio‑button navigation.
3. **Test suite hardened** – `goto()` in `tests/test_webapp_forms.py` now asserts that no error box appears after navigating to any page, catching hidden crashes.  The Species & Data test also verifies that the “Model selection” panel renders.  All **211 tests pass** (`python -m unittest discover -s tests` → OK).
4. **Cache clean‑up** – stale `__pycache__` directories removed after the rename.
5. **App sanity check** – running `HEADLESS=1 PORT=8602 ./run.sh` returns HTTP 200 with no errors in the log.

## Key modules for the upcoming ESP32‑measurement integration
- **`plant_measurement.py`** – script that processes an image from the ESP32‑CAM, extracts a height measurement (and potentially other metrics) and writes a row to `plant_daily_measurements.csv`.  We need to verify its **CSV schema** (column names, data types, timestamp format).
- **`plant_daily_measurements.csv`** – the persistent sensor output file.  Existing code in the repo imports CSVs via the **Import measurements (CSV)** tab (`webapp/pages/species_data.py` → `_import_tab`).  That logic already validates columns and stores rows as observations using the `observation` service.
- **`plantreadiness/services.py`** – provides functions such as `svc.record_observation()`, `svc.calculate_age()`, `svc.growth_rate()`.  These are the canonical ways to create an observation record in the repository.
- **`plantreadiness/repository.py`** – driver for SQLite (local dev) or Supabase (cloud).  The observation table stores: `plant_id`, `date_observed`, `height_cm`, `age_days`, `growth_rate_cm_per_day`, `source` (e.g., “sensor”, “manual”).  The schema can be inspected by looking at migration scripts or the DB model definitions.
- **`webapp/pages/observation.py`** – UI for manually entering observations; mirrors the logic we will reuse for sensor‑import synchronisation.
- **`webapp/pages/species_data.py`** – already implements a CSV‑import UI; we can reuse or expose it as a “Sync sensor data” button.

## What the integration needs to achieve
1. **Parse** `plant_daily_measurements.csv` (validate required columns, timestamp format, numeric fields).  Expected columns (from a quick glance in the repo) are likely:
   - `plant_id` (stable identifier matching the `plants` table)
   - `timestamp` (ISO‑8601 string or Unix epoch)
   - `height_cm`
   - optional extra columns (e.g., `width_cm`, `area_px`…) – ignore or store as extra metadata.
2. **Map** each row to an existing plant via `plant_id`.  If the plant does not exist, the row should be **skipped** and reported.
3. **Create** an observation using the service layer so that the same validation, age/growth‑rate calculation, and provenance (`source="sensor"`) are applied.
4. **Idempotency** – before inserting, check for an existing observation with the same `plant_id` and `timestamp` (or a unique constraint on those fields) to avoid duplicates.
5. **Reporting** – after a sync run, display counts of inserted, skipped, duplicate, or errored rows (use `ui.info_box` / `ui.error_box`).  Add a button on the **Import measurements (CSV)** tab (or a dedicated “Sync sensor data” tab) that triggers the sync.
6. **No destructive DB changes** – the sync never updates existing manual observations; it only adds new rows.

## Readiness‑event workflow (to be added)
- UI page (e.g., extend `webapp/pages/feedback.py` or create `webapp/pages/readiness_event.py`) with a form:
  - Plant selector (dropdown of registered plants)
  - Observation date (date input)
  - Assessment method (free‑text or enum, default “human observation”)
  - Optional notes
- Backend service call (new function `svc.record_readiness_event`) that inserts into the `readiness_events` table (if not present, a simple migration may be needed).
- Validation: the readiness date must be ≥ plant `date_started` and ≥ earliest observation date for that plant.
- Store provenance (`method="human"`).

## Training‑example construction (future work)
When real readiness events exist, a training example is built from the **latest observation before the readiness date**.  The `svc` layer can be extended with a helper `svc.build_training_examples()` that:
- Joins each readiness event with the most recent observation ≤ readiness date.
- Computes `days_remaining = (readiness_date - observation_date).days`.
- Emits a feature dict (species, variety, age, height, growth_rate, …) and target `days_remaining`.
- Ensures no leakage of post‑readiness data.

## Model selection (existing)
The `svc.predictions` service already implements a four‑level hierarchy (species‑variety → broader species → synthetic → unavailable).  That logic will remain unchanged; the new sensor data simply populates the observation history that the model uses.

## Next concrete steps (for implementation)
1. **Inspect `plant_measurement.py` and a sample of `plant_daily_measurements.csv`** to document the exact column names and types.
2. **Add a sync function** (e.g., `svc.sync_sensor_csv(csv_path: Path) -> SyncReport`) that performs the import logic described above.
3. **Expose the sync** via a new button on the “Import measurements (CSV)” tab (or a dedicated tab) in the Streamlit UI.
4. **Create a readiness‑event form** (new page) and corresponding service/repository functions.
5. **Write unit tests** for the sync function (duplicate detection, schema validation) and the readiness‑event form handling.
6. **Run the full test suite** to ensure everything still passes.

## References for future models
- **`webapp/app.py`** – entry point, navigation (radio only after rename).
- **`webapp/app_pages/`** – contains all UI pages; the import tab lives in `species_data.py`.
- **`plantreadiness/services.py`** – central business logic (observation creation, age/growth, model selection).
- **`plantreadiness/repository.py`** – DB abstraction (SQLite for dev, Supabase for prod).
- **`tests/`** – 211 passing tests; `test_webapp_forms.py` now asserts no hidden error boxes.
- **`run.sh`** – script to bring up the app locally with a temporary SQLite secrets file.

---
*This file is intended to be a concise, machine‑readable snapshot of the repository state and the recent fixes.  It can be used by downstream LLM agents to continue the implementation without needing to re‑explore the codebase.*