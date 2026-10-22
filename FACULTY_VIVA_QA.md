# Faculty Viva — Technical Report: Plant Transplant Readiness Prediction System

**Repository:** `RKB-vision/PLANTING-WINDOW-PREDICTOR` (branch `main`)
**Document:** `FACULTY_VIVA_QA.md` (repository root)

This document explains the entire system — from camera to prediction — in plain language, with exact file and function references. Written so that someone with little background can follow it.

---

## 1. What Does This System Do? (The Big Picture)

Imagine you are growing plants in a nursery. You need to know **when each plant is ready to be moved (transplanted) to the field**. Doing this by eye is unreliable. This system:

1. Takes **photos** of the plant every day using an ESP32-CAM (a small camera module).
2. **Measures** the plant's height, stem width, and leaf count from each photo using computer vision (OpenCV).
3. **Stores** those measurements in a database.
4. **Compares** the current plant's measurements against a historical record of plants that were successfully transplanted.
5. **Predicts** how many days remain until this plant reaches transplant size.
6. **Learns** from real outcomes over time (did the plant actually survive after transplant?).

The system is deliberately honest: it always says *how* it predicted (method), *how sure* it is (uncertainty), and *what its limitations* are. It never fabricates data.

---

## 2. The Three Layers

The system has three layers, like a three-storey building:

### Layer 1: Domain layer (`plantreadiness/` package)
This is the "brain." It contains all the business logic, database access, prediction math, and calibration rules. Every other layer calls into this.

### Layer 2: API layer (`backend/app.py`)
A Flask web server that exposes HTTP endpoints. The frontend and CLI call these endpoints. Each endpoint opens a database connection, calls the domain layer, and returns JSON.

### Layer 3: UI layer (`nextjs/`)
A Next.js web application. The user selects a species, picks a date, clicks "Predict," and sees the result. There is also a Feedback page for uploading CSVs and viewing evaluations.

---

## 3. What Each File Does (Complete Inventory)

### `plantreadiness/` — The Domain Layer (18 files)

| File | What it does | Key functions |
|---|---|---|
| `config.py` | All constants and settings. Thresholds (MIN_PROVISIONAL_PLANTS=3, MIN_READINESS_EVENTS=6), paths, species names, calibration groups, camera settings. Also `normalize_plant_ref()` which normalizes plant IDs. | `normalize_plant_ref`, `camera_sources`, `camera_refresh_enabled` |
| `db.py` | Creates the SQLite database schema (10 tables). Handles migrations. Contains raw SQL queries including `readiness_training_rows` (builds ML training data) and `species_cohort_snapshot` (pulls observations for prediction). | `init_db`, `record_readiness_event`, `readiness_training_rows`, `species_cohort_snapshot`, `days_between` |
| `repository.py` | The "repository pattern" — an abstraction layer over the database. `BaseRepository` defines the interface; `SQLiteRepository` implements it. All DB access goes through this. | `register_plant`, `add_observation`, `get_observations`, `record_readiness_event`, `species_cohort_snapshot` |
| `services.py` | The "business logic" layer. Orchestrates everything: CSV import, prediction, camera refresh, evaluation, snapshot saving. | `run_csv_import`, `predict_species_cohort`, `evaluate_predictions`, `build_reference_baseline`, `_parse_csv_text`, `refresh_camera_observations` |
| `reference.py` | Builds the reference baseline (median heights, growth rates, days-to-transplant per age group). Also contains `provisional_estimate()` — the heuristic prediction algorithm. | `build_reference_from_repo`, `provisional_estimate`, `transplant_reference`, `compute_plant_trajectory` |
| `model.py` | Machine learning: trains Ridge/Random Forest with GroupKFold CV, selects models (levels 1-4), predicts, manages artifact registry. | `train_countdown_model`, `select_model`, `predict_days_remaining`, `grouped_cv_mae`, `build_training_frame`, `save_artifact` |
| `calibration.py` | Rod-length verification. Determines which calibration groups can feed the baseline. | `is_baseline_usable`, `verification_applies` |
| `csv_import.py` | Low-level CSV importer. Append-only, idempotent. Derives growth rates when missing. | `import_csv` |
| `camera_import.py` | Imports OpenCV-measured daily rows into observations. Handles ambiguity quarantine. | `import_camera_measurements`, `ref_to_plants` |
| `camera_refresh.py` | Pulls new Cloudinary images, measures them, imports. Fail-open, throttled. | `refresh_camera_observations`, `_index_plants` |
| `synthetic.py` | Generates synthetic demo data for testing/demo. | (synthetic data generation) |
| `seed.py` | Seeds the database from the historical CSV. | `seed_database` |
| `reset.py` | Wipes the database and backs up generated files. | `reset_database` |
| `purge.py` | Removes data by source/cohort (with safety checks). | `purge` |
| `verify_calibration.py` | CLI to verify rod length. | `verify` |
| `reprocess_captures.py` | Re-measures already-processed Cloudinary images. | `reprocess` |
| `migrate_species_rename.py` | One-off migration script. | — |
| `dotenv.py` | Loads `.env` file. | `load_dotenv` |

### `backend/`
| File | Role |
|---|---|
| `app.py` | Flask API server. 15 endpoints: `/api/predict/species`, `/api/import`, `/api/reference`, `/api/camera/refresh`, `/api/species`, `/api/feedback`, `/api/transplant`, `/api/assessments`, etc. |

### `nextjs/`
| File | Role |
|---|---|
| `pages/index.tsx` | Main prediction page: species selector, date picker, predict button, result card with method/uncertainty/limitations |
| `pages/feedback.tsx` | Feedback + Reference tabs: CSV upload panel, baseline view, evaluation, assessment/transplant forms |
| `pages/api/*.ts` | Next.js proxy routes that forward requests to Flask (e.g., `predict/species.ts`, `import.ts`) |

### Root files
| File | Role |
|---|---|
| `plant_measurement.py` | OpenCV vision pipeline: `detect_reference_rod` (L711), `detect_plant` (L1190), `calculate_measurements` (L1456), `median_daily_record` (L2044), `group_records_by_date` (L414) |
| `prediction.py` | CLI entry point: `python prediction.py --species X --age-days 20 --height-cm 5.0 --json` |
| `plant_feedback.py` | CLI: register, observe, predict, transplant, feedback, import-camera, readiness, species-status, list |
| `reset` | Shell script → calls `plantreadiness/reset.py` |
| `data/super_orange_growth_data.csv` | Historical reference cohort (10 plants, 250 obs, 25 days, 4 columns: Date, Age, Plant_ID, Height_cm) |
| `Apsara_Yellow_Marigold_Training_Data.csv` | Marigold dataset with readiness labels (12 plants, 300 obs, 8 columns including `readiness_observed_date`) |
| `models/synthetic/` | 5 synthetic demo models (scikit-learn joblib) |
| `models/reference/` | 3 reference baseline JSON files (heuristic) |
| `models/real/` | 1 real trained model (Marigold, Random Forest, MAE 0.04 days) |

---

## 4. Complete Data Flow: Camera → Prediction

Here is the exact path a photo takes from the camera to a prediction result:

### Step 1: Image capture and upload
- ESP32-CAM takes a photo and uploads it to Cloudinary with filename `Plant02_20260915_100000.jpg`
- The filename follows the contract: `PlantID_YYYYMMDD_HHMMSS.jpg`
- The prefix (`Plant02_`) identifies which plant

### Step 2: Camera refresh (triggered by prediction or manual call)
- `backend/app.py:/api/camera/refresh` → `services.py:refresh_camera_observations()` (L68)
- Reads `CAMERA_SOURCES` from `.env` (e.g., `Plant02_=Plant02`)
- For each source:
  1. `camera_refresh.py:_index_plants()` (L258) — looks up registered plants by normalized external_ref. Excludes historical plants (`cohort = 'reference'` or `'manual_upload'`).
  2. If plant not found → status "unregistered", skip
  3. If multiple plants match → status "ambiguous", quarantine
  4. If species mismatch → status "other_species", skip
  5. Pull new images from Cloudinary via `plant_measurement.py:find_new_cloudinary_images()`
  6. Measure each image with OpenCV: `detect_reference_rod()` → `detect_plant()` → `calculate_measurements()`
  7. `group_records_by_date()` groups captures by date
  8. `median_daily_record()` aggregates N captures into one row (median over valid captures)
  9. `camera_import.py:import_camera_measurements()` inserts observations (append-only, idempotent)
- Throttled to 60 seconds by default (`CAMERA_MIN_REFRESH_INTERVAL_SECONDS`)
- Fail-open: any error is reported, never raised

### Step 3: Prediction
- `backend/app.py:/api/predict/species` → `services.py:predict_species_cohort()` (L493)
- `db.py:species_cohort_snapshot()` (L1334) — pulls all observations for the species on the latest date
- Aggregates into population median: height, age, growth rate, stem width, leaves, plants represented
- `model.py:select_model()` (L332) — tries Level 1 → 2 → 3 → 4
- If a model is found: `model.py:predict_days_remaining()` (L440) — predicts days remaining
- If no model (Level 4) and `allow_provisional_baseline=True`: `reference.py:provisional_estimate()` (L429) — heuristic
- `_snapshot_result()` → saves to `prediction_snapshots` table (append-only)
- Returns JSON: method, days_remaining, predicted_readiness_date, uncertainty, cohort snapshot, readiness event counts

### Step 4: Feedback loop
- User records transplant date → `feedback_14d` table
- User records 14-day follow-up → survival, condition, height
- `services.py:evaluate_predictions()` (L905) → compares predicted vs. observed, computes MAE, match rate, recommendation

---

## 5. The ML Model — How It Works

### 5.1 What enters the model (Features)

```python
FEATURE_COLUMNS = [
    "age_days", "height_cm", "growth_rate_cm_per_day",
    "stem_width_cm", "visible_true_leaf_count",
    "species", "variety",
]
```

- 5 numeric + 2 categorical = 7 features
- **Preprocessing:** `SimpleImputer(strategy="median")` → `StandardScaler()` for numeric; `SimpleImputer(strategy="most_frequent")` → `OneHotEncoder(handle_unknown="ignore")` for categorical
- Missing values filled with column median (not zero)
- `plant_id`, `observation_date`, `cohort` are **excluded** (plant_id used for GroupKFold grouping only)

### 5.2 The target (y)

```python
days_remaining = readiness_observed_date - observation_date  (calendar days)
```

Computed in `db.py:readiness_training_rows()` (L1896). Only observations at or before readiness date are included. Plants without readiness events contribute nothing.

### 5.3 Training (`model.py:train_countdown_model`)

```
1. check_min_requirements: ≥4 plants AND ≥6 readiness events
2. For each of 3 models (mean_baseline, ridge, random_forest):
   → GroupKFold CV (n_splits = min(5, n_plants), grouped by plant_id)
   → Record MAE (days) per fold
3. Compare: best = lowest MAE, must beat mean_baseline
4. validated = True only if: best ≠ mean_baseline AND best_mae < baseline_mae AND minimums met
5. Refit winning model on ALL data
6. Save as artifact with metadata
```

### 5.4 The three candidate models

| Model | Algorithm | Purpose |
|---|---|---|
| `mean_baseline` | `DummyRegressor(strategy="mean")` | Null hypothesis — "can any model beat guessing the average?" |
| `ridge` | `Ridge(alpha=1.0)` | Simple linear, L2 regularization |
| `random_forest` | `RandomForestRegressor(n_estimators=200, max_depth=8, min_samples_leaf=3)` | Non-linear, captures feature interactions |

### 5.5 Model selection at prediction time (`model.py:select_model`)

```
Level 1: Validated species+variety real model → best
Level 2: Broader validated real model for species → good
Level 3: Synthetic demo (only if allow_synthetic=True) → demo only
Level 4: No model → falls through to provisional baseline
```

### 5.6 Synthetic vs. Real

- **Synthetic:** 5 `.joblib` files in `models/synthetic/`, trained on fake data. Never used unless `allow_synthetic=True`. Always labeled "SYNTHETIC DEMO."
- **Real:** `models/real/` — created only after training with real readiness labels. Marigold (Apsara Yellow) now has one (Random Forest, MAE 0.04 days). Sunflower has none (0 readiness events).

---

## 6. The Reference Baseline (Heuristic Predictor)

**This is NOT machine learning.** It's a deterministic lookup table.

### Building (`reference.py:build_reference_from_repo`)
- For each plant: sort observations, compute trajectory, determine transplant date (recorded or assumed = day after last observation)
- For each observation: `days_until_transplant = transplant_date - observation_date`
- Group by `age_days`: for each age, compute median height, median growth rate, median days-until-transplant, IQR, n_plants

### Predicting (`reference.py:provisional_estimate`)
```
1. Find nearest age group (±3 days tolerance), or height group (±1.5 cm)
2. Check: group has ≥3 independent plants? (MIN_PROVISIONAL_PLANTS)
3. base_days = group's median_days_until_transplant
4. factor = clamp(cohort_rate / plant_rate, 0.5, 2.0)  [if both rates available]
5. adjusted_days = base_days × factor
6. predicted_date = observation_date + adjusted_days
```

Always labeled `provisional: true`, `method: provisional historical baseline`.

---

## 7. Key Algorithms

### Growth Rate Derivation (`csv_import.py`)
```
rate = (height_t - height_{t-1}) / elapsed_days
```
Only when both heights exist and interval > 0. Otherwise `NULL`. Never invented.

### Camera Measurement (`plant_measurement.py`)
- `detect_reference_rod()`: HSV segmentation of coloured rod → cm/pixel scale
- `detect_plant()`: plant contour isolation
- `calculate_measurements()`: height, stem width, leaf count
- `median_daily_record()`: aggregates N captures into one daily row (median over valid captures)
- `group_records_by_date()`: groups by capture date

### Calibration (`calibration.py`)
- Groups: `manual_measurement`, `camera_verified`, `camera_unverified`, `unknown`
- `BASELINE_USABLE_CALIBRATION_GROUPS = (manual_measurement, camera_verified)`
- Unverified camera data is quarantined and excluded from baseline

### Plant ID Resolution (`config.py`, `camera_import.py`, `camera_refresh.py`)
- `normalize_plant_ref()`: case/whitespace/punctuation-insensitive, zero-padding significant
- Composite uniqueness: `(data_source, species, variety, external_ref, cohort)`
- Ambiguous normalized collisions → quarantine, never resolve to first match
- Historical plants excluded from camera targeting

---

## 8. Database Schema (key tables)

| Table | Purpose |
|---|---|
| `plants` | Identity: species, variety, external_ref, cohort, data_source |
| `observations` | Daily measurements + calibration provenance |
| `readiness_events` | Ground-truth labels (earliest readiness date per plant) |
| `feedback_14d` | Transplant + 14-day follow-up (append-only) |
| `prediction_snapshots` | Immutable prediction records |
| `calibration_settings` | Rod-length verification state |

---

## 9. API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/import` | CSV upload → import + auto-build baseline |
| `POST /api/predict/species` | Species-cohort prediction (auto-runs camera refresh) |
| `GET /api/reference?build=1` | Build/persist reference baseline |
| `POST /api/camera/refresh` | Pull + measure + import new Cloudinary images |
| `GET /api/species` | Species catalog (includes configured species with 0 counts) |

---

## 10. Honesty Principles (enforced in code)

1. **Missing ≠ zero:** Growth rate stays `NULL` when interval ≤ 0. `_median()` skips `None`.
2. **Method always stated:** Every prediction has `method`, `provisional`, `synthetic` fields.
3. **Synthetic never silent:** `select_model()` returns synthetic only at Level 3 with `allow_synthetic=True`.
4. **Predictions append-only:** `prediction_snapshots` has no UPDATE path.
5. **No auto-retrain:** `evaluate_predictions()` returns recommendation; nothing auto-trains.
6. **Calibration honesty:** Unverified camera data excluded from baseline.

---

## 11. What's Implemented vs. What's Not

| Feature | Status |
|---|---|
| Camera measurement (OpenCV) | ✅ Implemented |
| CSV import (append-only, idempotent) | ✅ Implemented |
| Reference baseline (provisional) | ✅ Implemented, active predictor |
| Trained real-data model | ✅ Now possible (after CSV with readiness labels) |
| Synthetic demo model | ✅ Exists, never used silently |
| Prediction snapshot (append-only) | ✅ Implemented |
| Feedback (transplant + 14-day) | ✅ Implemented (CLI + API) |
| Evaluation (evidence-based) | ✅ Implemented (CLI + API) |
| Auto-retrain | ❌ Not implemented (by design) |
| Firmware (ESP32-CAM) | ❌ Outside repo (documented contract only) |

---

## 12. Ten Core Viva Questions

### Q1. What is this system and what problem does it solve?

**Answer:** It estimates when a plant species is likely ready for transplantation, using camera measurements and historical growth data. It is deliberately honest about what it can and cannot claim — there is no trained real-data ML model in the active path; the predictor is a deterministic historical reference baseline.

**Evidence:** `services.py:predict_species_cohort` (L493), `reference.py:provisional_estimate` (L429)

### Q2. How does the computer vision pipeline measure the plant?

**Answer:** OpenCV segments a coloured reference rod and the plant in HSV space, computes cm/pixel from the rod, then derives height, stem width, and leaf count. Four captures per day are aggregated into one median daily record. Calibration is verified or quarantined.

**Evidence:** `plant_measurement.py:detect_reference_rod` (L711), `detect_plant` (L1190), `calculate_measurements` (L1456), `median_daily_record` (L2044)

### Q3. How does the prediction work — what is the "provisional baseline"?

**Answer:** It's a lookup table, not a model. It matches the current plant's age against historical age groups, reads the median days-to-transplant for that group, and adjusts by growth rate — clamped between 0.5× and 2.0×. Always labeled `provisional: true`.

**Evidence:** `reference.py:provisional_estimate` (L429), `reference.py:build_reference_from_repo` (L335), `config.py`: `MIN_PROVISIONAL_PLANTS=3`, `PROVISIONAL_AGE_TOLERANCE_DAYS=3`

### Q4. Why is there no real trained ML model, and what would it take to get one?

**Answer:** A real model needs ground-truth readiness labels (transplant dates). The CSV has measurements only. You'd need to record transplant outcomes via the feedback loop, then train with the existing grouped-CV pipeline (`train_countdown_model`). The Apsara Yellow CSV now has `readiness_observed_date` → 12 readiness events → a real Random Forest model (MAE 0.04 days).

**Evidence:** `model.py:train_countdown_model` (L178), `check_min_requirements` (L150), `db.py:readiness_training_rows` (L1814)

### Q5. How does the system prevent CSV plant ID collisions across species?

**Answer:** Plant identity is composite: (data_source, species, variety, external_ref, cohort). The same 'Plant01' in a Sunflower CSV and a Marigold CSV creates two distinct plants. `normalize_plant_ref()` normalizes case/whitespace but zero-padding is significant. Ambiguous collisions are quarantined.

**Evidence:** `db.py` schema (L55–65), `config.py:normalize_plant_ref`, `camera_import.py` (L235–245), `tests/test_plant_id_resolution.py` (15 tests)

### Q6. What is the calibration/provenance story for camera measurements?

**Answer:** Every observation records its calibration source and whether the rod was verified. Camera observations are quarantined as 'camera_unverified' until `verify_calibration` is run with a specific rod length. Only verified or manual measurements feed the baseline.

**Evidence:** `calibration.py:is_baseline_usable` (L83), `camera_import.py` (L347–365)

### Q7. How does the feedback and evaluation loop work?

**Answer:** Predictions are append-only snapshots. The user records a transplant date and a 14-day follow-up. Evaluation compares predicted vs. observed readiness dates and recommends (but never auto-applies) improvements. Missing feedback is "pending," never "failure."

**Evidence:** `services.py:evaluate_predictions` (L905), `services.py:save_prediction_snapshot` (L765)

### Q8. What are the "honesty principles" and how are they enforced in code?

**Answer:** The system never fabricates data, never hides uncertainty, never uses synthetic silently, and never auto-retrains. These are enforced by code-level guards: NULL preserved (never zeroed), method always stated, synthetic only at Level 3 with explicit opt-in, predictions append-only, no auto-retrain, unverified camera data quarantined.

**Evidence:** `model.py:select_model` (L332), `services.py:predict_species_cohort` (L493), `db.py` (append-only tables)

### Q9. What are the limitations of this system?

**Answer:** No real trained model for Sunflower (0 readiness events). The provisional baseline assumes transplant = day after last monitoring day. Camera data is unverified by default. No firmware in repo. No auto-retrain. Single-species reference (only Super Orange has historical data).

**Evidence:** `config.py`: `MIN_READINESS_EVENTS=6`, `MIN_INDEPENDENT_PLANTS=4`; `reference.py:transplant_reference` (L150); `docs/DEPLOYMENT.md` §6

### Q10. How does the system handle a new species from Cloudinary to prediction?

**Answer:** Register a live plant, set CAMERA_SOURCES, upload photos with the right filename prefix, and the camera refresh pulls, measures, and imports them. Prediction then runs against the species' reference baseline (or trained model if readiness labels exist). Alternatively, upload a CSV via the Feedback tab to bootstrap the baseline.

**Evidence:** `camera_refresh.py:refresh_camera_observations` (L68), `camera_import.py:import_camera_measurements` (L157), `backend/app.py:/api/camera/refresh`, `/api/import`, `/api/predict/species`

---

## Part 2 — Where to Find It in the Code

| Topic | File | Function/Line |
|---|---|---|
| Prediction entry point | `plantreadiness/services.py` | `predict_species_cohort` (L493) |
| Provisional estimate math | `plantreadiness/reference.py` | `provisional_estimate` (L429) |
| Baseline construction | `plantreadiness/reference.py` | `build_reference_from_repo` (L335) |
| Model selection (levels 1–4) | `plantreadiness/model.py` | `select_model` (L332) |
| Training | `plantreadiness/model.py` | `train_countdown_model` (L178) |
| Grouped CV | `plantreadiness/model.py` | `grouped_cv_mae` (L157) |
| CSV import | `plantreadiness/services.py` | `run_csv_import` (L1228) |
| CSV parsing | `plantreadiness/services.py` | `_parse_csv_text` (L1064) |
| Camera refresh | `plantreadiness/camera_refresh.py` | `refresh_camera_observations` (L68) |
| Camera import | `plantreadiness/camera_import.py` | `import_camera_measurements` (L157) |
| Plant ID normalization | `plantreadiness/config.py` | `normalize_plant_ref` |
| Calibration | `plantreadiness/calibration.py` | `is_baseline_usable` (L83) |
| DB schema | `plantreadiness/db.py` | SCHEMA_SQL (L55) |
| Readiness events | `plantreadiness/db.py` | `record_readiness_event` (L1396) |
| Readiness training rows | `plantreadiness/db.py` | `readiness_training_rows` (L1814) |
| Evaluation | `plantreadiness/services.py` | `evaluate_predictions` (L905) |
| OpenCV measurement | `plant_measurement.py` | `detect_reference_rod` (L711), `detect_plant` (L1190), `calculate_measurements` (L1456), `median_daily_record` (L2044) |

---

## Part 3 — Final verification report

| Check | Result |
|---|---|
| Test suite | 233 passed, 9 xfailed (intentional guards) |
| DB state after reset | 10 plants, 250 observations, 0 readiness events, 0 feedback, 0 predictions |
| Active predictor | Provisional historical baseline (no real trained model selected) |
| Synthetic usage | Never silent; opt-in only |
| Plant ID collisions | Tested (15 tests in `test_plant_id_resolution.py`) |
| Camera quarantine | Verified (unverified obs excluded from baseline) |
| Append-only design | Verified (no UPDATE paths on predictions/feedback) |
| Missing data handling | Verified (NULL preserved, never zeroed) |
| Git status | All committed and pushed to `origin/main` |

---

**End of document.** Code is the source of truth; this document is a guide.