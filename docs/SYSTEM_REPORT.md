# Plant Readiness System — Detailed Working-Mechanism Report

**Repository:** `RKB-vision/PLANTING-WINDOW-PREDICTOR` (branch `main`)
**Generated:** 2026-10-17
**Companion docs:** `docs/ARCHITECTURE.md` (canonical), `docs/DEPLOYMENT.md`, `docs/REPORT.md`, `docs/audit/broken_test.md`

This report describes the end-to-end working mechanism of the plant-readiness
system: how data flows from the camera through measurement, aggregation,
prediction, the user interface, feedback, and evaluation. It is a narrative
companion to `docs/ARCHITECTURE.md`, which remains the canonical source of
truth.

---

## 1. Purpose

The system estimates **when a plant species is likely to be ready for
transplantation**, using (a) camera-derived measurements of individual plants
and (b) historical growth data from a cohort of plants that grew successfully.
It is *honest by construction*: predictions always state their method,
supporting measurements, uncertainty, and limitations, and never fabricate
growth. Missing values remain missing (they are not zero and not invented).

---

## 2. High-level data flow

```
ESP32-CAM  ──►  Cloudinary (cloud images)
                     │
                     ▼
        plant_measurement.py  (OpenCV)
        height, stem width, true leaves, rod calibration
                     │
                     ▼
        plant_daily_measurements.csv  (data/generated/, git-ignored)
                     │
                     ▼
        plantreadiness.camera_import  ──►  observations (SQLite)
                     │
                     ▼
        plantreadiness.services  ──►  species-level aggregation
        (medians + contributing plant counts)
                     │
                     ▼
        prediction.py  ──►  readiness date + method + uncertainty
                     │
                     ▼
        Next.js UI  (select species → predict)
                     │
                     ▼
        Feedback: assessment / transplant record / 14-day follow-up
                     │
                     ▼
        plantreadiness services  ──►  evaluation (feedback-driven,
                                      controlled improvement)
```

---

## 3. Components

### 3.1 Measurement pipeline — `plant_measurement.py`

- **Role:** ESP32-CAM → Cloudinary → OpenCV. Downloads images, segments the
  plant, measures height / stem width / true leaves using a reference rod for
  calibration.
- **Key constants:** `CAPTURED_IMAGE`, `RESULT_IMAGE`, `CSV_FILE`
  (`data/generated/plant_daily_measurements.csv`), `PROCESSED_IDS_FILE`.
- **Key functions:** `download_cloudinary_image`, `process_image`,
  `capture_record_from_result`, `median_daily_record`, `upsert_daily_row`,
  `cloudinary_capture_timestamp`.
- **Output:** one CSV row per plant/day (`Day, Plant, Date, Height_cm, …`),
  appended via `upsert_daily_row` (read-modify-write; see audit AUD-08).
- **Calibration:** rod length in cm must be physically measured and verified via
  `python -m plantreadiness.verify_calibration`; until then the value is
  `UNVERIFIED`. Default 30 cm is **not** assumed correct.

### 3.2 Domain / data layer — `plantreadiness/`

A package that owns all persistence and domain logic. CLI entry points stay thin.

| Module | Responsibility |
|---|---|
| `config.py` | Paths (`BASE_DIR`, `DEFAULT_DB_PATH`, `DEFAULT_REAL_CSV`, `CAMERA_CSV_PATH`, `MODELS_DIR`, `REFERENCE_DIR`, `DEMO_DB_PATH`, `DEMO_MODELS_DIR`), `DEFAULT_SPECIES`, `LEGACY_SPECIES_SUPER_ORANGE` |
| `db.py` | SQLite connection (`connect`), schema (`init_db`), CRUD (`register_plant`, `add_observation`, `add_capture`, …), revisions, calibration settings |
| `repository.py` | `SQLiteRepository` abstraction over the DB |
| `services.py` | Business logic: CSV import, species aggregation, readiness assessment, evaluation, prediction orchestration, calibration status |
| `csv_import.py` | Import a CSV into `observations` (tags `cohort='reference'`, `calibration_source=CALIBRATION_SOURCE_CSV`) |
| `seed.py` | Idempotent seed: import the reference cohort from `data/super_orange_growth_data.csv` + ensure the reference baseline |
| `reference.py` | Growth reference / baseline: `baseline_path`, `save_baseline`, `load_baseline`, `baseline_summary`, `baseline_usable` |
| `model.py` | ML model training, selection, metrics (vs. baseline mean) |
| `synthetic.py` | Synthetic data generation (always labelled `SYNTHETIC DEMO`) |
| `calibration.py` | Calibration provenance / usability gating |
| `camera_import.py` | Import daily measurements CSV into `observations` |
| `camera_refresh.py` | Bounded, fail-open single-pass refresh the backend runs before a species prediction |
| `reprocess_captures.py` | Re-process stored captures |
| `purge.py` | Remove synthetic rows only (`data_source='synthetic'`); never touches real data |
| `migrate_species_rename.py` | One-time rename `Super Orange` → `Sunflower (SuperOrange)` (dry-run by default) |
| `verify_calibration.py` | Record a verified rod length |
| `reset.py` | Wipe + rebuild the runtime DB from the committed CSV (see §7) |
| `dotenv.py` | Load `.env` into the process environment |

### 3.3 Backend API — `backend/app.py` (Flask, port 8000)

Endpoints (selected):
- `GET /plants` — list plants
- `POST /register` — register a plant
- `POST /predict` — predict for a plant
- `GET /api/species` — species catalog (with observation counts, available dates, readiness-event counts)
- `POST /api/predict/species` — species-level prediction
- `POST /api/camera/refresh` — trigger camera refresh
- `POST /api/import` — upload a measurement CSV (JSON `csv_text` or multipart
  `file`), all-or-nothing; auto-builds the reference baseline so predictions
  work from day 1
- `GET /api/reference` — reference baseline summary
- `GET /api/calibration` — calibration status

`backend/app.py` adds the repo root to `sys.path` so it can import
`plantreadiness.*` and `prediction`.

### 3.4 Frontend — `nextjs/`

Next.js app. Select a species → request a prediction → display readiness date,
method, supporting measurements, uncertainty, limitations. Shared design system
in `nextjs/styles/globals.css` (neutral surfaces, restrained green accent, no
emoji).

### 3.5 Prediction entry point — `prediction.py`

- `predict_for_plant(conn, plant_id, models_dir, …)` — single-plant prediction.
- CLI (`--plant`, `--species`, `--variety`, `--age-days`, `--height-cm`,
  `--growth-rate`, `--stem-width-cm`, `--true-leaves`, `--db`, `--models-dir`,
  `--allow-synthetic`, `--json`).
- **Fallback order (honest):**
  1. Validated model (real, evidence-backed)
  2. Provisional historical baseline (species medians from the reference cohort)
  3. Synthetic demo model — **only** if explicitly enabled (`--allow-synthetic`),
     always labelled `SYNTHETIC DEMO`
  4. Insufficient evidence — prediction unavailable, no fabrication

### 3.6 Feedback CLI — `plant_feedback.py`

Subcommands: `register`, `observe`, `feedback`/assessment, `import-camera`, etc.
- `register` — register a plant (species, variety, external ref, data source).
- `import-camera` — import from the daily measurements CSV (`--csv`,
  default `data/generated/plant_daily_measurements.csv`).
- `--species` default is the legacy `"Super Orange"` (note: known stale default;
  the current canonical species is `"Sunflower (SuperOrange)"`).

### 3.7 Training — `model_train.py`

- `--mode real` (uses `config.DEFAULT_REAL_CSV` = `data/super_orange_growth_data.csv`)
  or `--mode demo` (synthetic, isolated DB/models).
- Trains and evaluates models, compares against the baseline mean.

### 3.8 Demo — `demo_workflow.py`

Scripted end-to-end synthetic demonstration (Mode A): synthetic DB → train demo
models → register → observe → predict (labelled synthetic) → transplant →
14-day feedback → retrain → compare. Used by `tests/test_integration.py` (2 tests).

### 3.9 Purge — `plantreadiness/purge.py`

Removes only `data_source='synthetic'` rows. Real data is never touched. Used by
`tests/test_purge.py` and documented in README.

---

## 4. Record types (kept distinct)

| Type | Storage | Meaning |
|---|---|---|
| Raw observation | `observations` | A single measurement of one plant on one day |
| Species-level summary | computed in `services` | Medians + contributing plant counts per species |
| Prediction | `prediction_history` | A dated prediction with method, inputs, uncertainty |
| Feedback | `readiness_assessments`, `feedback_14d` | Assessment / transplant record / 14-day follow-up |
| Readiness event | `readiness_events` | A recorded transplant-readiness event (ground truth) |
| Camera capture | `camera_captures`, `capture_revisions` | A single ESP32-CAM capture with validity |
| Calibration | `calibration_settings` | Verified rod length / calibration provenance |

Raw observations, species summaries, predictions, and feedback are **different
things and stay distinguishable**. Predictions and feedback are append-only;
later feedback links to an earlier prediction without rewriting it. Missing
feedback is *pending*, never failure/death.

---

## 5. Database schema (SQLite)

Tables in `plant_readiness.db`:

| Table | Purpose |
|---|---|
| `plants` | Registered plants (species, variety, external_ref, cohort, data_source, calibration) |
| `observations` | Raw per-plant per-day measurements (height, stem width, true leaves, age) |
| `camera_captures` | ESP32-CAM captures (public_id, validity, rod length, height, confidence) |
| `capture_revisions` | Revision history for captures |
| `observation_revisions` | Revision history for observations |
| `readiness_events` | Recorded transplant-readiness events (ground truth) |
| `prediction_history` | Append-only prediction records (method, inputs, uncertainty, version) |
| `readiness_assessments` | Assessment records linked to predictions |
| `feedback_14d` | 14-day follow-up outcomes |
| `calibration_settings` | Verified calibration values + provenance |
| `sqlite_sequence` | SQLite autoincrement bookkeeping |

Migrations are forward-only and go through the repository abstraction. `init_db`
creates tables if absent.

---

## 6. Prediction honesty rules

- Always state the **method** (validated model / provisional historical
  baseline / synthetic demo / insufficient evidence).
- Always state the **supporting measurements** (height, stem width, true leaves,
  age, growth rate).
- Always state **uncertainty** and **limitations**.
- **Never fabricate growth.** With a single measurement, growth rate is `NULL`.
- Synthetic data/models carry a persistent `SYNTHETIC DEMO` indicator and are
  never used for a real plant unless explicitly enabled.

---

## 7. Reset mechanism (`./reset`)

`plantreadiness/reset.py` + the executable `reset` wrapper:

1. Back up the current DB via SQLite's online backup API →
   `data/generated/backups/plant_readiness.db.bak-<timestamp>` (git-ignored).
2. Delete the DB + `-wal`/`-shm`/`-journal` sidecars.
3. Rebuild from `data/super_orange_growth_data.csv` via `seed.seed_database`
   (`data_source='real'`, `cohort='reference'`,
   `calibration_source='csv_import_no_calibration'`).
4. Ensure the successful-cohort reference baseline exists.
5. Refuses to run if the CSV is missing (no wipe without a rebuild source).
6. Clears `processed_cloudinary_ids.txt` so camera refresh can import captures
   again after the runtime database has been rebuilt.

Flags: `--no-backup`, `--rebuild-baseline`, `--camera` (also clears downloaded
camera images and the `captures/` cache), `--json`.

---

## 8. Camera refresh (fail-open, single-pass)

`plantreadiness/camera_refresh.py` runs before a species prediction when
configured:
- Auto-enables only when Cloudinary credentials **and** at least one
  `CAMERA_SOURCES` mapping are present.
- Single pass, append-only, never invents growth.
- Quarantined (`camera_unverified`) rows must not drive predictions (see audit
  AUD-05 — currently they do; documented defect).

---

## 9. Calibration provenance

- `calibration_source` distinguishes `csv_import_no_calibration` (baseline-usable)
  from `unknown`/`camera_unverified` (quarantined).
- `camera_unverified` rows are excluded from baselines by `calibration.py`.
- Audit AUD-03 / AUD-09 document defects where `calibration_source` is stored
  as `NULL` or reclassified by `init_db`.

---

## 10. Evaluation & controlled improvement

- Feedback is append-only and links to an earlier prediction.
- `services` compute evaluation metrics from feedback (transplant outcomes,
  14-day follow-ups).
- **No auto-retrain after every submission.** A new method replaces an old one
  only when evaluation supports it (controlled, evidence-based).

---

## 11. Testing

- Suite: **219 passed, 11 xfailed** (Python, `pytest`).
- `tests/test_audit_adversarial.py`: 10 guards + 11 `xfail(strict=True)` pinning
  documented defects AUD-01..09.
- `tests/test_reset.py`: 7 reset tests.
- Additional suites: `test_backend_api`, `test_calibration`, `test_camera_refresh`,
  `test_dates`, `test_integration`, `test_model`, `test_purge`, `test_seed`.
- Run: `python3 -m pytest -q`.

---

## 12. Deployment

- `./run.sh` — loads `.env`, sets `PYTHONPATH`, starts Flask (`backend/app.py`,
  port 8000) and the Next.js dev server.
- `requirements.txt` — Python deps.
- `.env` (gitignored) — Cloudinary + camera config; template in `.env.example`.
- Database: `plant_readiness.db` (gitignored); rebuild with `./reset` or
  `python -m plantreadiness.seed`.
- Models: `models/` (tracked: reference baselines + synthetic `.joblib` +
  `registry.json`).

---

## 13. Non-negotiable principles (from `AGENTS.md`)

1. Use the existing system; no parallel system.
2. Keep record types distinct.
3. Never invent data; missing stays missing; single measurement → growth rate `NULL`.
4. Honest predictions (method, measurements, uncertainty, limitations).
5. Synthetic ≠ real (persistent `SYNTHETIC DEMO`, never used for real plant
   unless enabled).
6. Predictions/feedback append-only; missing feedback = pending.
7. Improve with evidence, not assumptions; no auto-retrain.
8. No destructive changes without approval.

---

## 14. Audit findings (summary)

`docs/audit/broken_test.md` (sections A–J) documents 16 defects (AUD-01..16),
including high-severity: AUD-05 (quarantined unverified-camera rows drive live
predictions without disclosure), AUD-02 (`evaluate_predictions` counts a deferral
as a transplant via `is False`), AUD-03 (`run_csv_import` stores
`calibration_source=NULL`, quarantining uploaded manual data). Full prioritized
remediation is in that file.

---

## 15. Repository layout (post-restructuring)

```
EMBEDDED/
├─ AGENTS.md  README.md  requirements.txt  .env.example  .gitignore
├─ run.sh  reset
├─ backend/            Flask API (app.py)
├─ nextjs/             Next.js UI
├─ plantreadiness/     domain + data layer (18 modules)
├─ plant_measurement.py  OpenCV measurement pipeline
├─ prediction.py         prediction / model-selection entry point
├─ plant_feedback.py     CLI (observations, feedback, camera import)
├─ model_train.py        training entry point
├─ demo_workflow.py      synthetic end-to-end demo
├─ data/
│   ├─ super_orange_growth_data.csv               (committed reference cohort)
│   ├─ super_orange_growth_data_with_growth_rate.csv
│   └─ generated/                                (git-ignored runtime output)
├─ models/
│   ├─ reference/baseline_*.json
│   └─ synthetic/*.joblib + registry.json
├─ docs/
│   ├─ ARCHITECTURE.md  DEPLOYMENT.md  REPORT.md  plan.md
│   └─ audit/broken_test.md
└─ tests/              219 passing + 11 xfailed audit guards
```

---

*End of report.*
