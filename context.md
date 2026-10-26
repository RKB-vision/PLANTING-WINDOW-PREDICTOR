# Project Context — Plant Transplant Readiness Prediction System

## What This System Does

A plant-monitoring system that estimates when a plant species is likely ready for transplantation. It photographs plants with an ESP32-CAM, measures them with OpenCV (height, stem width, leaves), stores observations in SQLite, and predicts days-until-transplant using either a provisional heuristic baseline or a trained ML model (Ridge/Random Forest). It is deliberately honest: method, uncertainty, and limitations are always stated.

## Branch: simplified-architecture (6 core files)

| File | Role |
|---|---|
| `db.py` | SQLite data layer: schema, seeding, temporal growth rate, training rows, predictions, feedback, plant ID normalization, calibration provenance, species cohort snapshot |
| `vision.py` | OpenCV: rod detection (px→cm), plant segmentation, height/stem width/leaf count, median daily record |
| `ml.py` | ML countdown predictor: BaselineTrajectory, GroupKFold CV (Dummy/Ridge/RF), fallback ladder, prediction, evaluation |
| `app.py` | Streamlit UI: species selector, date picker, Cloudinary fetch, image carousel, Calibrate & Predict, kid-friendly result |
| `cli.py` | CLI: seed, measure, train, predict, evaluate, register, refresh |
| `cloudinary_refresh.py` | Pull images from Cloudinary, measure with OpenCV, import observations (throttled, fail-open) |
| `test_simplified.py` | 6 tests (all passing) |

## Key Design Principles

1. **Missing ≠ zero** — growth rate stays NULL when interval ≤ 0 or heights missing
2. **Method always stated** — every prediction has `method`, `provisional`, `synthetic` fields
3. **Synthetic never silent** — only at Level 3 with `allow_synthetic=True`
4. **Predictions append-only** — no UPDATE path on predictions/feedback
5. **No auto-retrain** — evaluate returns recommendation; human decides
6. **Calibration honesty** — unverified camera data quarantined, excluded from baseline
7. **Plant ID normalization** — case/whitespace/hyphen/underscore-insensitive, zero-padding significant
8. **Ambiguity quarantine** — colliding refs never resolve to first match

## ML Model

- **Features (X):** age_days, height_cm, growth_rate_cm_per_day, stem_width_cm, visible_true_leaf_count, species, variety
- **Target (y):** days_remaining = readiness_observed_date - observation_date
- **3 candidates:** mean_baseline (DummyRegressor), ridge (Ridge, alpha=1.0), random_forest (200 trees, max_depth=8)
- **Validation:** GroupKFold on plant_id (no leakage), MAE in days
- **Min requirements:** ≥4 plants AND ≥6 readiness events
- **Validated:** True only if best model beats mean baseline AND minimums met
- **Selection:** Level 1 (validated species+variety) → Level 2 (broader real) → Level 3 (synthetic, opt-in) → Level 4 (insufficient evidence → provisional baseline)
- **Marigold result:** Random Forest, MAE 0.04 days, 12 plants, 12 readiness events

## Reference Baseline (Heuristic — NOT ML)

- Lookup table: median height, growth rate, days-until-transplant per age group
- Predict: find nearest age group (±3 days), read median days-to-transplant, adjust by growth rate (clamp 0.5–2.0×)
- Always labeled `provisional: true`
- Transplant date assumed = day after last monitoring day (flagged)

## Calibration

- Groups: manual_measurement, camera_verified, camera_unverified, unknown
- BASELINE_USABLE = (manual_measurement, camera_verified)
- Verification tied to specific rod length — changing length resets verification
- Camera observations stored even when unverified (flagged, excluded from baseline)

## Cloudinary Integration

- `CAMERA_SOURCES` in `.env`: `Plant02_=Plant02` (prefix → external_ref)
- `CAMERA_ROD_LENGTH_CM` in `.env` (default 12.0)
- `CAMERA_MIN_REFRESH_INTERVAL_SECONDS` (default 60, set 0 to disable)
- Throttled, fail-open, bounded to 4 captures per pass
- Processed IDs tracked in `data/generated/processed_cloudinary_ids.txt`

## Database Schema (simplified)

- `plants`: plant_id (TEXT PK), species, variety, external_ref, cohort, planted_date
- `observations`: id, plant_id, observation_date, age_days, height_cm, stem_width_cm, visible_true_leaf_count, growth_rate_cm_per_day, source, calibration_source, calibration_verified, rod_length_cm
- `readiness_events`: id, plant_id, event_date, status, notes
- `predictions`: id, plant_id, species, prediction_date, predicted_days_remaining, predicted_readiness_date, method, confidence, supporting_evidence
- `feedback`: id, prediction_id, plant_id, stage, assessment, actual_transplant_date, condition, notes

## CLI Commands

```bash
./run.sh                          # Launch Streamlit dashboard
./reset.sh                        # Reset DB (backup first)
./reset.sh --seed                 # Reset + seed from CSV
python cli.py seed --csv <path>   # Seed from CSV
python cli.py measure <image>     # Measure plant from image
python cli.py train --species X   # Train ML model
python cli.py predict --species X # Predict readiness
python cli.py evaluate            # Evaluate predictions
python cli.py register --plant-id X --species Y  # Register live plant
python cli.py refresh --species X  # Pull from Cloudinary
```

## UI Flow (Streamlit)

1. Select species → dropdown
2. Pick date → "Use latest" checkbox or manual date picker
3. Click "Predict"
   - If measurements exist → show them, predict
   - If no measurements → modal: "Plant details are not calibrated"
4. In modal: "Pull parameters" → fetch from Cloudinary, measure with OpenCV
5. Image carousel (left/right) → view all images with measurements
6. "Calibrate & Predict" → train model (if possible), predict
7. Result: days remaining + readiness date + which model + kid-friendly explanation

## Honesty Principles

1. Missing ≠ zero (growth rate NULL when interval ≤ 0)
2. Method always stated (method, provisional, synthetic fields)
3. Synthetic never silent (only Level 3 with allow_synthetic=True)
4. Predictions append-only (no UPDATE path)
5. No auto-retrain (evaluate returns recommendation; human decides)
6. Calibration honesty (unverified camera data quarantined)

## What's Implemented vs. Not

| Feature | Status |
|---|---|
| Camera measurement (OpenCV) | ✅ |
| CSV import (append-only, idempotent) | ✅ |
| Reference baseline (provisional) | ✅ Active predictor |
| Trained real-data model | ✅ Possible (after CSV with readiness labels) |
| Synthetic demo model | ❌ Not in simplified branch |
| Prediction snapshot (append-only) | ❌ Not in simplified branch |
| Feedback (transplant + condition) | ✅ Basic |
| 14-day follow-up | ❌ Not in simplified branch |
| Evaluation (MAE, bias) | ✅ Basic |
| Auto-retrain | ❌ By design |
| Firmware (ESP32-CAM) | ❌ External |
| Cloudinary integration | ✅ Added |
| Plant ID normalization | ✅ Added |
| Calibration provenance | ✅ Added |

## Experimental Results

### Model Comparison — Marigold (Apsara Yellow), 12 plants, 300 obs, 12 readiness events

| Model | MAE (days) | MAE Std | Beats baseline? |
|---|---|---|---|
| Mean baseline (DummyRegressor) | 6.24 | 0.00 | — |
| Ridge (linear, L2) | 0.13 | 0.02 | ✅ Yes |
| Random Forest (200 trees) | 0.04 | 0.02 | ✅ Yes (best) |

**Improvement:** Random Forest reduces MAE from 6.24 → 0.04 days (99.4% error reduction).

**Validation:** GroupKFold (n_splits=5, grouped by plant_id).

### Provisional Baseline (Heuristic — no ML)

| Species | n_plants | n_points | Method |
|---|---|---|---|
| Sunflower (SuperOrange) | 10 | 250 | Provisional baseline |
| Marigold (Apsara Yellow) | 12 | 300 | Provisional baseline (before training) |

### Before vs. After Training (Marigold)

| Metric | Before (provisional) | After (Random Forest, real) |
|---|---|---|
| Method | Provisional historical baseline | Level 2 real model (Random Forest) |
| validated | N/A | true |
| MAE | Not reported | 0.04 days |
| synthetic | false | false (real data) |
| model_name | — | random_forest |
| n_independent_plants | — | 12 |
| n_labelled_readiness_events | 0 | 12 |
| provisional | true | false |

## Known Limitations

1. No real trained model for Sunflower (0 readiness events)
2. Provisional baseline assumes transplant = day after last monitoring day
3. Camera data is unverified by default
4. No firmware in repo (ESP32-CAM code is external)
5. No auto-retrain (by design)
6. Single-species reference (only Super Orange has historical data)
7. No prediction snapshots (predictions not persisted)
8. No 14-day feedback follow-up (basic feedback only)

## Next Steps

1. Record real transplant outcomes via feedback loop for Sunflower
2. Train and validate a real model for Sunflower
3. Verify camera calibration (`verify_calibration --rod-length-cm N --verified`)
4. Add 14-day feedback follow-up to simplified branch
5. Add prediction snapshot persistence
6. Add synthetic demo models (optional, for demo purposes)