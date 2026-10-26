# Plant Transplant Readiness Prediction System — Complete Technical Documentation

**Repository:** `RKB-vision/PLANTING-WINDOW-PREDICTOR` (branch `simplified-architecture`)
**Document:** `SYSTEM_DOCUMENTATION.md` (repository root)
**Purpose:** Explain how the entire system works, how every file functions, and how they interact.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture and Data Flow](#2-architecture-and-data-flow)
3. [Complete File Inventory](#3-complete-file-inventory)
4. [Database Schema](#4-database-schema)
5. [End-to-End Data Flow: Camera to Prediction](#5-end-to-end-data-flow-camera-to-prediction)
6. [Computer Vision Pipeline](#6-computer-vision-pipeline)
7. [Machine Learning Model](#7-machine-learning-model)
8. [Reference Baseline (Heuristic Predictor)](#8-reference-baseline-heuristic-predictor)
9. [Plant ID Resolution and Collision Prevention](#9-plant-id-resolution-and-collision-prevention)
10. [Calibration and Provenance](#10-calibration-and-provenance)
11. [API Endpoints](#11-api-endpoints)
12. [CLI Commands](#12-cli-commands)
13. [Configuration Constants](#13-configuration-constants)
14. [Honesty Principles (Enforced in Code)](#14-honesty-principles-enforced-in-code)
15. [What Is Implemented vs. What Is Not](#15-what-is-implemented-vs-what-is-not)
16. [Experimental Results](#16-experimental-results)
17. [Known Limitations and Next Steps](#17-known-limitations-and-next-steps)

---

## 1. System Overview

This system estimates when a plant species is likely ready for transplantation. It:

1. **Photographs** plants daily using an ESP32-CAM
2. **Measures** plant height, stem width, and leaf count using OpenCV with a reference rod for scale
3. **Stores** measurements as daily observations in SQLite
4. **Compares** the current plant against a historical cohort of successful plants
5. **Predicts** how many days remain until transplant readiness
6. **Learns** from real outcomes (transplant dates, 14-day survival) recorded over time

The system is deliberately honest: it always states the method, uncertainty, and limitations. It never fabricates data, never uses synthetic models silently, and never auto-retrains.

---

## 2. Architecture and Data Flow

```
ESP32-CAM → Cloudinary (cloud images)
     │
     ▼
OpenCV pipeline (plant_measurement.py)
  detect_reference_rod()  → cm/pixel scale
  detect_plant()         → bounding box
  calculate_measurements() → height, stem width, true leaves
  median_daily_record()   → one row per plant per day (median over N captures)
     │
     ▼
plant_daily_measurements.csv (audit trail)
     │
     ▼
camera_import.import_camera_measurements() → observations table (append-only)
     │
     ├──► reference.build_reference_from_repo() → baseline JSON (medians + plant counts)
     │
     └──► services.predict_species_cohort() → prediction
              │
              ├──► _snapshot_result() → prediction_snapshots (append-only)
              │
              └──► feedback (transplant record, 14-day follow-up)
                       │
                       └──► services.evaluate_predictions() → evidence-based comparison
```

**Three layers:**
- **Domain layer** (`plantreadiness/`): config, DB, repository, services, reference baseline, model, calibration, CSV/camera import
- **API layer** (`backend/app.py`): Flask endpoints
- **UI layer** (`nextjs/`): Next.js frontend

---

## 3. Complete File Inventory

### `plantreadiness/` — Domain Layer (18 files)

| File | Role | Key Functions |
|---|---|---|
| `config.py` | All constants: thresholds, paths, species names, calibration groups, camera settings, `normalize_plant_ref()` | `normalize_plant_ref`, `camera_sources`, `camera_refresh_enabled` |
| `db.py` | SQLite schema (10 tables), migrations, raw queries | `init_db`, `record_readiness_event`, `readiness_training_rows`, `species_cohort_snapshot`, `days_between` |
| `repository.py` | Repository abstraction (`BaseRepository` + `SQLiteRepository`) — the only way the app touches the DB | `register_plant`, `add_observation`, `get_observations`, `record_readiness_event` |
| `services.py` | Business logic: CSV import, prediction, camera refresh, evaluation, snapshot saving | `run_csv_import`, `predict_species_cohort`, `evaluate_predictions`, `build_reference_baseline`, `_parse_csv_text`, `refresh_camera_observations` |
| `reference.py` | Builds reference baseline (medians per age group), `provisional_estimate()` — the active heuristic predictor | `build_reference_from_repo`, `provisional_estimate`, `transplant_reference`, `compute_plant_trajectory` |
| `model.py` | ML: trains Ridge/Random Forest with GroupKFold CV, selects models (levels 1-4), predicts, manages artifact registry | `train_countdown_model`, `select_model`, `predict_days_remaining`, `grouped_cv_mae`, `build_training_frame`, `save_artifact` |
| `calibration.py` | Rod-length verification, calibration groups, `is_baseline_usable()` | `is_baseline_usable`, `verification_applies` |
| `csv_import.py` | Low-level CSV importer (append-only, idempotent, growth rate derivation) | `import_csv` |
| `camera_import.py` | Imports OpenCV-measured daily rows into observations; ambiguity quarantine | `import_camera_measurements`, `ref_to_plants` |
| `camera_refresh.py` | Pulls new Cloudinary images, measures them, imports (fail-open, throttled) | `refresh_camera_observations`, `_index_plants` |
| `synthetic.py` | Synthetic demo data generation | (synthetic data generation) |
| `seed.py` | Seeds the DB from the historical CSV | `seed_database` |
| `reset.py` | Wipes the DB + backs up generated files | `reset_database` |
| `purge.py` | Removes data by source/cohort (with safety checks) | `purge` |
| `verify_calibration.py` | CLI to verify rod length | `verify` |
| `reprocess_captures.py` | Re-measure already-processed Cloudinary images | `reprocess` |
| `migrate_species_rename.py` | One-off migration script | — |
| `dotenv.py` | Loads `.env` file | `load_dotenv` |

### `backend/`

| File | Role |
|---|---|
| `app.py` | Flask API: 15 endpoints (`/api/predict/species`, `/api/import`, `/api/reference`, `/api/camera/refresh`, `/api/species`, `/api/feedback`, `/api/transplant`, `/api/assessments`, etc.) |

### `nextjs/`

| File | Role |
|---|---|
| `pages/index.tsx` | Main prediction page: species selector, date picker, predict button, result card with method/uncertainty/limitations |
| `pages/feedback.tsx` | Feedback + Reference tabs: CSV upload panel, baseline view, evaluation, assessment/transplant forms |
| `pages/api/*.ts` | Next.js proxy routes that forward requests to Flask |

### Root files

| File | Role |
|---|---|
| `plant_measurement.py` | OpenCV vision pipeline: `detect_reference_rod` (L711), `detect_plant` (L1190), `calculate_measurements` (L1456), `median_daily_record` (L2044), `group_records_by_date` (L414) |
| `prediction.py` | CLI entry point for ad-hoc prediction |
| `plant_feedback.py` | CLI: register, observe, predict, transplant, feedback, import-camera, readiness, species-status, list |
| `reset` | Shell script → calls `plantreadiness/reset.py` |
| `data/super_orange_growth_data.csv` | Historical reference cohort (10 plants, 250 obs, 25 days) |
| `Apsara_Yellow_Marigold_Training_Data.csv` | Marigold dataset with readiness labels (12 plants, 300 obs) |
| `models/synthetic/` | 5 synthetic demo models (scikit-learn joblib) |
| `models/reference/` | 3 reference baseline JSON files (heuristic) |
| `models/real/` | 1 real trained model (Marigold, Random Forest, MAE 0.04 days) |

---

## 4. Database Schema (key tables)

| Table | Purpose | Key constraint |
|---|---|---|
| `plants` | Identity: species, variety, external_ref, cohort, data_source | `UNIQUE (data_source, species, variety, external_ref, cohort)` via `ux_plants_identity` |
| `observations` | Daily measurements + calibration provenance | `UNIQUE (plant_id, observation_date)` |
| `readiness_events` | Ground-truth labels (earliest readiness date per plant) | `UNIQUE (plant_id, readiness_definition_version)` |
| `feedback_14d` | Transplant + 14-day follow-up (append-only, audited corrections) | — |
| `prediction_snapshots` | Immutable prediction records | — |
| `calibration_settings` | Rod-length verification state | — |

---

## 5. End-to-End Data Flow: Camera to Prediction

### Step 1: Image capture and upload
- ESP32-CAM takes a photo and uploads it to Cloudinary with filename `Plant02_20260915_100000.jpg`
- Filename contract: `PlantID_YYYYMMDD_HHMMSS.jpg`
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

### CSV import path (alternative to camera)
```
User uploads CSV (Feedback → Reference tab)
  │
  ▼
nextjs/pages/api/import.ts → backend/app.py:/api/import
  │
  ▼
services.py:run_csv_import() (L1228)
  ├── _parse_csv_text() (L1064) → validates rows, parses dates, detects readiness column
  ├── repo.list_plants() → existing plants for species
  ├── repo.register_plant() → new plants (cohort='manual_upload')
  ├── repo.add_observation() → observations (append-only, idempotent)
  └── repo.record_readiness_event() → if readiness_observed_date column present
       │
       ▼
services.py:build_reference_baseline() → models/reference/baseline_*.json
```

---

## 6. Computer Vision Pipeline

### `plant_measurement.py` functions:

1. **`detect_reference_rod()` (L711):** Finds the rod by HSV colour range, computes `cm_per_pixel = rod_length_cm / rod_pixels`.
2. **`detect_plant()` (L1190):** Isolates the plant contour.
3. **`calculate_measurements()` (L1456):** `height = (base_y − top_y + 1) × cm_per_pixel`; stem width at a fixed fraction of height; leaf count via watershed segmentation.
4. **`median_daily_record()` (L2044):** Four captures per day → one row with median height, width, leaves. Failed captures are counted, never imputed.
5. **`group_records_by_date()` (L414):** Groups by capture date.

### Calibration:
- If the rod length is not verified (`verify_calibration`), observations are quarantined as `camera_unverified` and excluded from the baseline.
- Verification is tied to the specific rod length — changing `CAMERA_ROD_LENGTH_CM` resets verification.

---

## 7. Machine Learning Model

### 7.1 Features (X) — `model.py:45`
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
- `plant_id`, `observation_date`, `cohort` are **excluded** from X

### 7.2 Target (y) — `db.py:1962`
```python
days_remaining = readiness_observed_date - observation_date  (calendar days)
```
- Computed in `readiness_training_rows()` (L1896): joins plants + observations + readiness_events
- Only observations at or before readiness date are included
- Earliest readiness event per plant is used (`MIN`)
- Plants without readiness events contribute nothing

### 7.3 Training Pipeline — `model.py:178`
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

### 7.4 Three Candidate Models
| Model | Algorithm | Purpose |
|---|---|---|
| `mean_baseline` | `DummyRegressor(strategy="mean")` | Null hypothesis — "can any model beat guessing the average?" |
| `ridge` | `Ridge(alpha=1.0)` | Simple linear, L2 regularization |
| `random_forest` | `RandomForestRegressor(n_estimators=200, max_depth=8, min_samples_leaf=3)` | Non-linear, captures feature interactions |

### 7.5 Model Selection at Prediction Time — `model.py:332`
```
Level 1: Validated species+variety real model → best
Level 2: Broader validated real model for species → good
Level 3: Synthetic demo (only if allow_synthetic=True) → demo only
Level 4: No model → falls through to provisional baseline
```

### 7.6 Synthetic vs. Real
- **Synthetic:** 5 `.joblib` files in `models/synthetic/`, trained on fake data. Never used unless `allow_synthetic=True`. Always labeled "SYNTHETIC DEMO."
- **Real:** `models/real/` — created only after training with real readiness labels. Marigold has one (Random Forest, MAE 0.04 days). Sunflower has none (0 readiness events).

---

## 8. Reference Baseline (Heuristic Predictor)

**This is NOT ML.** It's a deterministic lookup table.

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

## 9. Plant ID Resolution and Collision Prevention

### Identity design
- Internal `plant_id` (autoincrement) is the true identity.
- `external_ref` = source-system ID (e.g., CSV `Plant_ID` or ESP32 ref).
- Uniqueness is composite: `(data_source, species, variety, external_ref, cohort)`.

### Normalization (`config.py:normalize_plant_ref`)
- Case, whitespace, hyphens, underscores are ignored.
- **Zero-padding is significant:** `Plant01` ≠ `Plant1` ≠ `Plant001`.

### Collision scenarios handled
| Scenario | Handling |
|---|---|
| Same `Plant_ID` in two CSVs, different species | Distinct plants (species in uniqueness key) |
| Same `Plant_ID` in two CSVs, same species | Same plant (idempotent re-import) |
| Live plant + historical plant share `external_ref` | Distinct (cohort differs); camera resolves only to live plant |
| `Plant01` vs `Plant-01` vs `plant01` | Normalize to same key → ambiguous if both registered → quarantine |
| `Plant01` vs `Plant1` vs `Plant001` | Different (zero-padding significant) |
| Camera ref not registered | Skipped, never auto-created |

### Ambiguity quarantine
- `camera_import.py` and `camera_refresh.py` exclude historical plants (`cohort = 'reference'` or `'manual_upload'`) from camera targeting.
- If two registered refs normalize to the same key → quarantined, never resolves to first match.
- Tested: 15 tests in `test_plant_id_resolution.py`.

---

## 10. Calibration and Provenance

### Calibration groups
- `manual_measurement` (CSV), `camera_verified`, `camera_unverified`, `unknown`
- `BASELINE_USABLE_CALIBRATION_GROUPS = (manual_measurement, camera_verified)`

### Verification
- `is_baseline_usable()` in `calibration.py` (L83) gates which observations contribute to the baseline.
- Verification is tied to the specific rod length — changing `CAMERA_ROD_LENGTH_CM` resets verification.
- Camera observations are still stored and used for prediction (current-state input) even when unverified — they're just flagged and excluded from the baseline.

### Every observation records
- `calibration_source` (e.g., `csv_import_no_calibration`, `camera_configured`)
- `calibration_verified` (boolean)
- `rod_length_cm` (the rod length used for this measurement)

---

## 11. API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/import` | CSV upload → import + auto-build baseline |
| `POST /api/predict/species` | Species-cohort prediction (auto-runs camera refresh) |
| `GET /api/reference?build=1` | Build/persist reference baseline |
| `POST /api/camera/refresh` | Pull + measure + import new Cloudinary images |
| `GET /api/species` | Species catalog (includes configured species with 0 counts) |
| `GET /api/calibration` | Rod-length verification state + quarantine summary |
| `POST /api/reference/build` | Build reference baseline from stored measurements |

---

## 12. CLI Commands

| Command | Purpose |
|---|---|
| `./reset` | Clean DB + backup generated files |
| `plant_feedback.py register` | Register a live plant |
| `plant_feedback.py import-camera` | Import camera CSV |
| `plant_feedback.py predict` | View prediction for a plant |
| `plant_feedback.py transplant` | Record transplant date |
| `plant_feedback.py due/submit` | 14-day feedback workflow |
| `plant_feedback.py readiness` | Record ground-truth readiness event |
| `plant_feedback.py species-status` | Readiness events per species |
| `prediction.py --species X --json` | Ad-hoc prediction |
| `plant_measurement.py` | OpenCV measurement pipeline |
| `./run.sh` | Launch Streamlit dashboard |
| `./reset.sh` | Reset database + backup |

---

## 13. Configuration Constants (key thresholds)

| Constant | Value | Meaning |
|---|---|---|
| `MIN_PROVISIONAL_PLANTS` | 3 | Min independent plants per reference group |
| `MIN_INDEPENDENT_PLANTS` | 4 | Min for model training/validation |
| `MIN_READINESS_EVENTS` | 6 | Min readiness events for a validated model |
| `PROVISIONAL_AGE_TOLERANCE_DAYS` | 3 | Age matching tolerance |
| `PROVISIONAL_HEIGHT_TOLERANCE_CM` | 1.5 | Height matching tolerance |
| `GROWTH_RATE_ADJUSTMENT_MIN/MAX_FACTOR` | 0.5 / 2.0 | Growth-rate adjustment clamp |
| `CAMERA_CAPTURES_PER_REFRESH` | 4 | Max images per refresh pass |
| `CAMERA_MIN_REFRESH_INTERVAL_SECONDS` | 60 | Refresh throttle |
| `BASELINE_USABLE_CALIBRATION_GROUPS` | manual, camera_verified | Which calibration groups feed the baseline |

---

## 14. Honesty Principles (enforced in code)

1. **Missing ≠ zero:** Growth rate stays `NULL` when interval ≤ 0. `_median()` skips `None`. `calculate_measurements()` never imputes.
2. **Method always stated:** Every prediction has a `method` field (`validated model` / `provisional historical baseline` / `synthetic demo` / `insufficient evidence`).
3. **Synthetic never silent:** `select_model()` only returns synthetic at Level 3 when `allow_synthetic=True`. The UI passes `allow_synthetic: Boolean(body.allow_synthetic)` (defaults to false).
4. **Predictions append-only:** `prediction_snapshots` table has no UPDATE path. Feedback links via FK.
5. **No auto-retrain:** `evaluate_predictions` returns a recommendation; nothing auto-trains.
6. **Calibration honesty:** Unverified camera data is quarantined and excluded from the baseline.

---

## 15. What Is Implemented vs. What Is Not

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

## 16. Experimental Results

### 16.1 Model Comparison — Marigold (Apsara Yellow), 12 plants, 300 observations, 12 readiness events

| Model | MAE (days) | MAE Std | n_splits | Beats baseline? |
|---|---|---|---|---|
| Mean baseline (DummyRegressor) | 6.24 | 0.00 | 5 | — (reference) |
| Ridge (linear, L2) | 0.13 | 0.02 | 5 | ✅ Yes |
| Random Forest (200 trees) | 0.04 | 0.02 | 5 | ✅ Yes (best) |

**Improvement:** Random Forest reduces MAE from 6.24 → 0.04 days (99.4% error reduction).

**Validation:** GroupKFold (n_splits=5, grouped by plant_id). Repeated observations of the same plant never split across folds.

**Selected model:** Random Forest (lowest MAE, validated=True, 12 independent plants, 12 labelled readiness events).

### 16.2 Synthetic Demo Models (for reference only — never used silently)

| Model | Species/Variety | MAE (days) | Plants | Events |
|---|---|---|---|---|
| Ridge | Demo Bean / Bush | 0.066 | 8 | 8 |
| Ridge | Demo Bean / Climbing | 0.081 | 8 | 8 |
| Ridge | Super Orange / Early | 0.459 | 8 | 8 |
| Ridge | Super Orange / Late | 0.456 | 8 | 8 |
| Ridge | ALL / ANY | 0.355 | 32 | 32 |

### 16.3 Before vs. After Training (Marigold)

| Metric | Before (provisional) | After (Random Forest, real) |
|---|---|---|
| Method | Provisional historical baseline | Level 2 real model (Random Forest) |
| `validated` | N/A (heuristic) | `true` |
| MAE | Not reported (heuristic) | 0.04 days |
| `synthetic` | false | false (real data) |
| `model_name` | — | random_forest |
| n_independent_plants | — | 12 |
| n_labelled_readiness_events | 0 | 12 |
| `provisional` | true | false (real model selected) |

### 16.4 Key Experimental Observations

1. **The mean baseline (DummyRegressor) has MAE 6.24 days** — this is the "null hypothesis." Any real model must beat this to be useful.
2. **Ridge reduces MAE to 0.13 days** — a 98% improvement. Linear regression captures most of the signal (age + height + growth rate are strongly predictive of days remaining).
3. **Random Forest further reduces MAE to 0.04 days** — a 99.4% improvement over the mean baseline. The non-linear interactions (age × height × growth rate) provide additional signal.
4. **GroupKFold prevents data leakage** — observations from the same plant never split across train/test folds.
5. **The provisional baseline cannot report MAE** — it's not a trained model, so it has no cross-validated error estimate.
6. **Synthetic models have comparable MAE (0.066–0.459)** but are trained on fake data — their metrics are meaningless for real plants.

### 16.5 One-liner for presentation

> "We compared three models (mean baseline, Ridge, Random Forest) using plant-grouped cross-validation. Random Forest achieved MAE 0.04 days on real Marigold data — a 99.4% improvement over the mean baseline. Before training (no readiness labels), the system honestly falls back to a provisional heuristic with no MAE."

---

## 17. Known Limitations and Next Steps

1. **No real trained model for Sunflower** (0 readiness events). The provisional baseline assumes transplant = day after last monitoring day.
2. **Camera data is unverified by default.** Observations from uncalibrated cameras are quarantined (`camera_unverified`) and excluded from the baseline.
3. **No firmware in repo.** The ESP32-CAM code is external; only the filename contract (`PlantID_YYYYMMDD_HHMMSS.jpg`) is documented.
4. **No auto-retrain.** Even with enough data, a human must decide to train and promote a model.
5. **Single-species reference.** Only Super Orange has historical data. Marigold now has a trained model (after CSV import with readiness labels), but Sunflower still uses provisional baseline.
6. **No prediction snapshots in simplified branch.** Predictions are not persisted (append-only) in the Streamlit version.

**Next steps:**
- Record real transplant outcomes via the feedback loop for Sunflower
- Train and validate a real model for Sunflower using the existing grouped-CV pipeline
- Verify camera calibration (`verify_calibration --rod-length-cm N --verified`)
- Add 14-day feedback follow-up to the simplified branch
- Add prediction snapshot persistence

---

**End of document.** Code is the source of truth; this document is a guide.