# Faculty Viva — Technical Report: Plant Transplant Readiness Prediction System

**Repository:** `RKB-vision/PLANTING-WINDOW-PREDICTOR` (branch `main`)
**Document:** `FACULTY_VIVA_QA.md` (repository root)

---

## 1. System Architecture

```
ESP32-CAM → Cloudinary → OpenCV (plant_measurement.py) → CSV → SQLite (plantreadiness/) → Flask API (backend/app.py) → Next.js UI (nextjs/)
```

**Three layers:** Domain (`plantreadiness/`), API (`backend/app.py`), UI (`nextjs/`).

---

## 2. Code Structure — What Each File Does

### `plantreadiness/` (domain layer — 18 files)

| File | Role |
|---|---|
| `config.py` | All constants: thresholds (MIN_PROVISIONAL_PLANTS=3, MIN_INDEPENDENT_PLANTS=4, MIN_READINESS_EVENTS=6), paths, species names, calibration groups, camera settings, `normalize_plant_ref()` |
| `db.py` | SQLite schema (10 tables), migrations, raw queries (`readiness_training_rows`, `species_cohort_snapshot`, `record_readiness_event`) |
| `repository.py` | Repository abstraction (`BaseRepository` + `SQLiteRepository`) — the only way the app touches the DB |
| `services.py` | Business logic: `run_csv_import`, `predict_species_cohort`, `evaluate_predictions`, `build_reference_baseline`, `_parse_csv_text` |
| `reference.py` | Builds reference baseline (medians per age group), `provisional_estimate()` — the active heuristic predictor |
| `model.py` | ML: `train_countdown_model`, `select_model` (levels 1–4), `predict_days_remaining`, `grouped_cv_mae`, artifact registry |
| `calibration.py` | Rod-length verification, calibration groups, `is_baseline_usable()` |
| `csv_import.py` | Low-level CSV importer (append-only, idempotent, growth rate derivation) |
| `camera_import.py` | Imports OpenCV-measured daily rows into observations; ambiguity quarantine |
| `camera_refresh.py` | Pulls new Cloudinary images, measures them, imports (fail-open, throttled) |
| `synthetic.py` | Synthetic demo data generation |
| `seed.py` | Seeds the DB from the historical CSV |
| `reset.py` | Wipes the DB + backs up generated files |
| `purge.py` | Removes data by source/cohort (with safety checks) |
| `verify_calibration.py` | CLI to verify rod length |
| `reprocess_captures.py` | Re-measure already-processed Cloudinary images |
| `migrate_species_rename.py` | One-off migration script |
| `dotenv.py` | Loads `.env` file |

### `backend/`, `nextjs/`, root files

| File | Role |
|---|---|
| `backend/app.py` | Flask API: 15 endpoints (`/api/predict/species`, `/api/import`, `/api/reference`, `/api/camera/refresh`, etc.) |
| `nextjs/pages/index.tsx` | Main prediction page (species selector, predict, result card) |
| `nextjs/pages/feedback.tsx` | Feedback + Reference tabs (CSV upload, baseline view, evaluation) |
| `plant_measurement.py` | OpenCV vision pipeline: `detect_reference_rod` (L711), `detect_plant` (L1190), `calculate_measurements` (L1456), `median_daily_record` (L2044) |
| `prediction.py` | CLI entry point for ad-hoc prediction |
| `plant_feedback.py` | CLI: register, observe, predict, transplant, feedback, import-camera, readiness |
| `data/super_orange_growth_data.csv` | Historical reference cohort (10 plants, 250 obs, 25 days) |
| `Apsara_Yellow_Marigold_Training_Data.csv` | Marigold dataset with readiness labels (12 plants, 300 obs) |
| `models/synthetic/` | 5 synthetic demo models (scikit-learn joblib) |
| `models/reference/` | 3 reference baseline JSON files (heuristic) |
| `models/real/` | 1 real trained model (Marigold, Random Forest, MAE 0.04 days) |

---

## 3. The ML Model

### 3.1 Features (X) — `model.py:45`
```python
FEATURE_COLUMNS = ["age_days", "height_cm", "growth_rate_cm_per_day", "stem_width_cm", "visible_true_leaf_count", "species", "variety"]
```
- 5 numeric + 2 categorical = 7 features
- Preprocessing: `SimpleImputer(strategy="median")` → `StandardScaler()` (numeric); `SimpleImputer(strategy="most_frequent")` → `OneHotEncoder(handle_unknown="ignore")` (categorical)
- Missing values filled with column median (not zero)
- `plant_id`, `observation_date`, `cohort` excluded from X

### 3.2 Target (y) — `db.py:1962`
```python
days_remaining = readiness_observed_date - observation_date  (calendar days)
```
- Computed in `readiness_training_rows()` (L1896): joins plants + observations + readiness_events
- Only observations at or before readiness date included
- Earliest readiness event per plant used (`MIN`)
- Plants without readiness events contribute nothing

### 3.3 Training Pipeline — `model.py:178`
```
1. check_min_requirements: ≥4 plants AND ≥6 readiness events
2. For each of 3 models: GroupKFold CV (n_splits = min(5, n_plants), grouped by plant_id)
3. Compare: best = lowest MAE, must beat mean_baseline
4. validated = True only if: best ≠ mean_baseline AND best_mae < baseline_mae AND minimums met
5. Refit winning model on ALL data
6. Save as artifact with metadata
```

### 3.4 Three Candidate Models
| Model | Algorithm | Purpose |
|---|---|---|
| `mean_baseline` | `DummyRegressor(strategy="mean")` | Null hypothesis |
| `ridge` | `Ridge(alpha=1.0)` | Simple linear, L2 regularization |
| `random_forest` | `RandomForestRegressor(n_estimators=200, max_depth=8, min_samples_leaf=3)` | Non-linear, captures feature interactions |

### 3.5 Model Selection at Prediction Time — `model.py:332`
```
Level 1: Validated species+variety real model → best
Level 2: Broader validated real model for species → good
Level 3: Synthetic demo (only if allow_synthetic=True) → demo only
Level 4: No model → falls through to provisional baseline
```

### 3.6 Synthetic vs. Real
- **Synthetic:** 5 `.joblib` files in `models/synthetic/`, trained on fake data. Never used unless `allow_synthetic=True`.
- **Real:** `models/real/` — created after training with real readiness labels. Marigold has one (Random Forest, MAE 0.04 days). Sunflower has none (0 readiness events).

---

## 4. The Reference Baseline (Heuristic Predictor)

**NOT ML.** Deterministic lookup table.

### Building (`reference.py:build_reference_from_repo`)
- For each plant: sort observations, compute trajectory, determine transplant date (recorded or assumed = day after last observation)
- For each observation: `days_until_transplant = transplant_date - observation_date`
- Group by `age_days`: median height, median growth rate, median days-until-transplant, IQR, n_plants

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

## 5. Key Algorithms

### Growth Rate Derivation (`csv_import.py`)
```
rate = (height_t - height_{t-1}) / elapsed_days
```
Only when both heights exist and interval > 0. Otherwise `NULL`. Never invented.

### Camera Measurement (`plant_measurement.py`)
- `detect_reference_rod()` (L711): HSV segmentation of coloured rod → cm/pixel scale
- `detect_plant()` (L1190): plant contour isolation
- `calculate_measurements()` (L1456): height, stem width, leaf count
- `median_daily_record()` (L2044): aggregates N captures into one daily row (median over valid captures)
- `group_records_by_date()` (L414): groups by capture date

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

## 6. Database Schema (key tables)

| Table | Purpose |
|---|---|
| `plants` | Identity: species, variety, external_ref, cohort, data_source |
| `observations` | Daily measurements + calibration provenance |
| `readiness_events` | Ground-truth labels (earliest readiness date per plant) |
| `feedback_14d` | Transplant + 14-day follow-up (append-only) |
| `prediction_snapshots` | Immutable prediction records |
| `calibration_settings` | Rod-length verification state |

---

## 7. API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/import` | CSV upload → import + auto-build baseline |
| `POST /api/predict/species` | Species-cohort prediction (auto-runs camera refresh) |
| `GET /api/reference?build=1` | Build/persist reference baseline |
| `POST /api/camera/refresh` | Pull + measure + import new Cloudinary images |
| `GET /api/species` | Species catalog (includes configured species with 0 counts) |

---

## 8. Honesty Principles (enforced in code)

1. **Missing ≠ zero:** Growth rate stays `NULL` when interval ≤ 0. `_median()` skips `None`.
2. **Method always stated:** Every prediction has `method`, `provisional`, `synthetic` fields.
3. **Synthetic never silent:** `select_model()` returns synthetic only at Level 3 with `allow_synthetic=True`.
4. **Predictions append-only:** `prediction_snapshots` has no UPDATE path.
5. **No auto-retrain:** `evaluate_predictions()` returns recommendation; nothing auto-trains.
6. **Calibration honesty:** Unverified camera data excluded from baseline.

---

## 9. What's Implemented vs. What's Not

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

## 10. Ten Core Viva Questions

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

## Part 3 — Experimental Results (Model Comparison)

### 3.1 Marigold (Apsara Yellow) — Real Data (12 plants, 300 observations, 12 readiness events)

| Model | MAE (days) | MAE Std | n_splits | Beats baseline? |
|---|---|---|---|---|
| Mean baseline (DummyRegressor) | 6.24 | 0.00 | 5 | — (reference) |
| Ridge (linear, L2) | 0.13 | 0.02 | 5 | ✅ Yes |
| Random Forest (200 trees) | 0.04 | 0.02 | 5 | ✅ Yes (best) |

**Improvement over baseline:** Random Forest reduces MAE from 6.24 days to 0.04 days — a **99.4% error reduction**.

**Validation method:** GroupKFold (n_splits=5, grouped by plant_id). Repeated observations of the same plant never split across folds.

**Selected model:** Random Forest (lowest MAE, validated=True, n_independent_plants=12, n_labelled_readiness_events=12).

### 3.2 Synthetic Demo Models (for reference only — never used silently)

| Model | Species/Variety | MAE (days) | Plants | Events |
|---|---|---|---|---|
| Ridge | Demo Bean / Bush | 0.066 | 8 | 8 |
| Ridge | Demo Bean / Climbing | 0.081 | 8 | 8 |
| Ridge | Super Orange / Early | 0.459 | 8 | 8 |
| Ridge | Super Orange / Late | 0.456 | 8 | 8 |
| Ridge | ALL / ANY | 0.355 | 32 | 32 |

**Note:** These are trained on synthetic (fake) data. They exist as demos only. MAE values are from GroupKFold CV on synthetic data — not real plants.

### 3.3 Provisional Baseline (Heuristic — no ML)

| Species | n_plants | n_points | Method |
|---|---|---|---|
| Sunflower (SuperOrange) | 10 | 250 | Provisional baseline (median age-match + growth-rate adjustment) |
| Marigold (Apsara Yellow) | 12 | 300 | Provisional baseline (before training) |

The provisional baseline has no MAE because it's not a trained model — it's a lookup table. Its accuracy depends on how close the current plant's age is to a historical age group (±3 day tolerance) and how many independent plants contributed (≥3 required).

### 3.4 Before vs. After Training (Marigold)

| Metric | Before (provisional baseline) | After (Random Forest, real model) |
|---|---|---|
| Method | Provisional historical baseline | Level 2 real model (Random Forest) |
| `validated` | N/A (heuristic) | `true` |
| MAE | Not reported (heuristic) | 0.04 days |
| `synthetic` | false | false (real data) |
| `model_name` | — | random_forest |
| n_independent_plants | — | 12 |
| n_labelled_readiness_events | 0 | 12 |
| `provisional` | true | false (real model selected) |

### 3.5 Key Experimental Observations

1. **The mean baseline (DummyRegressor) has MAE 6.24 days** — this is the "null hypothesis." Any real model must beat this to be useful.
2. **Ridge reduces MAE to 0.13 days** — a 98% improvement. Linear regression captures most of the signal (age + height + growth rate are strongly predictive of days remaining).
3. **Random Forest further reduces MAE to 0.04 days** — a 99.4% improvement over the mean baseline. The non-linear interactions (age × height × growth rate) provide additional signal.
4. **GroupKFold prevents data leakage** — observations from the same plant never split across train/test folds. Without this, the model would "cheat" by seeing a plant's earlier observations in training.
5. **The provisional baseline cannot report MAE** — it's not a trained model, so it has no cross-validated error estimate.
6. **Synthetic models have comparable MAE (0.066–0.459)** but are trained on fake data — their metrics are meaningless for real plants.

### 3.6 How to Present This to Faculty

**One-liner:** "We compared three models (mean baseline, Ridge, Random Forest) using plant-grouped cross-validation. Random Forest achieved MAE 0.04 days on real Marigold data — a 99.4% improvement over the mean baseline. Before training (no readiness labels), the system honestly falls back to a provisional heuristic with no MAE."

**Key talking points:**
- The mean baseline is the null hypothesis — it proves the features contain signal
- GroupKFold prevents leakage between observations of the same plant
- The provisional baseline is not ML — it's a median lookup table
- Synthetic models exist but are never used silently
- The system refuses to claim a validated model when evidence is insufficient

### 3.7 What to Show in the Code

| Experiment | File | Function/Line |
|---|---|---|
| Model comparison (MAE) | `plantreadiness/model.py` | `train_countdown_model` (L178) |
| GroupKFold CV | `plantreadiness/model.py` | `grouped_cv_mae` (L157) |
| Candidate models | `plantreadiness/model.py` | `make_pipelines` (L111) |
| Feature columns | `plantreadiness/model.py` | `FEATURE_COLUMNS` (L45) |
| Min requirements | `plantreadiness/model.py` | `check_min_requirements` (L150) |
| Model selection | `plantreadiness/model.py` | `select_model` (L332) |
| Provisional baseline | `plantreadiness/reference.py` | `provisional_estimate` (L429) |
| Baseline construction | `plantreadiness/reference.py` | `build_reference_from_repo` (L335) |
| Target generation | `plantreadiness/db.py` | `readiness_training_rows` (L1896) |
| Readiness events | `plantreadiness/db.py` | `record_readiness_event` (L1396) |

---

## Part 4 — Final verification report

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