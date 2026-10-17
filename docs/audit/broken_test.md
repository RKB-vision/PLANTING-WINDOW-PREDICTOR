# broken_test.md — Adversarial QA & Security Audit

Plant-Readiness / Planting-Window Predictor (`/Users/visionary/Documents/CODING/EMBEDDED`)

Audit date: **2026-10-17** · Branch `main` · HEAD `7391732` ("Implement four-capture
daily aggregation, calibration verification, species rename, prediction input roles")

This report contains only defects that were **reproduced** in an isolated
environment (temporary databases and directories, synthetic images/CSV, mocked
or offline external services). Everything theoretical is labelled
`UNVERIFIED`. No real record was modified: the production database was only ever
opened read-only for inspection, and all experiments used `tempfile` directories.

---

## A. Executive summary

The system is well-architected, the existing test suite is green (202 passed),
and many safety invariants genuinely hold (see §C/§D). The audit nevertheless
found **16 reproducible defects**, three of them high severity, that undermine the
project's stated non-negotiables — most importantly *"be honest in predictions"*
and *"synthetic ≠ real / no invented data"*.

| ID | Severity | One-line summary |
|---|---|---|
| AUD-05 | **High** | Quarantined (unverified-camera) measurements still drive live predictions; exclusions are never disclosed. |
| AUD-02 | **High** | `evaluate_predictions` counts a deferral (`transplant_occurred=0`) as a real transplant, corrupting the evidence gate. |
| AUD-03 | **High** | The manual-upload path (`services.run_csv_import`) stores rows with no calibration source, so uploaded reference data is silently quarantined and can never support a provisional baseline. |
| AUD-01 | Medium | Three new CLIs are **un-importable** (stray `PY` at module level). |
| AUD-04 | Medium | Manual CSV import is **not** all-or-nothing: good rows are committed even when other rows are invalid. |
| AUD-06 | Medium | `+inf` height is accepted and stored as a real measurement (validation only checks `< 0`). |
| AUD-07 | Medium | `add_capture` stores `valid` captures with `+inf` height and lets a `public_id` be reassigned to another plant. |
| AUD-08 | Medium | Daily-aggregation CSV upsert is a lock-free read-modify-write → lost rows under concurrency. |
| AUD-09 | Medium | `init_db`'s "one-time" provenance migration re-runs on every call and silently promotes `unknown` rows into the baseline-usable `manual` group. |
| AUD-10 | Medium | Reprocessing a capture silently **wipes** the daily observation's derived growth rate. |
| AUD-11 | Medium | Flask API has **no authentication** and binds `0.0.0.0:8000` (all interfaces), exposing state-changing routes to the LAN. |
| AUD-12 | Low | Type-confused / out-of-range inputs return HTML 500 or leak Python internals instead of clean JSON 4xx. |
| AUD-13 | Low | CSV import accepts `nan`/`inf` heights and spreadsheet-formula payloads; export does not neutralize them. |
| AUD-14 | Low | Config validation is inconsistent: bad `CAMERA_ROD_LENGTH_CM` bricks startup (fail-closed but no graceful path); `CAMERA_ROD_HUE`/`CAPTURE_MIN_CONFIDENCE` are unvalidated (a NaN confidence disables the gate). |
| AUD-15 | Low | Frontend lacks the editable reference grid and the manual dataset upload control — those workflows are unreachable. |
| AUD-16 | Info | Species-rename handles only the exact legacy string `"Super Orange"`; variants (`"SuperOrange"`, `"Apsara Yellow"`) are never migrated or aliased. |

Overall the **readiness/analytics core is sound** (median aggregation,
growth-rate clamping, training-leakage guards, no-duplicate reprocessing), but
the **provenance/quarantine boundary is not enforced end-to-end**: data the
system has correctly decided to distrust is still used for predictions, and data
the user supplies through the upload path is silently distrusted. Those two are
the headline issues.

---

## B. Environment & reproducibility

| Item | Value |
|---|---|
| OS | macOS (darwin) |
| Python | 3.14.7 (`python3`) |
| Node / npm | v25.4.0 / 11.7.0 |
| OpenCV / NumPy / SciPy | 5.0.0 / 2.5.3 / 1.18.1 |
| Repo | `/Users/visionary/Documents/CODING/EMBEDDED`, branch `main`, HEAD `7391732` |
| Working tree at audit start | clean |

### Baseline results (before adding audit tests)

```
$ python3 -m pytest -q
202 passed in 25.26s            # exit 0

$ cd nextjs && npm run build
Route (pages)
┌ ○ /
├   /_app
├ ○ /404
├ ƒ /api/assessments  /api/evaluation  /api/feedback  /api/plants
├ ƒ /api/predict  /api/predict/species  /api/predictions  /api/reference
├ ƒ /api/species  /api/transplant
└ ○ /feedback
EXIT=0
```

### Results after adding the audit regression file

```
$ python3 -m pytest -q
212 passed, 11 xfailed in 24.46s
```

`tests/test_audit_adversarial.py` (test-only, added by this audit) contains 10
passing guards for behaviour that is correct today and 11 `xfail(strict=True)`
tests that encode the *expected* behaviour for the confirmed defects below. A
strict xfail turns into a failure if the defect is ever fixed, flagging the test
for promotion. **No application code was modified.**

### Isolation method

All experiments ran against `tempfile.mkdtemp()` databases/directories with
synthetic images and CSV. The real DB was opened read-only for inspection only.
External services (Cloudinary) were not contacted; the OpenCV pipeline was driven
directly with synthetic arrays. Reusable scripts from the audit are referenced
inline; each finding's **Reproduction** block is self-contained.

---

## C. Coverage matrix

Subsystems exercised, with method and outcome.

| Subsystem | Method | Outcome |
|---|---|---|
| Capture-name parsing / identity | Fuzz well-formed + malformed names (`parse_capture_name`) | **Solid** — rejects month 13, hour 24, 5-digit time, non-leap 02-29, trailing junk; strips whitespace/CRLF |
| Four-capture daily median | `median_daily_record` over valid/failed sets, incl. `inf`/`nan` | Median/failed-count logic correct; **non-finite can propagate** if a `valid` record carries it (AUD-06) |
| Daily CSV upsert | Same plant+date repeated, undated rows, concurrency | Idempotent & keyed correctly; **lost updates under concurrency** (AUD-08) |
| Calibration lifecycle | Groups, `verification_applies`, verify CLI, reprocess | Grouping correct; CLI runs via `-m`; **modules un-importable** (AUD-01); **reprocess wipes growth rate** (AUD-10) |
| OpenCV pipeline | Blank/black/tiny/noise/green/grayscale/empty/huge, corrupt files | Fails **loudly** (`RuntimeError`) for degenerate input; collector records failed captures. `cv2.error` for 2-D/empty arrays (unreachable via `imread`) |
| Prediction & growth-rate adjustment | Boundary rates, missing/zero/negative/NaN/inf, clamp 0.5–2.0 | Clamping correct; missing/zero/non-finite correctly skipped; **quarantined data still used** (AUD-05) |
| Reference baseline & leakage | Cohort tagging, training-row window, evaluation gate, retrain gate | No plant-level leakage (GroupKFold; rows ≤ readiness date); retrain gate requires validated + better MAE; **deferrals graded** (AUD-02) |
| Migrations | `init_db` ×N, provenance re-tagging | Append-only, counts preserved; **reclassifies `unknown` → CSV** (AUD-09) |
| Species rename | Dry-run / apply / repeat / variants | Idempotent, non-destructive; **variant aliases unhandled** (AUD-16) |
| Flask API | Hostile inputs, types, content-types | Parameterized SQL (no injection); **no auth + 0.0.0.0** (AUD-11); **messy errors** (AUD-12); **cohort/`data_source` mass-assignment** (AUD-11 notes) |
| Next.js API | Proxy targets, env, SSRF surface | Proxies to env-configured Flask; **no SSRF sink identified**; all routes inherit the Flask auth gap |
| CSV import/export | Missing cols, bad dates, nan/inf, formula, dups, partial | Not all-or-nothing (AUD-04); non-finite accepted (AUD-06/13); formula injection on export (AUD-13) |
| Frontend journeys | Page/route inventory, interactive controls | Predict/feedback/reference/evaluation flows present; **no upload or editable grid** (AUD-15) |
| Concurrency | Parallel CSV writers, parallel DB writers | CSV race confirmed (AUD-08); SQLite serializes safely (no corruption observed) |
| Property-style | Bounds/clamp/invariant checks over generated values | Clamp bounds and skip-conditions hold; finiteness invariant does **not** (AUD-06) |

---

## D. Findings by severity

Each finding lists 13 fields: **ID · Title · Severity · Confidence/status ·
Subsystem · Location · Description · Reproduction · Observed evidence · Impact ·
Root cause · Recommended fix · Regression test.**

### AUD-05 — Quarantined camera measurements still drive predictions (HIGH)

- **ID:** AUD-05
- **Title:** Unverified-camera rows are aggregated into live predictions and their exclusion is not disclosed
- **Severity:** High
- **Confidence/status:** Confirmed (reproduced end-to-end)
- **Subsystem:** Prediction / calibration quarantine
- **Location:** `plantreadiness/services.py:507` (`repo.species_cohort_snapshot`), `services.py:541-583` (median assembly), `plantreadiness/db.py:1252` (`species_cohort_snapshot`); per-plant path `services.py:377` (`predict_for_plant` → `repo.latest_observation`)
- **Description:** The calibration layer correctly classifies unverified camera rows as `camera_unverified` and excludes them from the **reference baseline**, and `summarise_groups` reports them as quarantined. But the *live* prediction paths never apply that filter: `species_cohort_snapshot` selects **all** observations on the latest date regardless of `calibration_source`/`calibration_verified`, and `predict_for_plant` uses `latest_observation` unconditionally. The resulting prediction carries no calibration/exclusion fields, so the user cannot see that the representative measurement came from quarantined data.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile; from pathlib import Path
  from plantreadiness import db as dbm, services as svc, calibration as calib, config
  from plantreadiness.repository import SQLiteRepository
  tmp = Path(tempfile.mkdtemp()); conn = dbm.init_db(tmp/"c.db"); repo = SQLiteRepository(conn)
  S = config.SPECIES_SUNFLOWER_SUPER_ORANGE; D = "2083-05-10"
  for i in range(1,5):                       # baseline: 4 manual reference plants
      p = repo.register_plant(species=S, data_source="real", external_ref=f"R{i}", cohort="reference")
      repo.add_observation(plant_id=p, observation_date="2083-05-02", age_days=7, height_cm=0.5,
                           growth_rate_cm_per_day=0.1, calibration_source="csv_import_no_calibration")
      repo.add_observation(plant_id=p, observation_date=D, age_days=15, height_cm=2.0,
                           growth_rate_cm_per_day=0.1, calibration_source="csv_import_no_calibration")
  svc.build_reference_baseline(repo, S, models_dir=tmp/"models")
  for i in range(1,6):                       # 5 live plants: ONLY unverified camera data
      p = repo.register_plant(species=S, data_source="real", external_ref=f"L{i}")
      repo.add_observation(plant_id=p, observation_date=D, age_days=15, height_cm=100.0,
                           growth_rate_cm_per_day=0.1, calibration_source=config.CALIBRATION_SOURCE_CAMERA,
                           rod_length_cm=10.0, calibration_verified=False)
  r = svc.predict_species_cohort(repo, S, models_dir=tmp/"models", allow_provisional_baseline=True)
  print("available:", r["available"], "| median_height:", r["cohort_snapshot"]["median_height_cm"])
  print("reports calibration/exclusions?:", any("calib" in k.lower() or "exclu" in k.lower() for k in r))
  PY
  ```
- **Observed evidence:**
  ```
  group counts: {'manual_measurement': 8, 'camera_verified': 0, 'camera_unverified': 5, 'unknown': 0} usable: 8 quarantined: 5
  available: True | method: historical_reference_baseline | days_remaining: 1.0
  snapshot median_height_cm: 100.0 | plants_represented: 9
  result reports calibration/exclusions?: False
  ```
  `predict_for_plant` behaves the same: with a plant whose only data is one unverified camera row (`height_cm=88.0`), the result is `available: True, method: historical_reference_baseline`, `observation.height_cm = 88.0`, and no calibration/exclusion field.
- **Impact:** Directly violates non-negotiable #4 ("be honest in predictions") and #5/#7. Measurements the system has explicitly decided not to trust (unverified rod length, or a legacy `unknown` source) still change the predicted readiness date, and the disclosure fields the design promises ("always report the calibration group used and the exclusion counts") are absent. A miscalibrated camera can therefore move a real recommendation while the UI shows an ordinary provisional prediction.
- **Root cause:** Quarantine is applied when *building the baseline* (`reference.py` calibration filter) but not when *selecting the live observation cohort* (`db.species_cohort_snapshot` / `latest_observation`). The two paths have different filters.
- **Recommended fix:** Apply the same `calibration.is_baseline_usable(...)` filter when assembling the cohort/plant representative observation; if too few usable rows remain, return `insufficient_evidence`. Add `calibration_group`, `usable_rows`, and `excluded_rows` to the result and surface them in the UI.
- **Regression test:** `tests/test_audit_adversarial.py::test_species_prediction_excludes_unverified_camera` (xfail).

### AUD-02 — Deferrals are graded as real transplants (HIGH)

- **ID:** AUD-02
- **Title:** `evaluate_predictions` treats `transplant_occurred=0` as a transplant
- **Severity:** High
- **Confidence/status:** Confirmed
- **Subsystem:** Evaluation / feedback evidence gate
- **Location:** `plantreadiness/services.py:929`
- **Description:** The guard is `if row.get("transplant_occurred", 1) is False: continue`. SQLite returns the column as `int 0/1`, and `0 is False` is `False`, so the guard never fires. A recorded **decision/deferral** (`transplant_occurred=False`) is therefore evaluated as if the transplant had occurred, using its `transplant_date` as ground truth.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile; from pathlib import Path
  from plantreadiness import db as dbm, services as svc, config
  from plantreadiness.repository import SQLiteRepository
  tmp = Path(tempfile.mkdtemp()); conn = dbm.init_db(tmp/"c.db"); repo = SQLiteRepository(conn)
  p = repo.register_plant(species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, data_source="real", external_ref="1")
  repo.record_transplant(plant_id=p, transplant_date="2083-05-10", transplant_occurred=False)  # NO transplant
  repo.save_prediction_snapshot(plant_id=p, species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, variety=None,
      observation_date="2083-05-01", predicted_readiness_date="2083-05-12", days_remaining=11,
      method=config.METHOD_REFERENCE_BASELINE, available=True, prediction_scope="plant", data_source="real")
  print("evaluated:", svc.evaluate_predictions(repo, species=config.SPECIES_SUNFLOWER_SUPER_ORANGE)["n_predictions_evaluated"])
  PY
  ```
- **Observed evidence:**
  ```
  stored transplant_occurred value: 0 int
  'is False' check: False
  n_predictions_evaluated: 1
  by_method: {'historical_reference_baseline': 1}
  ```
- **Impact:** Corrupts the controlled-improvement loop. Deferrals enter the MAE/match-rate as if they were the true transplant date, so the metric that gates model promotion (`decide_update`) is computed against phantom outcomes. Contradicts "#6 missing feedback is pending, never failure" and "#7 improve with evidence".
- **Root cause:** Identity comparison (`is False`) against a value the DB returns as an integer.
- **Recommended fix:** Use falsiness (`if not row.get("transplant_occurred", 1): continue`) or coerce to `bool(...)` at the repository boundary, and add a unit test that a deferral is excluded.
- **Regression test:** `tests/test_audit_adversarial.py::test_deferral_is_not_counted_as_transplant` (xfail).

### AUD-03 — Manual-upload data is silently quarantined (HIGH)

- **ID:** AUD-03
- **Title:** `services.run_csv_import` stores observations with `calibration_source=NULL`, making uploaded reference data unusable
- **Severity:** High
- **Confidence/status:** Confirmed
- **Subsystem:** CSV import / reference baseline
- **Location:** `plantreadiness/services.py:1194-1198`
- **Description:** The strict upload path calls `repo.add_observation(...)` **without** `calibration_source` (and without `rod_length_cm`). The row is stored with `calibration_source = NULL`, and `calibration.calibration_group` maps a blank source to `unknown`, which `is_baseline_usable` rejects. So every observation a user uploads through this path is quarantined and can never contribute to a provisional baseline — the exact opposite of the requirement that "valid manual reference data can still support a provisional prediction". The CLI importer (`plantreadiness/csv_import.py`) tags `csv_import_no_calibration` correctly; only the service/upload path is affected.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile; from pathlib import Path
  from plantreadiness import db as dbm, services as svc, calibration as calib, config
  from plantreadiness.repository import SQLiteRepository
  tmp = Path(tempfile.mkdtemp()); conn = dbm.init_db(tmp/"c.db"); repo = SQLiteRepository(conn)
  csv = "Date,Age_days,Plant_ID,Height_cm\n" + "\n".join(f"2083-05-0{d},{d},R{i},{0.5*d}"
        for i in range(1,5) for d in (2,9))
  rep = svc.run_csv_import(repo, csv, species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, dry_run=False)
  rows = [dict(r) for r in conn.execute("SELECT calibration_source, rod_length_cm FROM observations")]
  print("inserted:", rep["observations_inserted"], "| groups:", calib.summarise_groups(rows)["counts_by_group"])
  base = svc.build_reference_baseline(repo, config.SPECIES_SUNFLOWER_SUPER_ORANGE, models_dir=tmp/"models")
  print("baseline n_plants:", base.get("n_plants"), "n_points:", base.get("n_points"))
  PY
  ```
- **Observed evidence:**
  ```
  inserted: 8
  groups: {'manual_measurement': 0, 'camera_verified': 0, 'camera_unverified': 0, 'unknown': 2} usable: 0 quarantined: 8
  baseline n_plants: 0 n_points: 0
  ```
- **Impact:** The manual dataset upload feature is non-functional for its stated purpose (supporting a provisional baseline), and the failure is **silent** — the import reports success. Users would believe their data is in use when it is not.
- **Root cause:** Missing provenance argument on the service-layer import; provenance is set by the CLI importer only.
- **Recommended fix:** Pass `calibration_source=config.CALIBRATION_SOURCE_CSV` (and an explicit `rod_length_cm=None`) in `run_csv_import`, mirroring `csv_import.import_csv`.
- **Regression test:** `tests/test_audit_adversarial.py::test_uploaded_manual_data_is_baseline_usable` (xfail).

### AUD-01 — New CLIs are un-importable (MEDIUM)

- **ID:** AUD-01
- **Title:** Stray `PY` line at module level in three CLIs raises `NameError` on import
- **Severity:** Medium
- **Confidence/status:** Confirmed
- **Subsystem:** Verification / reprocessing / migration tooling
- **Location:** `plantreadiness/verify_calibration.py:68`, `plantreadiness/reprocess_captures.py:219`, `plantreadiness/migrate_species_rename.py:62`
- **Description:** Each file ends with a heredoc terminator (`PY`) at column 0, after the `if __name__ == "__main__":` block. When run as `python -m …` the guard raises `SystemExit` first, so the CLI works; when the module is **imported**, execution falls through to the bare `PY` expression and raises `NameError`.
- **Reproduction:**
  ```bash
  python3 -c "import plantreadiness.verify_calibration"
  python3 -c "import plantreadiness.reprocess_captures"
  python3 -c "import plantreadiness.migrate_species_rename"
  ```
- **Observed evidence:**
  ```
  File ".../verify_calibration.py", line 68, in <module>
      PY
  NameError: name 'PY' is not defined
  ```
  (same for the other two)
- **Impact:** No library/import integration, no unit testing, and any future caller that imports these modules crashes. It also blocks the natural fix for AUD-10 from being tested in-process.
- **Root cause:** Copy-paste of an unclosed heredoc terminator into the committed file.
- **Recommended fix:** Delete the trailing `PY` line from all three files; add an `import`-smoke test.
- **Regression test:** `tests/test_audit_adversarial.py::test_new_cli_modules_are_importable[3 variants]` (xfail).

### AUD-04 — Manual upload is not all-or-nothing (MEDIUM)

- **ID:** AUD-04
- **Title:** Invalid rows do not abort the import; valid rows are committed
- **Severity:** Medium
- **Confidence/status:** Confirmed
- **Subsystem:** CSV import
- **Location:** `plantreadiness/services.py:1161-1204`
- **Description:** `run_csv_import` computes `preview["valid"]` (all rows valid) but never consults it when `dry_run=False`. It iterates rows, commits each good one, and merely appends an issue for bad ones. A file with one typo therefore leaves a **partially imported** state.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile; from pathlib import Path
  from plantreadiness import db as dbm, services as svc, config
  from plantreadiness.repository import SQLiteRepository
  tmp = Path(tempfile.mkdtemp()); conn = dbm.init_db(tmp/"c.db"); repo = SQLiteRepository(conn)
  csv = "Date,Age_days,Plant_ID,Height_cm\n2083-05-02,7,R1,1.0\nbad-date,7,R2,2.0\n2083-05-03,8,R1,1.5\n"
  rep = svc.run_csv_import(repo, csv, species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, dry_run=False)
  print("inserted:", rep["observations_inserted"], "issues:", len(rep["issues"]))
  print([tuple(r) for r in conn.execute("SELECT external_ref, observation_date FROM observations o JOIN plants p USING(plant_id)")])
  PY
  ```
- **Observed evidence:**
  ```
  inserted: 2 issues: 1
  rows in DB after a partially-bad import: [('R1', '2083-05-02'), ('R1', '2083-05-03')]
  ```
- **Impact:** Contradicts the required "all-or-nothing validation" for manual uploads. Users get a silently mixed dataset; re-uploading after fixing the typo is partially idempotent, which makes the true state hard to reason about.
- **Root cause:** `preview()` result ignored; per-row commit with no transaction around the batch.
- **Recommended fix:** Refuse to import when `preview["valid"]` is false (return the issues), or wrap the batch in a single transaction and roll back on any issue.
- **Regression test:** `tests/test_audit_adversarial.py::test_manual_upload_is_all_or_nothing` (xfail).

### AUD-06 — `+inf` height is stored as a real measurement (MEDIUM)

- **ID:** AUD-06
- **Title:** Numeric validation rejects negatives but not non-finite values
- **Severity:** Medium
- **Confidence/status:** Confirmed
- **Subsystem:** DB validation / measurement ingestion
- **Location:** `plantreadiness/db.py:915-930` (`_validate_observation_numbers`), `plantreadiness/services.py:1105` (`height < 0`), `plantreadiness/camera_import.py` height check
- **Description:** Validation uses `float(x) < 0`. For `NaN` and `+inf` this comparison is `False`, so both pass. SQLite happens to coerce `NaN` to `NULL` (benign, "missing"), but `+inf` is stored verbatim as the height and flows into medians and predictions. The same pattern exists in the upload parser and the camera importer.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile; from pathlib import Path
  from plantreadiness import db as dbm, config
  from plantreadiness.repository import SQLiteRepository
  tmp = Path(tempfile.mkdtemp()); conn = dbm.init_db(tmp/"c.db"); repo = SQLiteRepository(conn)
  p = repo.register_plant(species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, data_source="real", external_ref="1")
  repo.add_observation(plant_id=p, observation_date="2083-05-02", age_days=7, height_cm=float("inf"))
  print(conn.execute("SELECT height_cm FROM observations").fetchone()[0])
  PY
  ```
- **Observed evidence:**
  ```
  add_observation height=nan: ACCEPTED stored=None     # SQLite coerces NaN -> NULL
  upsert height=inf: inserted stored=inf
  update height=nan: updated stored=None
  growth_rate=nan: ACCEPTED
  ```
- **Impact:** An infinite (or, on other DB backends, NaN) height can be averaged into the cohort/baseline and used in a prediction. Violates "never invent data" and produces nonsensical downstream numbers.
- **Root cause:** Finiteness is not checked; only sign is.
- **Recommended fix:** Add `math.isfinite(float(x))` checks (rejecting `nan`/`±inf`) alongside the `>= 0` checks in `db._validate_observation_numbers`, the CSV parsers, and the camera importer.
- **Regression test:** `tests/test_audit_adversarial.py::test_add_observation_rejects_non_finite_height` (xfail).

### AUD-07 — Capture integrity: infinite heights and cross-plant reassignment (MEDIUM)

- **ID:** AUD-07
- **Title:** `add_capture` accepts `+inf` and lets a `public_id` move between plants
- **Severity:** Medium
- **Confidence/status:** Confirmed
- **Subsystem:** Camera captures
- **Location:** `plantreadiness/db.py:615` (`add_capture`)
- **Description:** (a) A capture can be stored as `validity='valid'` with `height_cm='inf'` — same finiteness gap as AUD-06, but here the row is explicitly marked valid and later feeds `median_daily_record`. (b) On a repeat `add_capture` with the same `public_id`, the existing row is looked up **by `public_id` only** and its `plant_id` is overwritten with the new argument, so a capture can be silently moved to a different plant.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile; from pathlib import Path
  from plantreadiness import db as dbm, config
  from plantreadiness.repository import SQLiteRepository
  tmp = Path(tempfile.mkdtemp()); conn = dbm.init_db(tmp/"c.db"); repo = SQLiteRepository(conn)
  p1 = repo.register_plant(species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, data_source="real", external_ref="1")
  p2 = repo.register_plant(species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, data_source="real", external_ref="2")
  repo.add_capture(plant_id=p1, public_id="Plant01_20830510_100000", observation_date="2083-05-10",
                   validity="valid", rod_length_cm=10.0, height_cm=float("inf"), confidence=0.9)
  print("stored:", tuple(conn.execute("SELECT height_cm, validity FROM camera_captures").fetchone()))
  repo.add_capture(plant_id=p2, public_id="Plant01_20830510_100000", observation_date="2083-05-10",
                   validity="valid", rod_length_cm=10.0, height_cm=2.0, confidence=0.9)
  print("plant_id now:", conn.execute("SELECT plant_id FROM camera_captures").fetchone()[0], "(was", p1, ")")
  PY
  ```
- **Observed evidence:**
  ```
  capture inf height stored: (inf, 'valid')
  after re-add under a DIFFERENT plant, capture plant_id: 5 (was 1) height: 2.0
  ```
- **Impact:** (a) An infinite height enters the four-capture median and onward. (b) Camera identity is `public_id`, but nothing prevents that identity from being reassigned to another plant, so provenance/audit history can be silently rewritten.
- **Root cause:** Same finiteness gap plus a `public_id`-scoped lookup with an unconditional `plant_id` update.
- **Recommended fix:** Reject non-finite capture metrics; on an existing `public_id`, either refuse a different `plant_id` or treat it as a hard conflict rather than mutating the row.
- **Regression test:** `tests/test_audit_adversarial.py::test_capture_integrity` (xfail).

### AUD-08 — Lost updates in the daily CSV under concurrency (MEDIUM)

- **ID:** AUD-08
- **Title:** `upsert_daily_row` is a lock-free read-modify-write
- **Severity:** Medium
- **Confidence/status:** Confirmed (deterministic with an injected delay)
- **Subsystem:** Daily aggregation store
- **Location:** `plant_measurement.py:2126` (`_read_daily_rows`), `2149` (`upsert_daily_row`), `2135` (`_write_daily_rows`)
- **Description:** Each upsert reads the whole CSV, merges its row in memory, and rewrites the file. Two writers that read before either writes will each write only their own view, dropping the other's row.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile, threading, time; from pathlib import Path
  import plant_measurement as pm
  path = Path(tempfile.mkdtemp())/"daily.csv"
  orig = pm._write_daily_rows
  pm._write_daily_rows = lambda p, rows: (time.sleep(0.1), orig(p, rows))[1]   # widen race window
  def rec(pid): return [pm.CaptureRecord(public_id=f"{pid}_x", capture_date="2083-05-10",
      capture_timestamp="2083-05-10T10:00:00", validity="valid", height_cm=10.0)]
  def work(pid): pm.write_daily_record("d", pid, [], csv_file=path, capture_date="2083-05-10", records=rec(pid))
  ts=[threading.Thread(target=work, args=(f"Plant-{i:02d}",)) for i in range(8)]
  [t.start() for t in ts]; [t.join() for t in ts]
  print("rows:", len(pm._read_daily_rows(path)[0]), "of 8 expected")
  PY
  ```
- **Observed evidence:**
  ```
  concurrent upsert_daily_row loses rows? expected 8 distinct plant rows, got 5
  ```
  (with the injected delay the loss is deterministic.)
- **Impact:** The CSV daily store — the pipeline's published aggregate — can silently lose a plant-day record when multiple processes write concurrently (e.g. overlapping CLI runs, or a multi-worker WSGI deployment). The DB capture store is unaffected, but the aggregate is not.
- **Root cause:** No file locking or atomic replace around the read-modify-write.
- **Recommended fix:** Serialize with an OS file lock (`fcntl.flock`) or write to a temp file + `os.replace` under a lock; or make the CSV a derived export of the DB rather than a write target.
- **Regression test:** `tests/test_audit_adversarial.py::test_daily_csv_concurrent_writes_do_not_lose_rows` (xfail).

### AUD-09 — `init_db` reclassifies honest `unknown` rows (MEDIUM)

- **ID:** AUD-09
- **Title:** The "one-time" provenance migration runs on every `init_db` and promotes `unknown` rows to baseline-usable `manual`
- **Severity:** Medium
- **Confidence/status:** Confirmed
- **Subsystem:** Migrations / provenance
- **Location:** `plantreadiness/db.py:389-393`
- **Description:** The statement `UPDATE observations SET calibration_source='csv_import_no_calibration' WHERE calibration_source='unknown' AND rod_length_cm IS NULL` is executed unconditionally on every `init_db`, not once. A genuinely `unknown` row with no rod length is therefore turned into a manual row — which `is_baseline_usable` **accepts** — after any subsequent `init_db`.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  import tempfile; from pathlib import Path
  from plantreadiness import db as dbm, calibration as calib, config
  from plantreadiness.repository import SQLiteRepository
  f = Path(tempfile.mkdtemp())/"x.db"; conn = dbm.init_db(f); repo = SQLiteRepository(conn)
  p = repo.register_plant(species=config.SPECIES_SUNFLOWER_SUPER_ORANGE, data_source="real", external_ref="1")
  repo.add_observation(plant_id=p, observation_date="2083-05-02", age_days=0, height_cm=1.0,
                       calibration_source=config.CALIBRATION_SOURCE_UNKNOWN)
  def grp():
      r = dict(conn.execute("SELECT calibration_source, rod_length_cm, calibration_verified FROM observations").fetchone())
      g = calib.calibration_group(calibration_source=r["calibration_source"], rod_length_cm=r["rod_length_cm"],
                                  calibration_verified=r["calibration_verified"])
      return r["calibration_source"], g, calib.is_baseline_usable(g)
  print("before:", grp()); conn.close(); dbm.init_db(f)
  conn = dbm.connect(f); print("after :", grp())
  PY
  ```
- **Observed evidence:**
  ```
  before re-init: ('unknown', 'unknown', False)
  after  re-init: ('csv_import_no_calibration', 'manual_measurement', True)
  ```
- **Impact:** A re-run of `init_db` (which the app calls routinely, and which `run.sh`/tests trigger) silently changes provenance classification and can move distrust data into the trusted baseline. Breaks the append-only/honest-provenance contract; the "one-time" migration is not idempotent in effect.
- **Root cause:** A data-mutating migration placed in the general schema-bootstrap path with a predicate (`='unknown' AND rod_length IS NULL`) broad enough to match future rows.
- **Recommended fix:** Gate the tagging on the column actually being created (inside the `if "calibration_source" not in obs_cols:` branch), or record applied migrations in a `schema_migrations` table and run each exactly once.
- **Regression test:** `tests/test_audit_adversarial.py::test_init_db_does_not_reclassify_unknown` (xfail).

### AUD-10 — Reprocessing wipes the derived growth rate (MEDIUM)

- **ID:** AUD-10
- **Title:** `reprocess_captures --apply` sets `growth_rate_cm_per_day` back to `NULL`
- **Severity:** Medium
- **Confidence/status:** Confirmed (via CLI subprocess; the module is un-importable per AUD-01)
- **Subsystem:** Capture reprocessing
- **Location:** `plantreadiness/reprocess_captures.py:199` (`repo.upsert_daily_observation(...)` without a growth rate)
- **Description:** After re-measuring a capture, the tool recomputes the daily observation by calling `upsert_daily_observation` with height/stem/leaves only. Because an upsert writes all supplied fields, the previously derived `growth_rate_cm_per_day` is overwritten with `NULL` and is not recomputed. No duplicate rows/captures are created (the append-only/idempotency guarantee holds).
- **Reproduction:**
  ```bash
  # (build a temp DB with two daily rows and a capture, plus a local image, then:)
  python3 -m plantreadiness.reprocess_captures --db repro.db --apply
  ```
- **Observed evidence:**
  ```
  BEFORE: height= 4.0 growth_rate= 0.5 n_obs= 2 n_cap= 1
  AFTER:  height= 6.973684210526315 growth_rate= None n_obs= 2 n_cap= 1
  revisions: [('create', 1), ('recompute', 1)]
  ```
- **Impact:** A maintenance action (re-processing after verifying calibration) silently discards a real derived measurement, degrading the growth-rate evidence used by the provisional adjustment.
- **Root cause:** The recompute call omits `growth_rate_cm_per_day` (and relies on full-replace upsert semantics).
- **Recommended fix:** Recompute the growth rate from the previous observation before upserting (reuse `camera_import`'s logic), or make the upsert preserve existing values for unspecified fields.
- **Regression test:** Not added as an in-process test because the module cannot be imported (AUD-01); reproducible via the subprocess command above.

### AUD-11 — Unauthenticated API on all interfaces (MEDIUM)

- **ID:** AUD-11
- **Title:** Flask exposes state-changing routes with no authentication and binds `0.0.0.0`
- **Severity:** Medium (a local prototype, but the documented run command binds all interfaces and there is no auth on any route)
- **Confidence/status:** Confirmed (bind + absence of auth); exploitation is trivial for anyone on the same network
- **Subsystem:** Flask backend / Next.js proxy
- **Location:** `backend/app.py:485` (`app.run(host='0.0.0.0', port=8000)`), `run.sh:31` (`flask run --host=0.0.0.0 --port=8000`); routes `/register`, `/api/reference/build`, `/api/camera/refresh`, `/api/transplant`, `/api/feedback`, `/api/assessments`, `/api/evaluation`
- **Description:** There is no authentication, authorization, session, token, or API-key mechanism anywhere in the backend or the Next.js API routes (grep returned nothing). The server binds all interfaces. Every state-changing endpoint is therefore reachable by any host on the network. Additionally, `/register` forwards the caller-supplied `cohort` and `data_source` verbatim (`backend/app.py:90-105`). `data_source` is validated against `VALID_SOURCES`, but `cohort` is not — `build_reference_from_repo` prefers plants tagged `cohort='reference'`, so a caller can tag arbitrary plants as reference-cohort members.
- **Reproduction:**
  ```bash
  grep -rniE "auth|login|session|token|password|api[_-]?key" backend/app.py nextjs/pages/api || echo "no auth found"
  grep -n "0.0.0.0" backend/app.py run.sh
  # then, from another host on the LAN:
  curl -X POST http://<host>:8000/register -H 'Content-Type: application/json' \
       -d '{"species":"X","cohort":"reference","data_source":"real"}'
  ```
- **Observed evidence:**
  ```
  backend/app.py:485:    app.run(host='0.0.0.0', port=8000)
  run.sh:31:flask run --host=0.0.0.0 --port=8000 &
  (auth/session/login/token grep: no matches)
  ```
- **Impact:** Anyone able to reach the host can register plants (including tagging them into the reference cohort), trigger Cloudinary camera pulls (`/api/camera/refresh`), rebuild/write baseline files, and submit transplant/feedback/assessment records. Combined with AUD-02/AUD-05 this can bias the prediction/evaluation evidence. Note this is an intentionally local tool with no auth design; the exposure comes from binding `0.0.0.0` rather than `127.0.0.1`.
- **Root cause:** No authentication layer; server bound to all interfaces.
- **Recommended fix:** If local-only, bind `127.0.0.1`. If LAN access is intended, add authentication and validate `cohort` against an allowlist (or remove it from the public registration payload). Document the trust boundary.
- **Regression test:** `UNVERIFIED` as an automated test (would require a live server and network peer); documented as a configuration/design finding.

### AUD-12 — Messy API error handling (LOW)

- **ID:** AUD-12
- **Title:** Type-confused inputs yield HTML 500s or leak Python internals
- **Severity:** Low
- **Confidence/status:** Confirmed
- **Subsystem:** Flask API
- **Location:** `backend/app.py` — `/predict` (`int(data['plantId'])`), `/api/predictions` (query parsing), `/register` (unbroadened `str(e)`)
- **Description:** Several routes pass unvalidated values into `int()`/`float()` and catch only `ValueError`/`TypeError`, or return raw exception strings. `plantId = 1e400` becomes `inf` and raises `OverflowError`; `?plant_id=abc` raises `ValueError`; both produce Flask's default **HTML** 500 page. Non-JSON content types return Flask's HTML 415. `/register` with a non-string species returns `str(exc)` (e.g. `'int' object has no attribute 'strip'`). SQL injection itself is **not** possible — all queries are parameterized (`DROP TABLE` as a value was treated as a value).
- **Reproduction:**
  ```bash
  # via Flask test client (see audit scripts) or:
  curl -s -o /dev/null -w '%{http_code} %{content_type}\n' 'http://localhost:8000/api/predictions?plant_id=abc'
  curl -s -X POST http://localhost:8000/predict -H 'Content-Type: application/json' -d '{"plantId":1e400}'
  ```
- **Observed evidence:**
  ```
  POST /predict {"plantId": 1e400}                -> 500 [text/html]   (OverflowError logged)
  GET  /api/predictions?plant_id=abc              -> 500 [text/html]   (ValueError logged)
  GET  /api/feedback?plant_id=1;DROP TABLE plants -> 500 [application/json] "invalid literal for int()..."
  POST /register {"species": 123}                 -> 400 "'int' object has no attribute 'strip'"
  POST /register (text/plain)                     -> 415 [text/html]
  ```
- **Impact:** Inconsistent, non-machine-readable errors; internal detail leakage (minor); the Next.js proxy may mis-handle HTML error bodies. Not a direct compromise.
- **Root cause:** Ad-hoc per-route validation with inconsistent exception handling.
- **Recommended fix:** Add a global error handler that maps `ValidationError`→400, `NotFoundError`→404, `DuplicateRecordError`→409 and everything else→500 **JSON**; validate/coerce `plantId` (reject non-finite); guard `request.get_json(silent=True)`.
- **Regression test:** `UNVERIFIED` (behaviour observed via the Flask test client; a light test is easy to add once the error contract is defined).

### AUD-13 — CSV accepts non-finite values and formula payloads; export does not neutralize (LOW)

- **ID:** AUD-13
- **Title:** `nan`/`inf` heights accepted; leading `=`/`+`/`-` values exported verbatim
- **Severity:** Low
- **Confidence/status:** Confirmed
- **Subsystem:** CSV import/export
- **Location:** `plantreadiness/services.py:1094` (`float(row["Height_cm"])`), `services.py:1217` (`export_table_csv`)
- **Description:** The parser accepts `nan` and `inf` strings (they parse as floats and pass `< 0`), and accepts plant refs/data containing spreadsheet formulas. `export_table_csv` writes raw values, so a value beginning with `=` is emitted as a live formula when the export is opened in Excel/Sheets (CSV injection). Missing-column headers raise a clean `ValidationError`; duplicate headers are accepted with last-column-wins.
- **Reproduction:**
  ```bash
  python3 - <<'PY' 2>/dev/null
  from plantreadiness import services as svc
  for t in ["Date,Age_days,Plant_ID,Height_cm\n2083-05-02,7,R1,nan\n",
            "Date,Age_days,Plant_ID,Height_cm\n2083-05-02,7,R1,inf\n"]:
      print(svc.preview_csv_import(t)["valid"])
  PY
  # formula export: register a plant with external_ref '=HYPERLINK("http://evil")', then export_table_csv(repo,'plants')
  ```
- **Observed evidence:**
  ```
  nan_height     valid=True rows=1 issues=[]
  inf_height     valid=True rows=1 issues=[]
  exported snippet: 1,Sunflower (SuperOrange),,,real,,"=HYPERLINK(""http://evil"")",,...
  ```
- **Impact:** Non-finite data pollutes downstream stats (AUD-06); formula injection is a known hazard if exported CSVs are opened in a spreadsheet (no HTTP export route is wired today, so exposure is limited to local files).
- **Root cause:** Missing finiteness check; no CSV-injection neutralization on export.
- **Recommended fix:** Reject non-finite heights; prefix values beginning with `= + - @` with `'` (or a space) on export.
- **Regression test:** `UNVERIFIED` (covered partially by AUD-06's test).

### AUD-14 — Inconsistent configuration validation (LOW)

- **ID:** AUD-14
- **Title:** Invalid rod length bricks startup; some settings are unvalidated (NaN confidence disables the gate)
- **Severity:** Low
- **Confidence/status:** Confirmed
- **Subsystem:** Configuration
- **Location:** `plantreadiness/config.py` (`positive_float_setting` for `CAMERA_ROD_LENGTH_CM`; raw `int(os.getenv("CAMERA_ROD_HUE", "0"))`; raw `float(os.getenv("CAPTURE_MIN_CONFIDENCE", "0.0"))`)
- **Description:** `CAMERA_ROD_LENGTH_CM` is validated at import and raises a clear `ValueError` for `abc`/`0`/`nan`/`inf` — fail-closed (no bogus measurements) but the process cannot start and there is no fallback. Other settings bypass validation: a non-integer `CAMERA_ROD_HUE` raises a raw `ValueError`, and `CAPTURE_MIN_CONFIDENCE` accepts `nan`, which makes `confidence <= CAPTURE_MIN_CONFIDENCE` always false and silently **disables** the confidence gate; it also accepts negatives.
- **Reproduction:**
  ```bash
  CAMERA_ROD_LENGTH_CM=abc python3 -c "import plantreadiness.config"
  CAMERA_ROD_HUE=notanint    python3 -c "import plantreadiness.config"
  CAPTURE_MIN_CONFIDENCE=nan python3 -c "import plantreadiness.config as c; import math; print(math.isfinite(c.CAPTURE_MIN_CONFIDENCE))"
  ```
- **Observed evidence:**
  ```
  ValueError: CAMERA_ROD_LENGTH_CM must be a number of centimetres, got 'abc'.
  ValueError: invalid literal for int() with base 10: 'notanint'
  nan isfinite: False
  ```
- **Impact:** A config typo prevents the whole application from starting (no graceful degradation), while a NaN confidence can quietly remove a quality filter. Both are operational/safety concerns rather than data corruption.
- **Root cause:** Only one setting went through a validating helper; the rest use raw `int()`/`float()`.
- **Recommended fix:** Route rod hue/tolerance and `CAPTURE_MIN_CONFIDENCE` through validating helpers (finite, non-negative, bounded); decide explicitly whether a bad rod length should be fatal or should disable camera import with a clear runtime error.
- **Regression test:** `UNVERIFIED` (subprocess checks shown above).

### AUD-15 — Unreachable frontend workflows (LOW)

- **ID:** AUD-15
- **Title:** No editable reference grid and no manual dataset upload control
- **Severity:** Low
- **Confidence/status:** Confirmed (static inspection)
- **Subsystem:** Next.js frontend
- **Location:** `nextjs/pages/feedback.tsx:735-825` (Reference tab)
- **Description:** The Reference tab renders a read-only table and a "Rebuild" button; there is no `<input type="file">`, no editable cells, and no `<form>`. The manual-upload service (AUD-03) and an editable reference sheet have no UI entry point. Present-day per-plant characteristics are not surfaced either (only the cohort snapshot).
- **Reproduction:**
  ```bash
  grep -n "<input type=\"file\"\|<form\|contentEditable" nextjs/pages/feedback.tsx   # no matches
  grep -c "onChange" nextjs/pages/feedback.tsx   # controls exist for feedback/transplant only
  ```
- **Observed evidence:** Interactive controls exist for the feedback/transplant/assessment flows (`setAssess*`, `setTx*`, `setFollow*`) but none for reference editing or file upload.
- **Impact:** Documented/designed workflows cannot be exercised from the UI, so features like the (broken) manual upload are both unreachable and untested end-to-end.
- **Root cause:** Frontend scope incomplete relative to the backend.
- **Recommended fix:** Either implement the upload/editable-grid UI, or remove the claims from the docs and mark the capability backend-only/experimental.
- **Regression test:** `UNVERIFIED` (no UI test harness in the repo).

### AUD-16 — Species-rename alias gap (INFO)

- **ID:** AUD-16
- **Title:** Only the exact legacy string `"Super Orange"` is renamed; variants are stranded
- **Severity:** Informational
- **Confidence/status:** Confirmed
- **Subsystem:** Migration / species identity
- **Location:** `plantreadiness/migrate_species_rename.py:18-32`
- **Description:** The migration matches `species = config.LEGACY_SPECIES_SUPER_ORANGE` exactly. `"SuperOrange"` (no space) or `"Apsara Yellow"` (bare) are neither migrated nor resolved by a runtime alias, so such plants remain under a name that the new baseline/species catalogue does not recognise.
- **Reproduction:**
  ```bash
  # temp DB with 'Super Orange', 'SuperOrange', 'Marigold (Apsara Yellow)'
  python3 -m plantreadiness.migrate_species_rename --db rename.db --yes
  # -> 'SuperOrange' remains unchanged
  ```
- **Observed evidence:**
  ```
  before: ['Super Orange', 'SuperOrange', 'Marigold (Apsara Yellow)']
  apply:  Renamed 1 plant(s).
  after:  ['Sunflower (SuperOrange)', 'SuperOrange', 'Marigold (Apsara Yellow)']   (3 rows preserved)
  second apply: Renamed 0 plant(s).   # idempotent
  ```
- **Impact:** Low today (the production DB contains no such variants), but a data set with common spelling variants would be silently split across labels.
- **Root cause:** Single exact-match predicate; no alias/normalisation layer.
- **Recommended fix:** Add an alias map (e.g. `SuperOrange`, `Super Orange` → canonical) applied both at migration and at lookup time.
- **Regression test:** `UNVERIFIED` (behaviour demonstrated above).

---

## E. Failed-test catalogue

The committed suite does not fail today (202 passed). To make the defects
regression-visible, this audit added `tests/test_audit_adversarial.py`
(**test-only**), whose `xfail(strict=True)` cases currently fail by design:

| Test | Finding | Expected (failing) behaviour |
|---|---|---|
| `test_new_cli_modules_are_importable[verify_calibration]` | AUD-01 | Import succeeds |
| `test_new_cli_modules_are_importable[reprocess_captures]` | AUD-01 | Import succeeds |
| `test_new_cli_modules_are_importable[migrate_species_rename]` | AUD-01 | Import succeeds |
| `test_deferral_is_not_counted_as_transplant` | AUD-02 | `n_predictions_evaluated == 0` for a deferral |
| `test_uploaded_manual_data_is_baseline_usable` | AUD-03 | `quarantined_rows == 0`, all rows usable |
| `test_manual_upload_is_all_or_nothing` | AUD-04 | `observations_inserted == 0` when any row is invalid |
| `test_species_prediction_excludes_unverified_camera` | AUD-05 | Median ignores quarantined rows; result discloses calibration |
| `test_add_observation_rejects_non_finite_height` | AUD-06 | `ValidationError` on `+inf` |
| `test_capture_integrity` | AUD-07 | Non-finite rejected; `public_id` cannot move plant |
| `test_daily_csv_concurrent_writes_do_not_lose_rows` | AUD-08 | All 8 rows present |
| `test_init_db_does_not_reclassify_unknown` | AUD-09 | Source stays `unknown` |
| `test_growth_adjustment_is_clamped` (PASS) | — | 1.0×, clamp 0.5–2.0, skip non-finite — correct |
| `test_capture_name_*` (PASS) | — | Valid names accepted, malformed rejected — correct |

```
$ python3 -m pytest tests/test_audit_adversarial.py -q
10 passed, 11 xfailed in 1.17s

$ python3 -m pytest -q
212 passed, 11 xfailed in 24.46s
```

No failing test was weakened or skipped, and no application code was changed to
make anything pass.

---

## F. Data-integrity & migration results

- **`init_db` idempotency (record counts):** running `init_db` three times on a
  populated DB left the observation count unchanged (1 → 1). No row is dropped.
- **`init_db` provenance (semantics):** **not** idempotent — see AUD-09; an
  `unknown` row becomes `manual_measurement` (baseline-usable) on re-run.
- **Species rename:** dry-run by default (0 writes), `--yes` renamed exactly the
  legacy rows, a second `--yes` renamed 0 (idempotent), and the plant row count
  was preserved (`rows lost?: 3` for 3 input plants). Variants unhandled (AUD-16).
- **Reprocessing:** no duplicate observations or captures were created
  (`n_obs=2`, `n_cap=1` before and after; `capture_revisions` recorded
  `create` then `recompute`), but the derived growth rate was lost (AUD-10).
- **Real production DB (read-only inspection):** 11 plants, 251 observations,
  0 captures, all plants `Sunflower (SuperOrange)`, all observations
  `csv_import_no_calibration`, `calibration_settings` empty. No experiment
  touched it; it remains intact and unchanged.
- **Backups:** the audit never used or altered the real DB backup.

---

## G. Security findings

Mapped to the **OWASP API Security Top 10 (2023)** checklist:

| OWASP item | Applicable? | Notes |
|---|---|---|
| API1 Broken Object Level Authorization | No (no auth to break) | Every object is reachable once you reach the host — see API2/API5. |
| API2 Broken Authentication | **Yes (AUD-11)** | No authentication on any route; server binds `0.0.0.0`. |
| API3 Broken Object Property Level Authorization (mass assignment) | **Yes (AUD-11 notes)** | `/register` forwards caller-controlled `cohort` (unvalidated) and `data_source`; `cohort='reference'` influences baseline plant selection. |
| API4 Unrestricted Resource Consumption | Partial | `/api/camera/refresh` can trigger Cloudinary pulls; no rate limiting. `UNVERIFIED` as an exploit. |
| API5 Broken Function Level Authorization | Yes (AUD-11) | No roles; all functions public. |
| API6 Unrestricted Access to Sensitive Business Flows | **Yes (AUD-11)** | Reference build, registration, transplant/feedback are unauthenticated. |
| API7 Server-Side Request Forgery | No sink found | `PYTHON_BACKEND_URL` is server-side env, not user input; Cloudinary URLs come from the Cloudinary API. |
| API8 Security Misconfiguration | Partial | No security headers/CORS; `0.0.0.0` bind; verbose exception strings (AUD-12). |
| API9 Improper Inventory Management | Minor | Mixed prefixes (`/plants` vs `/api/*`); no versioning. |
| API10 Unsafe Consumption of APIs | Minor | Consumes Cloudinary; consumes user CSV (AUD-13). |

Additional notes:

- **SQL injection:** not found — queries are parameterized; hostile values such
  as `1;DROP TABLE plants` were treated as literals and produced a 500, not a
  mutation.
- **Path traversal:** `baseline_path` sanitizes `/` to `-`, so `../../etc/passwd`
  cannot escape `models/reference/`. Species strings are not used for reads
  outside that directory.
- **Client-side injection:** no `dangerouslySetInnerHTML`/`contentEditable` in
  the Next.js pages; React escaping applies.
- **CSRF:** JSON bodies trigger a CORS preflight, which the backend does not
  answer, limiting cross-site JSON CSRF; but the missing auth (AUD-11) is the
  dominant concern.
- **Secrets:** no secret value is reproduced in this report; `.env` was not read
  into the report.

---

## H. Blind spots / what was NOT tested

- **Live Cloudinary round-trip** (download by `public_id`, verify+reprocess
  re-fetch). The audit ran offline with synthetic images; the production raw
  image `Plant01_20261016_095000.jpg` is missing from disk and its presence in
  Cloudinary remains **unanswered**.
- **Real-hardware vision quality** — no real ESP32-CAM images were measured;
  the OpenCV results here are for synthetic frames only.
- **Long-running / multi-worker deployment** — AUD-08's race was demonstrated in
  threads; a real gunicorn/multi-process run was not staged.
- **Browser end-to-end journey** — pages were inspected statically; no Playwright
  run against `:3000`/`:8000` was performed for this report.
- **Model retraining/promotion execution** — the gate logic was read; no real
  candidate was trained and promoted.
- **Auth/authorization** — none exists, so it was assessed as absent rather than
  tested.
- **Time-zone boundaries** across the capture-name (local) vs Cloudinary
  (`created_at`, UTC) transition were reasoned about but not exercised with a
  clock-shifted environment.

---

## I. Prioritized remediation

1. **AUD-05 (High)** — apply the calibration-usable filter to live cohort/plant
   observation selection and disclose `calibration_group`/exclusions; refuse
   when usable data is insufficient.
2. **AUD-02 (High)** — fix the truthiness check (or coerce `transplant_occurred`
   to `bool` at the repository boundary) so deferrals are excluded from
   evaluation; re-run the evaluation gate afterwards.
3. **AUD-03 (High)** — pass `calibration_source=CSV` in `run_csv_import` so
   uploaded manual data is baseline-usable; add an end-to-end upload→prediction
   test.
4. **AUD-01 (Medium)** — delete the trailing `PY` lines; add import-smoke tests.
5. **AUD-06 / AUD-07 (Medium)** — reject non-finite numeric input everywhere
   (DB, CSV, camera) and stop mutating a capture's `plant_id`.
6. **AUD-04 (Medium)** — make manual upload transactional/all-or-nothing.
7. **AUD-08 (Medium)** — lock or atomically replace the daily CSV; or derive it
   from the DB.
8. **AUD-09 (Medium)** — make the provenance migration one-shot (guard it on
   column creation or a `schema_migrations` table).
9. **AUD-10 (Medium)** — preserve/recompute the growth rate during reprocessing.
10. **AUD-11 (Medium)** — bind `127.0.0.1` (local-only) or add auth; validate
    `cohort` on `/register`; document the trust boundary.
11. **AUD-12 / AUD-13 / AUD-14 / AUD-15 (Low)** — JSON error contract, finiteness
    + formula neutralization, validated config helpers, and (or) reconcile the
    UI/docs.
12. **AUD-16 (Info)** — add a species alias map.

---

## J. Final test results

```
# Baseline (before audit changes)
$ python3 -m pytest -q
202 passed in 25.26s                                   # exit 0

$ cd nextjs && npm run build
✓ built, all API routes + /feedback + / prerendered   # exit 0

# Adversarial regression file (test-only, added by this audit)
$ python3 -m pytest tests/test_audit_adversarial.py -q
10 passed, 11 xfailed in 1.17s

# Full suite (after adding the audit file)
$ python3 -m pytest -q
212 passed, 11 xfailed in 24.46s
```

- **Passing:** all 202 original tests (unchanged) + 10 passing guards added by
  the audit.
- **xfailed (documented, currently-failing defects):** 11, mapping to AUD-01
  through AUD-09.
- **Skipped:** none. **Not executed:** none of the committed tests.
- **Application code changed:** none. All changes are test-only
  (`tests/test_audit_adversarial.py`) plus this report.
- **Blocked execution:** the three CLIs could not be exercised in-process
  because they are un-importable (AUD-01); they were driven via
  `python -m …` subprocesses instead.
