# Plan — Four-Capture Daily Aggregation, Species Rename/Add, Editable Reference Sheet, Present-Day Characteristics

Status: **PLANNED, NOT IMPLEMENTED.** This file is the memory of the plan. Nothing in the
repository has been modified. Execute after leaving Plan mode.

Open decisions (§9): **all resolved** — see §9 for the confirmed answers.

Repo: `/Users/visionary/Documents/CODING/EMBEDDED` (branch `main`, HEAD `feee01b`)

---

## 0. Ground rules

- Reuse `plantreadiness/`, `backend/`, `nextjs/`. No parallel system, no DB reset, no deletion of
  historical records, no credential changes, no removal of existing components.
- Never invent data: missing stays `NULL`, one capture stays low-confidence, no age from height.
- Keep rod-length calibration (`cm_per_pixel = rod_length_cm / reference_pixels`) untouched.
- Keep the Cloudinary naming format `Plant01_YYYYMMDD_HHMMSS` unchanged.
- No unrelated refactoring. Smallest coherent change set.
- Run `python3 -m pytest -q` before and after (baseline: **196 passing**).

---

## 1. Current flow, traced (as-is, with line references)

### 1.1 Capture → measurement (offline CLI, `plant_measurement.py`)

| Step | Where | What happens |
|---|---|---|
| List cloud images | `get_cloudinary_images()` ~L211 | Cloudinary Admin API, by prefix |
| Filter already-seen | `find_new_cloudinary_images()` L318, filter L341-345 | skips IDs in `processed_cloudinary_ids.txt` (`get_processed_ids()` L285) |
| Download | `download_cloudinary_image()` L360 | local JPG in `BASE_DIR` |
| Measure | `process_image()` L1528 | `detect_reference_rod` → `detect_plant` → `calculate_measurements()` L1200 → `cm_per_pixel = rod_length_cm / reference_pixels` L1216, `height_cm = (base_y - top_y + 1) * cm_per_pixel` L1223, `width_cm = stem_width_pixels * cm_per_pixel` L1229 → `CaptureResult(timestamp, height_cm, width_cm, leaf_count, cm_per_pixel, confidence)` |
| Timestamp | **L1593-1596** | `if timestamp is None: timestamp = datetime.now()` |
| Mark seen | `mark_processed(public_id)` L307 | append to `processed_cloudinary_ids.txt` |
| Aggregate the run | `format_daily_record()` **L1673-1755** | **`np.mean`** of height / width / leaves / scale / confidence |
| Persist | `write_daily_record()` **L1758-1799** | **blind `"a"` append** of ONE row to `plant_daily_measurements.csv` |

CSV columns (`DAILY_RECORD_FIELDS` L1659-1670):
`day, plant_id, captures_used, average_height_cm, average_width_cm, average_visible_leaves, average_cm_per_pixel, average_confidence, first_capture_timestamp, last_capture_timestamp`

`plant_daily_measurements.csv` is **tracked by git** and currently holds 1 real row:
`Day-01, Plant-01, 1, 13.3758, 1.3376, 2.0000, 0.027298, 0.6641, 2026-10-15T19:26:40, 2026-10-15T19:26:40`

CLI main loop L2060-2157: processes up to `--captures` new images for `--plant-id`, calls
`process_image(image, args, capture_number=n)` **without a timestamp** → `datetime.now()`, then
`write_daily_record(f"Day-{args.day:02d}", args.plant_id, daily_results)`.
`--day` is only a CSV label; it does not set any date.

### 1.2 Capture → measurement (backend auto-refresh, `plantreadiness/camera_refresh.py`)

- `refresh_camera_observations()` L68 → per `CameraSource(prefix, external_ref)`:
- `_default_puller()` L292-319 → `build_measurement_args(rod_length_cm=config.CAMERA_ROD_LENGTH_CM)` L303
  → `collect_new_captures_once()` → in `plant_measurement.py` **L1820-1869**, passes
  `timestamp=cloudinary_capture_timestamp(resource)` i.e. Cloudinary `created_at` (L1802-1817).
- `_rows_from_results()` L322-339 → groups by date **within this single run only**, then
  `format_daily_record()` (mean) → `_append_rows()` L277-289 appends to
  `config.CAMERA_CSV_PATH` (`plant_daily_measurements.csv`).
- Then `camera_import.import_camera_csv(repo, buffer, auto_register=False, data_source="real")`.

### 1.3 CSV → database (`plantreadiness/camera_import.py`)

- Required columns: `plant_id, average_height_cm, first_capture_timestamp` (L42-46).
- Resolve plant from `external_ref` (or numeric id); **never auto-registers** in the refresh path
  (L245-243 skip when unregistered).
- `age_days` derived from `plants.date_started` only (L248-257) — never from height. `#89` has
  `date_started = NULL` → `age_days = NULL`.
- `growth_rate` derived from the **latest stored observation** using real elapsed days
  `days_between(latest.observation_date, obs_date)` (L259-274).
- Insert via `repo.add_observation()` L278; `DuplicateRecordError` → `observations_skipped_duplicate`
  **L290-291 — never updates an existing row**.
- `observation_date` = date part of `first_capture_timestamp` (L22-23, L280).

### 1.4 Storage (`plantreadiness/db.py`)

`observations` (L66-84): `observation_id, plant_id, observation_date, age_days, height_cm,
growth_rate_cm_per_day, canopy_width_cm, stem_width_cm, visible_true_leaf_count, data_source,
recorded_at` with **`UNIQUE (plant_id, observation_date)`** L83.
There is **no capture-level table** — individual images are not auditable in the DB.

### 1.5 Prediction

`backend/app.py` `/api/predict/species` L177-217 → `svc.refresh_camera_observations()`
(60 s throttle) → `svc.predict_species_cohort()` `services.py` L493-605 →
`repo.species_cohort_snapshot()`, medians across rows of the newest date → `predict_ad_hoc()` →
`_apply_provisional_baseline()` L693-729 → `reference.provisional_estimate()` L418-511:

- **age-first**: `age_days` present → matched by age ±`PROVISIONAL_AGE_TOLERANCE_DAYS` (3) and
  **height is ignored**;
- else height ±`PROVISIONAL_HEIGHT_TOLERANCE_CM` (1.5) vs group `median_height_cm`;
- `MIN_PROVISIONAL_PLANTS = 3` (config L71); `days_remaining = group.median_days_until_transplant`;
  `predicted_readiness_date = observation_date + days`.

Baseline path/filename: `reference._safe()` L370 → `models/reference/baseline_{species}_{variety}.json`.

---

## 2. Defects to fix (exact)

| # | Defect | Evidence |
|---|---|---|
| D1 | Daily aggregation is **per run**, not per (plant, calendar date) | `_rows_from_results` L322; main loop L2060 |
| D2 | Aggregation uses **mean**, spec requires **median** | `format_daily_record` L1685-1728 |
| D3 | CLI records `datetime.now()` instead of the capture timestamp **in the image name** | L1593-1596 + L2124-2128 |
| D4 | CSV append is **not idempotent** → a second run the same day adds a second daily row | `write_daily_record` L1758-1799 |
| D5 | Import **skips** duplicates → a later capture the same day is silently lost, the daily record never improves | `camera_import.py` L290-291 |
| D6 | No capture-level audit (public IDs, timestamps, per-image measurements, failures) | `db.py` L66-84 |
| D7 | No valid/failed capture counts, no low-confidence flag, no per-field "unavailable" | CSV lacks fields |
| D8 | Repository has **no `update_observation` / `delete_observation`** → blocks both the daily upsert and the editable sheet | `repository.py` L68, L78, L81, L307, L322, L325 |

Added by the second spec (measurement reliability, calibration, prediction roles, validation gate):

| # | Defect | Evidence |
|---|---|---|
| D9 | Rod length has **three competing sources**: `config.CAMERA_ROD_LENGTH_CM` hardcoded default `"30"` (`config.py` L35), the required CLI flag `--rod-length-cm` (L1908), and `.env` (currently **`10`**). README L69 and the CLI docstrings L27/35/44 all still say 30. One authoritative value does not exist. | verified by grep |
| D10 | No record of the rod length actually used per measurement → historical heights are not traceable to a calibration | no rod column in `observations` or in the CSV |
| D11 | Existing data **silently mixes calibrations**: the CSV row holds 13.3758 cm at rod 30 (`cm_per_pixel` 0.027298) while DB plant #89 holds 6.5854 cm at rod 15 | verified earlier |
| D12 | **Age suppresses height and growth rate** in the prediction: `if age is not None: group = nearest age group … elif height is not None: …` — height is only a fallback. Directly contradicts the required behaviour. | `reference.py` L439-448 |
| D13 | Nothing in the upload path prevents an uploaded dataset from being read as a *validated* method; promotion needs an explicit gate and provenance label | new upload path |
| D14 | Per-day capture accounting is absent: no expected / processed / valid / failed counts, so an incomplete day cannot be distinguished from a complete one | D7 |

Growth rate already complies with the spec (daily records, real elapsed days, L259-274). Keep it.

---

## 3. Where daily aggregation belongs

**At the measurement → CSV/import boundary, keyed by `(plant_ref, capture_date)`, recomputed from
ALL captures known for that date — not from one run's results.**

Pipeline after the change:

```
Cloudinary listing
  → filter processed_cloudinary_ids.txt
  → parse name:      Plant01_YYYYMMDD_HHMMSS → (plant_ref, capture_date, capture_time)
  → measure each image independently (existing OpenCV + rod calibration, unchanged)
  → validate each result (existing quality signals: confidence, detected rod/plant, positive values)
  → persist each capture (audit table camera_captures, unique by public_id)
  → for each (plant_ref, capture_date) with >=1 valid capture:
        median of height / stem width / leaf count across VALID captures
        valid_captures, failed_captures, low_confidence = (valid < 3)
  → UPSERT one daily record (CSV row + observations row)
  → derive growth rate from the previous DAILY observation, real elapsed days
```

Four images of `Plant01` on `2026-10-16` → exactly one record for `(Plant01, 2026-10-16)`.
Four images must never count as four days.

---

## 4. Data format changes

### 4.1 `plant_daily_measurements.csv` (tracked — extend, stay backward compatible)

Keep all 10 existing columns, in the existing order, and **append** new columns so the old row still
parses:

```
+ capture_date            (YYYY-MM-DD, stable grouping key)
+ rod_length_cm           (effective calibration used for this aggregate)
+ expected_captures       (int or empty when unknown — e.g. 4)
+ processed_captures      (int — images actually measured)
+ valid_captures          (int — measurements that passed validation)
+ failed_captures         (int — measured but invalid/failed)
+ incomplete              (0/1 — valid_captures < expected/reliable minimum)
+ low_confidence          (0/1 — valid_captures < 3)
+ median_height_cm        (preferred representative value)
+ median_width_cm
+ median_visible_leaves
+ capture_ids             ("|"-joined Cloudinary public IDs)
+ capture_details         ("|"-joined "id@ts:height" audit string, or empty)
+ problems                ("|"-joined per-image failure reasons)
```

`average_*` columns keep their old mean semantics (written unchanged) so any external consumer and
`camera_import.CAMERA_REQUIRED_COLUMNS` keep working. The importer prefers `median_*` when present.
`capture_date` is the upsert key; `day` remains a human label.

### 4.2 Database (idempotent, additive — existing DBs keep working)

New audit table added to `db.py` SCHEMA (all statements already `CREATE TABLE IF NOT EXISTS`, so
existing DBs gain it on the next `init_db` — **no reset**):

```sql
CREATE TABLE IF NOT EXISTS camera_captures (
    capture_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id          INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE,
    observation_date  TEXT    NOT NULL,          -- capture_date
    public_id         TEXT    NOT NULL,          -- Plant01_20261016_095000
    capture_timestamp TEXT,                      -- from the name; created_at only as fallback
    height_cm         REAL,
    stem_width_cm     REAL,
    visible_true_leaf_count INTEGER,
    cm_per_pixel      REAL,
    confidence        REAL,
    validity          TEXT    NOT NULL,          -- 'valid' | 'failed' | 'pending'
    problem           TEXT,                      -- NULL when valid
    rod_length_cm     REAL    NOT NULL,          -- effective calibration used
    data_source       TEXT NOT NULL CHECK (data_source IN ('real','synthetic')),
    recorded_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (public_id)
);
CREATE INDEX IF NOT EXISTS idx_camera_captures_plant_date
    ON camera_captures (plant_id, observation_date);
```

Optional (recommended, for the editable sheet) append-only edit trail:

```sql
CREATE TABLE IF NOT EXISTS observation_revisions (
    revision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL,
    action         TEXT NOT NULL,     -- 'update' | 'delete' | 'create'
    before_json    TEXT,
    after_json     TEXT,
    reason         TEXT,
    recorded_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`observations` itself is unchanged; only the write path changes (upsert instead of skip).

---

### 4.3 Manual dataset upload contract (single source of truth = `plantreadiness/csv_import.py`)

Nothing new is invented here — the upload must satisfy the contract the importer already enforces.

**Required columns** (`csv_import.REQUIRED_COLUMNS` L35, `_missing_columns` L91):

| Column | Aliases | Accepted values | Enforced by |
|---|---|---|---|
| `Date` | — | `DD-MM-YYYY`, `DD/MM/YYYY`, or ISO `YYYY-MM-DD` | `_parse_date` L102 |
| `Age_days` | `Age` | integer days; `"7 days"` tolerated | `_to_int` L123, `age_days >= 0` (db L71) |
| `Plant_ID` | — | non-empty string → `plants.external_ref` | L186 |
| `Height_cm` | — | numeric, `>= 0`; missing = row issue (importer skips it, **upload rejects it**) | L210, db L72 |

**Optional column:** `Growth_rate_cm_per_day` — derived from the previous measurement when absent
(L240-246). Never required, never invented.

### 4.3.1 Minimum viable dataset — and what is genuinely optional

Width, canopy and leaf counts are **never required** to upload or to predict:

| Column | Required? | Consumed by |
|---|---|---|
| `Date`, `Plant_ID` | yes | identity + ordering |
| `Height_cm` | yes (row-level) | reference baseline (height basis) |
| `Age_days` / `Age` | yes (row-level) | reference baseline (preferred basis) |
| `Growth_rate_cm_per_day` | **no** — derived when absent (L240-246) | validated-model path only |
| stem width (`average_width_cm` → `stem_width_cm`) | **no** | validated-model path only |
| leaf count (`average_visible_leaves`) | **no** | validated-model path only |
| canopy width | **no** — the camera never measures it (camera_import L284 sets it `NULL`) | nothing today |

Proof that "optional" really is optional: `super_orange_growth_data.csv` ships with only
`Date, Age, Plant_ID, Height_cm` — no growth rate, no width, no leaves — and imports with zero issues
and zero fabricated values.

**Which path reads what (verified):**

| Path | Features read | When one is missing |
|---|---|---|
| Reference baseline — `reference.provisional_estimate` L418-448 | `age_days`, else `height_cm`. **Verified: 0 occurrences of `stem_width` / `canopy` / `leaf` in `reference.py`** | refuses → *insufficient evidence*. **Never imputes.** |
| Validated model — `model.predict_days_remaining` L440-471 | all of `FEATURE_COLUMNS` (L45-53): age, height, growth rate, stem width, leaf count, species, variety | the sklearn pipeline's `SimpleImputer(strategy="median")` (L118) fills gaps from the training median. Only runs when a validated artefact exists — currently none. |

**Conclusion:** an upload with only `Date`, `Age`, `Plant_ID`, `Height_cm` is complete and predicts.
Extra width/leaf columns are a bonus that only affects the model path once a validated model exists —
they are not a gate, and their absence is **not** a rejection reason.

### 4.4 Calibration provenance (addresses D9-D11)

**One authoritative value.** `config.CAMERA_ROD_LENGTH_CM` becomes the single source of truth,
validated at load (finite, `> 0`), with no hidden `"30"` fallback: an absent env var falls back to a
named, documented `DEFAULT_ROD_LENGTH_CM` and the effective value is logged at startup. `.env` holds
the physical value; `.env.example` (L19), `README.md` (L69) and the CLI docstrings
(`plant_measurement.py` L27/35/44) are corrected to say "configured by `CAMERA_ROD_LENGTH_CM`" rather
than hardcoding 30. `--rod-length-cm` defaults to the configured value so the CLI can no longer
disagree with the backend. Validation happens **before** the first image is processed (keep the
existing `rod_length_cm <= 0` guard at L1206, raise earlier at config load).

**Per-measurement provenance.**

| Store | New field | Meaning |
|---|---|---|
| `camera_captures` | `rod_length_cm REAL NOT NULL`, `cm_per_pixel REAL NOT NULL` | the effective calibration used for that image |
| `observations` | `rod_length_cm REAL` (nullable), `calibration_source TEXT` | `camera_configured` / `csv_import_no_calibration` / `unknown` |
| daily CSV | `rod_length_cm` (added; `average_cm_per_pixel` already exists) | traceability of the aggregate |

Added with the **existing migration convention** — `PRAGMA table_info(...)` + `ALTER TABLE ... ADD
COLUMN` (`db.py` L265-289). Non-camera imports record `calibration_source='csv_import_no_calibration'`
and `rod_length_cm = NULL`: an honest unknown, never a guessed 30 or 15.

**Existing measurements — report, never rewrite.**

- New `python -m plantreadiness.calibration_report [--db …]` lists every observation with its
  provenance, groups by effective rod length, and flags mixed calibrations. Expected today:
  - `plant_daily_measurements.csv` → 13.3758 cm @ rod 30 (`cm_per_pixel` 0.027298), legacy, not in the DB
  - `observations` #89 → 6.5854 cm @ rod 15
**Calibration verification state (CONFIRMED — the configured value must not be assumed correct).**
Until the physical rod is measured or its specification confirmed, the configuration is recorded as
**unverified**:

- New `calibration_settings` table: `rod_length_cm, verified INTEGER, verified_at TEXT, verified_by,
  note` — an auditable record of *when* the value became trusted. The value itself still comes from
  `config.CAMERA_ROD_LENGTH_CM` (one source of truth); this table records only its verification state.
- `python -m plantreadiness.verify_calibration --rod-length-cm 10 --verified [--note "measured with a
  steel rule"]` records the event; `--unverified` revokes it.
- Every capture stores the effective `rod_length_cm` **and** the verification state at capture time.

**Calibration groups — never mixed in one baseline (CONFIRMED policy).**

| Group | Origin | May feed the growth baseline? |
|---|---|---|
| `manual_measurement` | human-recorded CSV / reference cohort (no rod involved) | yes — the pixel→cm problem does not apply |
| `camera_verified` | camera, rod length verified | yes, as its own group |
| `camera_unverified` | camera, rod length not yet verified | **no — quarantined** |
| `unknown` | legacy rows with no recorded provenance | **no — quarantined** |

The five-point policy:

1. **Reprocess when possible** — if raw images exist, recompute the affected measurements under a
   verified calibration (command below).
2. **Never mix incompatible known calibrations** in one baseline. Exclude records that cannot be
   corrected reliably.
3. **Quarantine unknown-provenance records**, keeping legacy rows separate from verified rows.
4. **Predict only while sufficient reliable data remains.** If the surviving cohort is too small, or
   its calibration cannot be established, return **insufficient evidence** rather than silently using
   questionable measurements.
5. **Report exclusions.** Every result states which calibration group was used and how many records
   were excluded: `calibration_group_used`, `n_excluded_*`, `calibration_conflict`.

**Consequence while the rod is unverified:** camera measurements are still collected and stored, but
quarantined from the growth baseline, and any prediction consuming an unverified-calibration input
carries `input_calibration_unverified: true` plus a visible note. The manual reference cohort
(`super_orange_growth_data.csv`) is unaffected — it has no rod calibration to verify.

**Historical heights change only explicitly.** A config change affects **new** captures only.
Historical heights change solely through
`python -m plantreadiness.reprocess_captures --plant Plant-01 --from 2026-10-16 --rod-length-cm 15`
(dry-run by default): re-measure the preserved Cloudinary images, write new capture rows, recompute the
daily median, log a revision. Re-download is possible because `camera_captures` stores the Cloudinary
public ID.

Header names and values are whitespace-normalised (L48-61), so `" Age"` and `" 7 days "` are valid.

**Hard rejection (400, nothing written):** no header row; any required column missing; not decodable
as text/CSV; empty file; over `MAX_UPLOAD_BYTES` / `MAX_UPLOAD_ROWS` (new config constants).

**Row-level issues → also a hard rejection for uploads (CONFIRMED all-or-nothing):** unparseable
date, bad numeric value, missing `Plant_ID`, missing `Height_cm`, `observation_date` before the
plant's `date_started` (db L426), and duplicate `(plant_id, observation_date)` **within the file**.
The dry-run preview lists every issue with its line number, and the file is refused until it is
clean. No partial imports — not one row is written until the whole file validates.

The commit runs inside a **single transaction** (`BEGIN` / `COMMIT`, `ROLLBACK` on any error) so a
mid-file failure can never leave a half-imported dataset.

**IMPORTANT — this strictness applies only to the upload path.** `import_csv()` keeps its current
lenient behaviour (skip bad rows, import the rest) for `seed.py` and `model_train.py`, which must not
change. Implement strictness as an opt-in `strict=True` parameter, not as a new default.

**CONFIRMED:** `cohort` defaults to `reference` (builds a prediction baseline, hidden from the
feedback tab) with a visible switch to `operational`.

**Attributes the uploader must choose (not columns):** `species` (required — an existing species or a
new one to create), `variety` (optional), `date_started` (optional). `cohort` is covered above.

---

## 5. Ordered TODO

Workstreams appear in execution order (A → B → C → D → F → G → H → I → E); the letters are labels, not
the order. E is last because its tests and docs cover all of the others.

### Workstream A — four-capture daily aggregation (core task)

- [ ] **A1** `plant_measurement.py`: add `parse_capture_name(public_id) -> {plant_ref, capture_date, capture_time, source}`.
      Rule: regex `^(?P<ref>[A-Za-z]+\d+)_(?P<d>\d{8})_(?P<t>\d{6})$` → date `YYYY-MM-DD`.
      Fallback to the Cloudinary `created_at` date; if neither exists → `capture_date = None` and the
      capture is reported as unplaceable (never guessed).
- [ ] **A2** `plant_measurement.py`: add `validate_capture(result)` → `(valid, problem)` using only existing
      signals (rod found, plant detected, positive finite height/width, confidence > 0). Threshold lives
      in `config.py` as a named constant; no new invention of values.
- [ ] **A3** `plant_measurement.py`: add `median_daily_record(plant_ref, capture_date, results)` computing
      medians over **valid** results only + the full capture accounting:
      `expected_captures`, `processed_captures`, `valid_captures`, `failed_captures`,
      `incomplete` (fewer valid captures than expected/reliable — never treat 1 image as 4),
      `low_confidence = valid_captures < config.MIN_CAPTURES_FOR_CONFIDENCE` (new constant, 3),
      `capture_ids`, `capture_details`, `problems`, and `rod_length_cm` (the effective calibration).
- [ ] **A4** `plant_measurement.py`: rewrite `write_daily_record()` to **upsert** on `(plant_id, capture_date)`
      (read file → replace matching row → write, preserving every other row and the header) instead of
      blind append. Keep the function name and signature shape; keep `format_daily_record` as the
      legacy mean formatter for any caller that needs it.
- [ ] **A5** `plant_measurement.py` CLI main loop: resolve `capture_date` from parsed names, group
      `daily_results` by capture date, write one row per group. Pass through the parsed timestamp.
- [ ] **A6** `plantreadiness/camera_refresh.py`: `_rows_from_results` → group by parsed capture date and
      emit median rows; merge with captures already stored for that date from `camera_captures` so a
      later refresh the same day **recomputes** from all captures of the day.
- [ ] **A7** `plantreadiness/repository.py`: add `add_capture()`, `list_captures(plant_id, date)`,
      `daily_records(...)`, `update_observation()`, `delete_observation()` (all through the existing
      `BaseRepository` + SQLite implementation pair, L68-90 and L307-330).
- [ ] **A8** `plantreadiness/camera_import.py`: accept the new columns; choose `median_*` when present;
      persist **each capture** to `camera_captures` keyed by `public_id` (idempotent — processing the
      same image twice never counts twice) with its calibration (`rod_length_cm`, `cm_per_pixel`) and
      `validity`;
      **CONFIRMED: recompute and update the daily observation when the incoming row has strictly more
      valid captures than the stored one** — a new same-day capture is never discarded merely because
      a daily row exists; a repeat of the same aggregate is reported as
      `observations_skipped_duplicate` as today. On camera rows, set
      `observations.rod_length_cm` + `calibration_source='camera_configured'`. Extend the report with
      `captures_recorded`, `daily_records_updated`, `low_confidence_records`, `incomplete_days`,
      `failed_captures`.
- [ ] **A9** Wire `growth_rate_cm_per_day` to be recomputed on upsert from the previous **daily**
      observation and the real elapsed days (reuse the L259-274 logic; do not assume 24 h).
- [ ] **A10** Constants in `plantreadiness/config.py`: `MIN_CAPTURES_FOR_CONFIDENCE = 3`,
      `CAPTURE_NAME_RE`, `CAPTURE_MIN_CONFIDENCE`. Document each.

### Workstream B — species rename + new species

- [ ] **B1** CONFIRMED species names. Add `plantreadiness/species.py` (or extend `config.py`) with the
      canonical catalog:
      `SPECIES_SUPER_ORANGE = "Sunflower (SuperOrange)"` (renamed from `"Super Orange"`), and
      `SPECIES_MARIGOLD = "Marigold (Apsara Yellow)"` (new, **no data yet**).
      Keep the historical string `"Super Orange"` only as the rename-migration source constant.
- [ ] **B2** Add a small, explicit, non-destructive migration CLI
      `python -m plantreadiness.rename_species --from "Super Orange" --to "Sunflower (SuperOrange)" [--yes]`
      that updates the species string in `plants`, `prediction_history`, `readiness_assessments`
      (dry-run first, prints counts). Never touches observations/heights. No reset.
- [ ] **B3** Update the species defaults: `csv_import.import_csv(species=...)`, `camera_import`
      `default_species`, `plant_feedback.py` L500, `prediction.py` docs, `seed.py`.
- [ ] **B4** Rebuild the reference baseline under the new name. DECIDED: do **not** change `_safe()`;
      it strips spaces, so `"Sunflower (SuperOrange)"` →
      `models/reference/baseline_Sunflower(SuperOrange)_ANY.json` and `"Marigold (Apsara Yellow)"` →
      `baseline_Marigold(ApsaraYellow)_ANY.json` (parentheses kept — **quote these paths in every
      shell command**). `Marigold (Apsara Yellow)` gets **no** baseline file: there is nothing to
      build from, and inventing one would break rule #3.
      Keep the old `baseline_SuperOrange_ANY.json` in place until the new one is verified identical
      except `species`/`source_label`/`generated_at`; then remove it in a **separate, approved** commit.
- [ ] **B5** Make the second species visible: `/api/species` (`backend/app.py` L143-174) currently
      derives the catalog from observations, so a species with no data cannot appear. Extend it to
      merge a configured species list with observed ones. CONFIRMED behaviour: it appears with
      `observation_count = 0`, `latest_observation_date = null`, and a prediction of
      **insufficient evidence / no reference baseline** — never a fabricated date.
- [ ] **B6** Flag: `models/synthetic/registry.json` scopes artifacts to `"Super Orange"`. After the
      rename those demo artifacts no longer match the species. Decide: leave them (they simply stop
      applying) or regenerate in demo mode. Do **not** silently retarget them.
- [ ] **B7** Update docs (`docs/ARCHITECTURE.md` L39-40/L111, `README.md`, `AGENTS.md`,
      `docs/REPORT.md`) for the new species names.

### Workstream C — editable reference/measurements sheet in the web app

- [ ] **C1** Backend endpoints in `backend/app.py`:
      `GET /api/observations?species=&plant_id=&from=&to=&cohort=` (rows joined with
      `plants.species / external_ref`), `PATCH /api/observations/<id>` (height, stem width, leaves,
      age — validated, `NULL` allowed), `POST /api/observations` (manual row, explicit species),
      `DELETE /api/observations/<id>` (explicit confirm flag). Every mutation writes
      `observation_revisions`.
      **CONFIRMED: the grid edits daily `observations` rows only** (raw `camera_captures` rows are
      read-only), and **delete is allowed with an explicit confirm + audit log**.
- [ ] **C2** `nextjs/pages/api/observations.ts` proxy (mirror the existing proxy pattern in
      `nextjs/pages/api/*.ts`).
- [ ] **C3** `nextjs/pages/feedback.tsx` — in the **Reference** tab (render currently L735-820), add a
      "Measurements" grid below the existing baseline table: sticky header, inline-editable cells,
      add row, delete row (with confirm), save/discard, and a "Download CSV" button. Keep the existing
      `by_age_days` baseline table intact; reference-cohort plants stay hidden from the feedback tab
      (existing filter L155).
- [ ] **C4** Styling: reuse `nextjs/styles/globals.css` tokens only (neutral surfaces, restrained green).
      No new design system.

### Workstream D — present-day characteristics on "Predict readiness"

- [ ] **D1** `plantreadiness/services.py::predict_species_cohort` (L493-605): include in the result
      `daily_record` (the aggregated values for the newest date) and `captures` (per-image audit rows
      from `camera_captures`), plus `valid_captures / failed_captures / low_confidence`.
- [ ] **D2** `backend/app.py` `/api/predict/species` (L177-217): pass those fields through (they are
      already snapshotted; extend `_snapshot_result`).
- [ ] **D3** `nextjs/pages/index.tsx` (result render from L297): add a "Present-day characteristics"
      card — observation date, median height, stem width, visible leaves, cm/pixel, confidence,
      `captures used (valid/failed)`, a **low-confidence** badge when valid < 3, and a collapsible
      per-image list (public ID + timestamp + individual height) so the aggregate is auditable.
- [ ] **D4** Ensure the card degrades honestly: `NULL` fields render as `—`, never 0.

### Workstream F — manual dataset upload (with attribute validation)

- [ ] **F1** New `plantreadiness/dataset_schema.py` (small, read-only):
      `describe_schema()` → required columns + aliases + accepted date formats + per-column constraints,
      **derived from `csv_import.REQUIRED_COLUMNS` / `COLUMN_ALIASES`** so it can never drift from the
      importer; and `validate_dataset(text) -> {ok, missing_columns, extra_columns, row_count, plants,
      issues[], preview[]}` that performs **zero writes**.
- [ ] **F2** `plantreadiness/csv_import.py`: let `import_csv()` accept an in-memory
      `io.StringIO`/text buffer as well as a path, and add an opt-in `strict=True` mode (abort the
      whole file on any row-level issue, one transaction). Keep the existing path API **and the
      lenient default** used by `seed.py` and `model_train.py` unchanged.
- [ ] **F3** `backend/app.py` new endpoints:
      `GET /api/datasets/schema` (the required-attributes manifest + a template CSV),
      `POST /api/datasets/validate` (dry run, returns the report above),
      `POST /api/datasets/import` (commit; requires `species`; optional `variety`, `date_started`,
      `cohort` — defaulting to `reference`).
      **CONFIRMED all-or-nothing:** `import` re-validates every row first and refuses the entire file
      on any issue, inside one transaction. Mismatch or bad rows → **HTTP 400** with
      `missing_columns`, `extra_columns`, the full required list, and the per-line issue list;
      nothing written.
- [ ] **F4** Guardrails in `config.py`: `MAX_UPLOAD_BYTES`, `MAX_UPLOAD_ROWS`, `ALLOWED_DATASET_ENCODINGS`.
      An upload may only ever create `data_source='real'`; `synthetic` is refused unless an explicit
      flag is passed (rule #5).
- [ ] **F5** `nextjs/pages/api/datasets/{schema,validate,import}.ts` proxies, mirroring the existing
      proxy pattern.
- [ ] **F6** UI: an "Add dataset" panel in the Feedback → Reference tab, wired from the existing empty
      state ("no reference yet", currently only a Build button at feedback.tsx L754-758). Before the
      file picker, show the checklist in **three tiers** so the reminder is accurate and cannot
      over-scope the user:
      **(1) Required:** `Date`, `Age_days` (or `Age`), `Plant_ID`, `Height_cm`;
      **(2) Optional, derived if absent:** `Growth_rate_cm_per_day`;
      **(3) Optional, used if present:** stem width and leaf count (only affects the validated-model
      path — state that plainly).
      Plus a template CSV download and the species/variety/date-started/cohort fields. After choosing
      a file: dry-run preview with row count, plants, and the issues table; then Confirm. On rejection,
      show *found vs required* side by side.
- [ ] **F7** CONFIRMED: **do not chain** import into a baseline build. Import and reference-building
      stay two separate, explicitly-triggered steps. After a successful import, surface a plain hint
      pointing at the existing "Build reference" button (feedback.tsx L754-758) so the user can take
      the second step themselves. When they do, the result is labelled *provisional historical
      baseline*, never a validated model.
- [ ] **F8** Tests: validate never writes; missing required column → 400; malformed/empty/oversized
      file → 400; **all-or-nothing: a file with exactly one bad row imports nothing at all** (row
      counts in `plants` and `observations` unchanged); the same file with the bad row fixed imports
      completely; **a CSV with only the four required columns (`Date, Age, Plant_ID, Height_cm`) is
      accepted and yields a provisional prediction with no width/leaf/rate data present**; a re-upload
      is idempotent; the uploaded `data_source` is always `real`; the schema manifest matches
      `csv_import.REQUIRED_COLUMNS`; and the lenient default is unchanged
      (`seed.py`/`model_train.py` still skip bad rows).

### Workstream G — one authoritative rod calibration + provenance (D9-D11)

- [ ] **G1** `config.py`: replace the hidden `"30"` default with a named `DEFAULT_ROD_LENGTH_CM` and a
      validated loader (finite, `> 0`, raises on invalid). Log the effective value once at startup.
- [ ] **G2** `plant_measurement.py`: `--rod-length-cm` defaults to the configured value (no silent
      disagreement); correct docstrings L27/35/44, `README.md` L69, `.env.example` L19. Validate before
      the first image.
- [ ] **G3** `db.py`: `rod_length_cm` + `cm_per_pixel` on `camera_captures`; nullable `rod_length_cm` +
      `calibration_source` on `observations`, added via the existing `PRAGMA table_info` +
      `ALTER TABLE ADD COLUMN` pattern (L265-289). Existing rows are backfilled to
      `calibration_source='unknown'` — honest, never a guess.
- [ ] **G4** Daily CSV: add the `rod_length_cm` column (traceability of the aggregate).
- [ ] **G5** New `python -m plantreadiness.calibration_report [--db …]` + a read-only
      `GET /api/calibration` endpoint: per-observation provenance, grouped by effective rod length,
      mixed-calibration flags.
- [ ] **G6** Guard the baseline: refuse or explicitly exclude observations with an incompatible
      `rod_length_cm`; surface `calibration_conflict` in the result and in the UI.
- [ ] **G7** New `python -m plantreadiness.reprocess_captures` (dry-run default, explicit, revision-
      logged): re-measure preserved Cloudinary images under a chosen rod length, recompute the daily
      median. The only sanctioned way historical heights change.
- [ ] **G6b** `calibration_settings` table + `python -m plantreadiness.verify_calibration
      --rod-length-cm N --verified|--unverified [--note …]`; snapshot the verification state onto every
      capture at capture time.
- [ ] **G6c** Implement the four calibration groups and the five-point policy in the baseline builder
      and in the prediction payload (`calibration_group_used`, `n_excluded_*`,
      `input_calibration_unverified`, `calibration_conflict`), including the
      "insufficient evidence when too little reliable data remains" rule (reuse
      `MIN_PROVISIONAL_PLANTS`).
- [ ] **G8** Tests: config validation (0/negative/absent/valid); the configured value reaches
      `calculate_measurements`; per-capture provenance is stored; **unverified camera rows are
      quarantined from the baseline and the exclusion is reported**; a `manual_measurement` row and a
      `camera_verified` row are never merged into the same group; a mixed-calibration cohort is flagged
      and either excluded or refused per policy 4; `verify_calibration` flips the state and is
      auditable; reprocessing is dry-run by default and changes heights only when explicitly applied;
      historical rows are untouched by a config change.

### Workstream H — prediction input roles (D12; **REVERSES an earlier decision**)

> This contradicts the decision recorded earlier ("leave it — age-first is intended"). The second spec
> overrides it: *"A reliable planting date must determine age. It must not disable height or growth
> rate as potential prediction inputs."* §9 has been updated accordingly.

- [ ] **H1** First, **prove the root cause with a failing test**: with `age_days` set, changing
      `height_cm` returns an identical result (`reference.py` L439-448).
- [ ] **H2** Redesign `provisional_estimate` so each input's role is explicit and reported:
      - `age_days` → selects the comparable ageing window (a *constraint/tolerance*), not an exclusive switch;
      - `height_cm` → selects/tunes within that window (nearest median height; residual reported);
      - `growth_rate_cm_per_day` → optional, **bounded** adjustment (see H3);
      - `species` / `variety` → selects which baseline applies (unchanged).
      Emit `basis_inputs` naming exactly which inputs were used and how.
- [ ] **H3** Growth-rate adjustment — **CONFIRMED, bounded and reported**:
      ```
      D_adjusted = D_base × clamp(G_cohort / G_plant, 0.5, 2.0)
      ```
      Requirements, all mandatory:
      - `D_base` is the estimate from the existing method; the two growth rates must be computed
        **consistently** (same definition, same window).
      - **Report both rates and the adjustment factor** (`growth_rate_cohort_cm_per_day`,
        `growth_rate_plant_cm_per_day`, `growth_rate_adjustment`).
      - **Skip the adjustment** when either rate is missing, invalid, zero, or otherwise unsuitable.
      - Label the result a **provisional heuristic**, never a validated prediction.
      - Treat the inverse relationship (days ∝ 1/rate) as a **hypothesis to test**, not an established
        fact; the clamp bounds extreme values but does **not** prove accuracy.
      - Feed `adjusted` vs `unadjusted` variants into Workstream I so both are later compared against
        real outcomes.
- [ ] **H4** Missing planting date → age stays **unknown**; never derived from height. Height and growth
      rate are still used, with an explicit note.
- [ ] **H5** Insufficient history → no invented rate; return a provisional estimate or an unavailable
      result according to what the evidence supports (existing `insufficient_growth_history` /
      `history_note`).
- [ ] **H6** Keep this a **lookup + documented adjustment**. Do not introduce a new model or a new
      dependency.
- [ ] **H7** Tests: height changes the prediction **when the planting date is known** and **when it is
      missing**; a missing date never fabricates an age; the adjustment applies only when both rates
      exist and are usable (missing / zero / negative / non-finite → no adjustment); the clamp is
      respected at both ends; both rates and the factor are present in the result; the result is
      labelled a provisional heuristic.

### Workstream I — provisional baseline vs validated model (D13)

- [ ] **I1** Guarantee the upload path can never set `validated: true` and never triggers training.
- [ ] **I2** Confirm and document the three distinct concepts (human readiness assessment / actual
      transplant date / ~14-day outcome) as separate records, and that missing feedback stays
      *pending*.
- [ ] **I3** Verify the evaluation gate against the spec: plant-level grouping to prevent leakage,
      evaluation on plants not used to build/tune, comparison with the existing method/baseline,
      day-error metrics, outcomes examined separately, and the **existing** thresholds enforced —
      `MIN_INDEPENDENT_PLANTS = 4`, `MIN_READINESS_EVENTS = 6`, `auto_applied: False`
      (`services.py` L1002-1026, L661-665). Fix only if a real gap is found.
- [ ] **I4** Ensure `method` is always one of `validated_model` / `synthetic_demo` /
      `historical_reference_baseline` / `insufficient_evidence` and that the UI shows it (it does —
      `index.tsx` L80-90); uploaded data must land on `historical_reference_baseline`.
- [ ] **I4b** Extend the evaluation to compare **adjusted vs unadjusted** growth-rate variants against
      actual outcomes, so the H3 hypothesis is testable and never assumed.
- [ ] **I5** Tests: a completed training run alone does **not** mark a model validated; promotion needs
      labelled events + the gate; missing feedback is pending, not failure.

### Workstream E — tests and docs

- [ ] **E1** New `tests/test_daily_aggregation.py` covering the seven required cases:
      1. four same-plant/same-date captures → **one** daily record;
      2. different plants / different dates stay separate;
      3. median correctness (spec example 12.8, 13.0, 13.1, 16.5 → **13.05**);
      4. failed/invalid/missing captures reported transparently (`valid`, `failed`, `problems`);
      5. duplicate processing does not duplicate daily records (re-run → 1 row, idempotent);
      6. growth rate uses daily records + actual elapsed days (e.g. gaps of 1 and 3 days);
      7. rod calibration unchanged: same pixels at rod 15 vs 30 → exactly 2× height; and
         `calculate_measurements` output is byte-identical to the current implementation.
- [ ] **E2** Extend `tests/test_camera_refresh.py` for the median/grouping/upsert behaviour
      (it currently feeds a fake puller with `average_*` fields — keep those working).
- [ ] **E3** Add backend tests for the new `/api/observations` endpoints (extend
      `tests/test_backend_api.py`) and a species-rename migration test (dry-run + apply on a
      throwaway DB).
- [ ] **E4** Update `tests/test_model.py`, `tests/test_seed.py`, `tests/test_integration.py`,
      `tests/test_reference.py` where they assert the *real* species string or the real baseline
      filename. Fixture-only uses of `"Super Orange"` stay as they are (they are arbitrary test data).
- [ ] **E5** Run `python3 -m pytest -q`, report the real numbers (was 196 passing).
- [ ] **E6** Update `docs/ARCHITECTURE.md` data-flow diagram (four captures → one daily record),
      `README.md` (new commands, new CSV columns, species names), `AGENTS.md` where the flow is
      described.

---

## 6. Limitations to state honestly (not bugs)

1. The reference `days_until_transplant` comes from an **assumed** transplant date (day after each
   plant's last monitoring day). The prediction is historical-timing lookup, not a growth model.
2. **Age no longer beats height — RESOLVED by Workstream H (this reverses the earlier decision).**
   A planting date supplies age; height and growth rate remain inputs (D12). Without a planting date,
   age stays unknown and is never derived from height.
3. `valid_captures < 3` (configurable) is flagged low-confidence; a single valid capture is never
   presented as a four-image aggregate.
4. Median is over **valid** captures only; failed captures are counted and reported, never imputed.
5. `models/synthetic/registry.json` artifacts are scoped to the old species name and stop applying
   after the rename — synthetic demo only.
6. Resolved: *Apsara Yellow* is a marigold (*Tagetes*), so it is catalogued as its own
   `Marigold (Apsara Yellow)` species rather than being filed under Sunflower.
7. The validated-model path **median-imputes** missing numeric features (growth rate, stem width,
   leaf count) inside the sklearn pipeline (`model.py` L118). The reference baseline never imputes.
   So a model-based result must disclose that the model filled gaps, while a baseline result must say
   *insufficient evidence* instead — the two paths must never be blurred in the UI.
8. A dataset supplies **measurements only**; it can never produce a validated model. Uploading data
   makes prediction possible via the **provisional historical baseline**; a validated model still
   requires labelled readiness events and the evaluation gate.
9. **Calibration provenance for pre-existing rows is partly unknowable** — the rod length used was
   never recorded. Those rows are quarantined (`unknown` provenance) rather than reinterpreted, and are
   never silently re-scaled. Only an explicit `reprocess_captures` run changes a historical height.
   **The configured value is also unverified until you measure the rod**, so camera measurements are
   collected but excluded from the growth baseline until `verify_calibration` records the real length.
10. The growth-rate adjustment (H3) is a **documented bounded heuristic**. The inverse relationship
    (days ∝ 1/rate) is a **hypothesis to be tested**, not established fact; the 0.5×–2× clamp bounds
    extreme values but does not prove the formula is accurate. Both rates and the factor are always
    reported, and adjusted-vs-unadjusted are compared against real outcomes later.
11. Expected-capture counts are only known when the pipeline is told how many images to expect; when
    that is unknown the day is marked `incomplete` on the basis of the configured reliable minimum
    (3), never guessed from nothing.

---

## 7. Verification / acceptance

- [ ] `python3 -m pytest -q` green, counts reported.
- [ ] Pipe four same-day captures for `Plant-01` through `camera_import` on a scratch DB → exactly one
      row in `observations` for that date, four rows in `camera_captures`, `valid_captures = 4`,
      `low_confidence = 0`, `height_cm` = median of the four.
- [ ] Re-run the import → still one row, counts unchanged (idempotent).
- [ ] Feed 1 valid + 3 failed → one row, `valid_captures = 1`, `low_confidence = 1`, `problems` populated.
- [ ] Two dates 3 days apart → `growth_rate = (h2 - h1) / 3`.
- [ ] `Plant01` and `Plant02` same day → two separate records.
- [ ] UI: Predict readiness shows the present-day card; the Reference tab grid loads, edits a value,
      saves, and the change is reflected after reload; CSV download works.
- [ ] Species dropdown lists `Sunflower (SuperOrange)` with real data and
      `Marigold (Apsara Yellow)` with an honest "no observations yet".
- [ ] Uploading the shipped `super_orange_growth_data.csv` through the new panel reports ✓ with the
      expected row/plant counts and zero issues.
- [ ] A minimal file with **only** `Date, Age, Plant_ID, Height_cm` uploads successfully and Predict
      readiness returns a provisional estimate — no width, leaf, or growth-rate data required.
- [ ] Uploading a CSV without `Height_cm` is **rejected**: HTTP 400, `missing_columns: ["Height_cm"]`,
      and the DB is untouched.
- [ ] A CSV with **one** bad row imports **nothing** (all-or-nothing); fixing that row lets the whole
      file import.
- [ ] The required-attributes checklist is visible **before** a file is chosen.
- [ ] `POST /api/datasets/validate` changes no row counts (proved by querying before/after).
- [ ] After a **separately triggered** reference build, Predict readiness returns a **provisional
      historical baseline** result for that species, labelled as such.
- [ ] The upload appears in Feedback → Reference; `seed.py` and `model_train.py` behaviour is
      unchanged (lenient import still skips bad rows).
- [ ] Four captures, same plant, same day → **one** `observations` row + four `camera_captures` rows; a
      **fifth** same-day capture **recomputes** the median (still one row, no new row).
- [ ] Processing the same Cloudinary `public_id` twice leaves the capture count at 1.
- [ ] Changing `CAMERA_ROD_LENGTH_CM` changes **new** heights proportionally (15 → 10 cm scales by
      10/15) and leaves every historical height byte-identical.
- [ ] `calibration_report` flags the 13.3758 cm @ rod 30 row and the 6.5854 cm @ rod 15 row as
      different calibrations.
- [ ] With a planting date set, changing the measured height **changes** the prediction and
      `basis_inputs` lists both `age_days` and `height_cm`.
- [ ] A completed training run alone does **not** mark a model `validated`.
- [ ] `cd nextjs && npm run build` succeeds (production build actually run, output reported).
- [ ] With the rod unverified, camera observations are quarantined: the baseline reports
      `calibration_group_used` and `n_excluded_*`, and prediction still runs on the manual reference
      cohort.
- [ ] `verify_calibration --verified` lets camera rows join the baseline as `camera_verified`;
      `--unverified` revokes it again.
- [ ] With both rates usable, `days_remaining` equals `D_base × clamp(G_cohort / G_plant, 0.5, 2.0)`
      and the result carries both rates + the factor; with either rate missing/zero, the adjustment is
      skipped (`D_adjusted == D_base`) and said so explicitly.
- [ ] `git status` shows only intended files; `plant_readiness.db`, `plant_daily_measurements.csv`
      (schema-extended), `models/reference/*`, code, tests, docs.

---

## 8. Ordered execution checklist

0. **Housekeeping already pending in the working tree** (from the previous decision): commit the two
   staged deletions (`image.png`, `IMAGE OF PLANT.png`) and the `nextjs/next-env.d.ts` dev-path
   version. That file drifts between `.next/types/...` (build) and `.next/dev/types/...` (dev) —
   confirmed again on this check; `next dev` in `run.sh` is the reason. Do not stage `.env`, DBs,
   `node_modules/`, `.next/`.
1. B2 rename migration module (+ dry run on the real DB, no `--yes` yet) — read-only until approved.
2. A1-A5 (`plant_measurement.py`): name parsing, validation, median, upsert, CLI grouping.
3. A10 config constants + **G1-G2** (single rod-length source, validated, CLI default, docs corrected).
3b. **G3-G4** calibration provenance columns (`camera_captures`, `observations`, daily CSV).
4. A7 repository methods (update/delete/captures/daily records).
5. A8-A9 `camera_import.py` medians + upsert + capture audit + growth rate.
6. A6 `camera_refresh.py` grouping + merge-with-stored-captures.
6b. **G5-G7, G6b-G6c** calibration report endpoint + CLI, `verify_calibration` +
    `calibration_settings`, the four calibration groups and the five-point mixing policy,
    `reprocess_captures` CLI. **Operational note:** once this lands, camera measurements stay
    quarantined from the growth baseline until you run `verify_calibration` with the measured rod
    length — predictions continue on the manual reference cohort meanwhile.
6c. **H1** (prove the defect with a failing test) → **H2-H6** prediction input roles. This **reverses an
    earlier decision**, so re-run the whole suite afterwards — the reference tests encode the old
    age-first behaviour.
6d. **I1-I5** provisional-vs-validated separation checks.
7. E1-E3 tests for A; run them.
8. B3-B4 species defaults + baseline rebuild; E4 test updates.
9. D1-D4 present-day characteristics (services → backend → UI).
10. C1-C4 observations endpoints + editable grid.
11. B5 species catalog merge.
11b. F1-F5 upload validation: schema manifest, buffer import, endpoints, guardrails, proxies.
11c. F6-F7 upload UI (checklist before the picker, dry-run preview, all-or-nothing confirm, hint to
     the separate Build-reference step).
12. B7 + E6 docs.
12b. F8 + E3 dataset-upload tests.
13. Full suite (`python3 -m pytest -q`) + **`cd nextjs && npm run build`** + the acceptance pass in §7.
    Report the ACTUAL results, then produce the seven-part final report the spec requires (§6 of the
    task): root causes, files/structures changed, capture-vs-daily mechanics, calibration governance,
    age/height/growth-rate roles, provisional-vs-validated, tests run + remaining limitations.
14. Commit in small, topical commits; never stage `.env`, DBs, `node_modules/`, `.next/`,
    `processed_cloudinary_ids.txt`, `plant_result*.jpg`.

---

## 9. Decisions — CONFIRMED (2026-10-17)

1. **Species strings.** `"Sunflower (SuperOrange)"` (renamed from `"Super Orange"`) and
   `"Marigold (Apsara Yellow)"` (new). The marigold is catalogued under its correct genus, not
   `Sunflower(...)`.
2. **Second species with no data.** `Marigold (Apsara Yellow)` appears in the dropdown with
   `observation_count = 0` / `latest_observation_date = null` and an honest "no observations yet /
   insufficient evidence" prediction. No placeholder plant is registered.
3. **Daily-record write semantics.** When more captures for the same plant+day arrive, the daily
   record is **recomputed and updated** (median + `valid_captures`). Individual captures remain in
   `camera_captures` as the audit trail. The append-only rule in AGENTS.md applies to predictions and
   feedback, not to measurements — this is recorded here deliberately.
4. **Sheet edit scope.** The grid edits daily `observations` rows only. `camera_captures` is
   read-only.
5. **Deletion in the sheet.** Allowed, with an explicit confirm and an `observation_revisions` audit
   entry containing the previous values.
6. **Baseline filename.** Do not change `_safe()`. Filenames keep parentheses
   (`baseline_Sunflower(SuperOrange)_ANY.json`) — quote these paths in shell commands. No baseline
   file is created for the marigold.
7. **Dataset upload — cohort.** Defaults to `reference` (builds a baseline; hidden from the feedback
   tab) with a visible switch to `operational`.
8. **Dataset upload — row errors.** **All-or-nothing**: any row-level issue refuses the entire file
   (HTTP 400 with the per-line list, one transaction, nothing written). Strictness is an opt-in
   `strict=True` used by the upload path only — `seed.py` / `model_train.py` keep the lenient
   skip-bad-rows behaviour.
9. **Dataset upload — no chaining.** Import and reference-building remain two separate steps; the UI
   only hints at the existing "Build reference" button.
10. **Dataset upload — location.** Feedback → Reference tab, replacing/extending the current
    empty-state "Build reference" affordance.

Added by the second spec (measurement reliability / calibration / prediction roles / validation gate):

11. **One authoritative rod length.** `config.CAMERA_ROD_LENGTH_CM` wins; no hardcoded 30 or 15.
    Note `.env` currently holds **`10`**, `.env.example`/README/docstrings say 30, and the two existing
    measurements were taken at 30 and 15 — all three are reconciled in favour of the configured value,
    and the divergence is reported rather than rewritten.
12. **Prediction input roles — REVERSES the earlier "leave it, age-first is intended" answer.** Age
    supplies age; **height and growth rate remain available inputs even when a planting date is
    known**. A missing planting date leaves age unknown and never fabricates one from height.
13. **Growth-rate adjustment (H3) — CONFIRMED as option (a), bounded and reported.**
    `D_adjusted = D_base × clamp(G_cohort / G_plant, 0.5, 2.0)`. Both rates and the factor are always
    reported; the adjustment is skipped when either rate is missing, invalid, zero or unsuitable; the
    result is labelled a **provisional heuristic**; and the inverse relationship is treated as a
    **hypothesis to test**, not established fact. Adjusted vs unadjusted are both fed to the
    evaluation for later comparison against real outcomes.
14. **Calibration conflict handling — CONFIRMED as a combined policy:** reprocess when possible →
    never mix incompatible known calibrations → quarantine unknown/unverified provenance → predict only
    while sufficient reliable data remains, otherwise *insufficient evidence* → always report which
    calibration group was used and how many records were excluded.
15. **Rod length stays configurable and is treated as UNVERIFIED until physically confirmed.** Do not
    assume `.env` (= `10`) is correct. Preserve the current configuration, record the verification
    state, and mark the calibration unverified. Camera measurements taken under an unverified
    calibration are **not trusted for the growth baseline** (quarantined), though they are still
    collected. Once the physical length is measured, record it with `verify_calibration` and use it
    consistently for new captures; historical measurements with uncertain calibration are reconciled
    only through an explicit reprocess. **Action for you: measure the rod and tell me the value.**
