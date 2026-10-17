"""Adversarial QA regression tests (audit artefact, test-only).

Every ``xfail(strict=True)`` test below encodes the behaviour the system is
*supposed* to have and therefore currently **fails** against a confirmed defect
documented in ``docs/audit/broken_test.md``.  They exist so the defects are reproducible on
demand::

    python3 -m pytest tests/test_audit_adversarial.py -q

When a defect is fixed the matching test flips to XPASS, which strict mode turns
into a failure - i.e. the test tells you to promote it to a normal assertion and
update the report.  No application code is changed by this file.
"""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path

import pytest

from plantreadiness import calibration as calib
from plantreadiness import config
from plantreadiness import db as dbm
from plantreadiness import reference as ref
from plantreadiness import services as svc
from plantreadiness.repository import SQLiteRepository
import plant_measurement as pm

SPECIES = config.SPECIES_SUNFLOWER_SUPER_ORANGE
MANUAL = config.CALIBRATION_SOURCE_CSV
CAMERA = config.CALIBRATION_SOURCE_CAMERA


def _repo(tmp_path: Path) -> tuple[SQLiteRepository, "dbm.sqlite3.Connection"]:  # type: ignore[name-defined]
    conn = dbm.init_db(tmp_path / "audit.db")
    return SQLiteRepository(conn), conn


def _reference_cohort(repo: SQLiteRepository) -> None:
    """Four tagged reference plants with a manual (baseline-usable) history."""
    for i in range(1, 5):
        p = repo.register_plant(species=SPECIES, data_source="real",
                                external_ref=f"R{i}", cohort="reference")
        repo.add_observation(plant_id=p, observation_date="2083-05-02",
                             age_days=7, height_cm=0.5,
                             growth_rate_cm_per_day=0.1,
                             calibration_source=MANUAL)
        repo.add_observation(plant_id=p, observation_date="2083-05-10",
                             age_days=15, height_cm=2.0,
                             growth_rate_cm_per_day=0.1,
                             calibration_source=MANUAL)


# --------------------------------------------------------------------------- #
# Correct behaviour (regression guards that should PASS today)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "Plant01_20261016_095000",
    "Plant01_20261016_235959",
    "  Plant01_20261016_095000  ",
])
def test_capture_name_accepts_wellformed(name):
    parsed = pm.parse_capture_name(name)
    assert parsed is not None
    assert parsed["capture_date"] == "2026-10-16"


@pytest.mark.parametrize("name", [
    "Plant01_20261345_095000",     # month 13
    "Plant01_20261016_240000",     # hour 24
    "Plant01_20261016_95600",      # 5-digit time
    "Plant01_20261016",            # no time
    "Plant01_20261016_095000_x",   # trailing junk
    "A1_20260229_000000",          # 2026 is not a leap year
])
def test_capture_name_rejects_malformed(name):
    assert pm.parse_capture_name(name) is None


def test_growth_adjustment_is_clamped(tmp_path):
    plants = [{"plant_id": i, "external_ref": str(i),
               "observations": [
                   {"observation_date": "2083-05-02", "age_days": 7,
                    "height_cm": 0.5, "growth_rate_cm_per_day": 0.1},
                   {"observation_date": "2083-05-10", "age_days": 15,
                    "height_cm": 2.0, "growth_rate_cm_per_day": 0.1}]}
              for i in range(1, 5)]
    base = ref.build_reference_baseline(plants, SPECIES)

    def est(rate):
        return ref.provisional_estimate(
            base, {"age_days": 15, "height_cm": 2.0,
                   "growth_rate_cm_per_day": rate,
                   "observation_date": "2083-05-10"})

    raw = est(0.1)["days_remaining"]
    assert est(0.1)["days_remaining"] == raw                       # equal -> 1.0x
    assert est(0.001)["days_remaining"] == raw * 2.0               # clamp max 2.0
    assert est(100.0)["days_remaining"] == raw * 0.5               # clamp min 0.5
    for bad in (0.0, -0.1, float("nan"), float("inf"), None):
        assert est(bad)["days_remaining"] == raw                   # skipped


# --------------------------------------------------------------------------- #
# Confirmed defects (strict xfail -> currently fails)
# --------------------------------------------------------------------------- #

@pytest.mark.xfail(strict=True, reason=(
    "AUD-01: module-level stray 'PY' line makes these three new CLIs "
    "un-importable (NameError); they only work via `python -m`."))
@pytest.mark.parametrize("module", [
    "plantreadiness.verify_calibration",
    "plantreadiness.reprocess_captures",
    "plantreadiness.migrate_species_rename",
])
def test_new_cli_modules_are_importable(module):
    importlib.import_module(module)


@pytest.mark.xfail(strict=True, reason=(
    "AUD-02: evaluate_predictions guards `transplant_occurred is False`; "
    "sqlite returns int 0, so a deferral (did NOT transplant) is counted as a "
    "real transplant and graded."))
def test_deferral_is_not_counted_as_transplant(tmp_path):
    repo, conn = _repo(tmp_path)
    pid = repo.register_plant(species=SPECIES, data_source="real",
                              external_ref="1")
    repo.record_transplant(plant_id=pid, transplant_date="2083-05-10",
                           transplant_occurred=False)
    repo.save_prediction_snapshot(
        plant_id=pid, species=SPECIES, variety=None,
        observation_date="2083-05-01",
        predicted_readiness_date="2083-05-12", days_remaining=11,
        method=config.METHOD_REFERENCE_BASELINE, available=True,
        prediction_scope="plant", data_source="real")
    result = svc.evaluate_predictions(repo, species=SPECIES)
    assert result["n_predictions_evaluated"] == 0


def test_uploaded_manual_data_is_baseline_usable(tmp_path):
    repo, conn = _repo(tmp_path)
    csv_text = "Date,Age_days,Plant_ID,Height_cm\n" + "\n".join(
        f"2083-05-0{d},{d},R{i},{0.5 * d}"
        for i in range(1, 5) for d in (2, 9))
    svc.run_csv_import(repo, csv_text, species=SPECIES, dry_run=False)
    rows = [dict(r) for r in conn.execute(
        "SELECT calibration_source, rod_length_cm FROM observations")]
    groups = calib.summarise_groups(rows)
    assert groups["quarantined_rows"] == 0
    assert groups["usable_rows"] == len(rows)


def test_manual_upload_is_all_or_nothing(tmp_path):
    repo, conn = _repo(tmp_path)
    csv_text = ("Date,Age_days,Plant_ID,Height_cm\n"
                "2083-05-02,7,R1,1.0\n"
                "bad-date,7,R2,2.0\n"          # invalid row
                "2083-05-03,8,R1,1.5\n")
    report = svc.run_csv_import(repo, csv_text, species=SPECIES,
                                dry_run=False)
    assert report["issues"]                      # there was an invalid row
    assert report["observations_inserted"] == 0  # ...so nothing may be written


@pytest.mark.xfail(strict=True, reason=(
    "AUD-05: predict_species_cohort aggregates the latest-date observations "
    "regardless of calibration provenance, so quarantined (camera_unverified) "
    "measurements drive the prediction and no exclusion/calibration info is "
    "reported."))
def test_species_prediction_excludes_unverified_camera(tmp_path):
    repo, conn = _repo(tmp_path)
    _reference_cohort(repo)
    svc.build_reference_baseline(repo, SPECIES, models_dir=tmp_path / "models")
    for i in range(1, 6):  # 5 live plants, only unverified camera data
        p = repo.register_plant(species=SPECIES, data_source="real",
                                external_ref=f"L{i}")
        repo.add_observation(plant_id=p, observation_date="2083-05-10",
                             age_days=15, height_cm=100.0,
                             growth_rate_cm_per_day=0.1,
                             calibration_source=CAMERA, rod_length_cm=10.0,
                             calibration_verified=False)
    result = svc.predict_species_cohort(
        repo, SPECIES, models_dir=tmp_path / "models",
        allow_provisional_baseline=True)
    snapshot = result.get("cohort_snapshot", {})
    # The representative height must not be dominated by quarantined rows...
    assert snapshot.get("median_height_cm") == 2.0
    # ...and the result must disclose the calibration/exclusion it applied.
    assert any("calib" in k.lower() or "exclu" in k.lower()
               for k in result)


@pytest.mark.xfail(strict=True, reason=(
    "AUD-06: db validation only rejects height_cm < 0, so +inf is accepted and "
    "stored as a real measurement."))
def test_add_observation_rejects_non_finite_height(tmp_path):
    repo, conn = _repo(tmp_path)
    pid = repo.register_plant(species=SPECIES, data_source="real",
                              external_ref="1")
    with pytest.raises(dbm.ValidationError):
        repo.add_observation(plant_id=pid, observation_date="2083-05-02",
                             age_days=7, height_cm=float("inf"))


@pytest.mark.xfail(strict=True, reason=(
    "AUD-07: add_capture stores a 'valid' capture with +inf height and lets the "
    "same public_id be reassigned to a different plant."))
def test_capture_integrity(tmp_path):
    repo, conn = _repo(tmp_path)
    p1 = repo.register_plant(species=SPECIES, data_source="real",
                             external_ref="1")
    p2 = repo.register_plant(species=SPECIES, data_source="real",
                             external_ref="2")
    with pytest.raises(dbm.ValidationError):
        repo.add_capture(plant_id=p1, public_id="Plant01_20830510_100000",
                         observation_date="2083-05-10", validity="valid",
                         rod_length_cm=10.0, height_cm=float("inf"),
                         confidence=0.9)
    repo.add_capture(plant_id=p1, public_id="Plant01_20830510_100000",
                     observation_date="2083-05-10", validity="valid",
                     rod_length_cm=10.0, height_cm=2.0, confidence=0.9)
    repo.add_capture(plant_id=p2, public_id="Plant01_20830510_100000",
                     observation_date="2083-05-10", validity="valid",
                     rod_length_cm=10.0, height_cm=2.0, confidence=0.9)
    plant_id = conn.execute(
        "SELECT plant_id FROM camera_captures "
        "WHERE public_id='Plant01_20830510_100000'").fetchone()[0]
    assert plant_id == p1  # a capture must not be moved between plants


@pytest.mark.xfail(strict=True, reason=(
    "AUD-08: upsert_daily_row is a read-modify-write over the CSV with no "
    "locking, so concurrent writers lose rows."))
def test_daily_csv_concurrent_writes_do_not_lose_rows(tmp_path, monkeypatch):
    csv_path = tmp_path / "daily.csv"
    orig_write = pm._write_daily_rows

    def slow_write(path, rows):
        time.sleep(0.1)          # widen the read -> write window
        return orig_write(path, rows)

    monkeypatch.setattr(pm, "_write_daily_rows", slow_write)

    def record(pid):
        return [pm.CaptureRecord(public_id=f"{pid}_x",
                                 capture_date="2083-05-10",
                                 capture_timestamp="2083-05-10T10:00:00",
                                 validity="valid", height_cm=10.0)]

    def worker(pid):
        pm.write_daily_record("d", pid, [], csv_file=csv_path,
                              capture_date="2083-05-10", records=record(pid))

    threads = [threading.Thread(target=worker, args=(f"Plant-{i:02d}",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows, _ = pm._read_daily_rows(csv_path)
    assert len(rows) == 8


@pytest.mark.xfail(strict=True, reason=(
    "AUD-09: init_db's 'one-time' provenance UPDATE runs on every call and "
    "silently reclassifies an honest 'unknown' row (no rod length) as "
    "'csv_import_no_calibration', which is baseline-usable."))
def test_init_db_does_not_reclassify_unknown(tmp_path):
    dbfile = tmp_path / "x.db"
    conn = dbm.init_db(dbfile)
    repo = SQLiteRepository(conn)
    pid = repo.register_plant(species=SPECIES, data_source="real",
                              external_ref="1")
    repo.add_observation(plant_id=pid, observation_date="2083-05-02",
                         age_days=0, height_cm=1.0,
                         calibration_source=config.CALIBRATION_SOURCE_UNKNOWN)
    conn.close()

    dbm.init_db(dbfile)  # re-run the migration
    conn = dbm.connect(dbfile)
    source = conn.execute(
        "SELECT calibration_source FROM observations").fetchone()[0]
    conn.close()
    assert source == config.CALIBRATION_SOURCE_UNKNOWN
