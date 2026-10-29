"""Stable readiness dates, age provenance and suspect-reading handling.

The behaviours pinned here:

* while a plant keeps growing, a provisional date never moves later;
* when height genuinely stalls, a later date is allowed;
* a known age (planting date or user-supplied nursery age) anchors the date;
* a reading >50% away from the previous accepted height is kept but excluded;
* the CLI can register a seedling's current age without inventing a sowing date.
"""

from __future__ import annotations

import contextlib
import io
from datetime import date, timedelta

import pytest

import plant_feedback
from plantreadiness import config
from plantreadiness import db as dbm
from plantreadiness import services as svc
from plantreadiness.db import ValidationError
from plantreadiness.repository import create_sqlite_repository

SPECIES = "Test Plant"


@pytest.fixture()
def cohort(tmp_path):
    """A reference cohort whose heights grow 0.45 cm/day to ~19.5 cm."""
    repo = create_sqlite_repository(tmp_path / "stability.db")
    models = tmp_path / "models"
    for i in range(4):
        plant_id = repo.register_plant(
            species=SPECIES, cohort="reference", external_ref=f"R{i}",
            date_started="2026-08-01")
        for age in range(1, 41):
            day = (date(2026, 8, 1) + timedelta(days=age - 1)).isoformat()
            repo.upsert_daily_observation(
                plant_id=plant_id, observation_date=day, age_days=age,
                height_cm=1.5 + 0.45 * (age - 1),
                calibration_source=config.CALIBRATION_SOURCE_CSV)
    svc.build_reference_baseline(repo, SPECIES, models_dir=models)
    try:
        yield repo, models
    finally:
        repo.close()


def _predict(repo, models, day):
    return svc.predict_species_cohort(
        repo, SPECIES, None, models, observation_date=day,
        allow_provisional_baseline=True)


def _live_plant(repo, observations, **plant_fields):
    plant_id = repo.register_plant(
        species=SPECIES, external_ref="LIVE", **plant_fields)
    for day, height in observations:
        repo.upsert_daily_observation(
            plant_id=plant_id, observation_date=day, height_cm=height,
            calibration_source=config.CALIBRATION_SOURCE_CAMERA)
    return plant_id


# --------------------------------------------------------------------------- #
# Date stability
# --------------------------------------------------------------------------- #


def test_growing_plant_never_moves_the_date_later(cohort):
    repo, models = cohort
    _live_plant(repo, [
        ("2026-10-01", 4.0),
        ("2026-10-08", 4.7),
        ("2026-10-15", 5.4),
        ("2026-10-22", 6.1),
    ])

    first = _predict(repo, models, "2026-10-15")
    second = _predict(repo, models, "2026-10-22")

    assert first["available"] and second["available"]
    assert first["estimate_basis"] == "growth_trajectory"
    assert second["estimate_basis"] == "growth_trajectory"
    assert second["plant_grew_since_previous"] is True
    assert (second["predicted_readiness_date"]
            <= first["predicted_readiness_date"])
    assert second["date_stability"] == "held_while_growing"


def test_stalled_plant_may_move_the_date_later(cohort):
    repo, models = cohort
    _live_plant(repo, [
        ("2026-10-01", 4.0),
        ("2026-10-08", 4.7),
        ("2026-10-15", 5.4),
        ("2026-10-22", 5.4),
    ])

    before = _predict(repo, models, "2026-10-15")
    stalled = _predict(repo, models, "2026-10-22")

    assert before["available"] and stalled["available"]
    assert stalled["plant_grew_since_previous"] is False
    assert stalled["predicted_readiness_date"] > before["predicted_readiness_date"]
    assert stalled["date_stability"] == "later_after_stall"


def test_known_initial_age_keeps_the_date_stable(cohort):
    repo, models = cohort
    _live_plant(
        repo,
        [("2026-10-01", 5.55), ("2026-10-08", 8.0), ("2026-10-15", 11.0)],
        initial_age_days=10, initial_age_measured_on="2026-10-01")

    results = [_predict(repo, models, day)
               for day in ("2026-10-01", "2026-10-08", "2026-10-15")]

    assert all(r["available"] for r in results)
    assert {r["age_source"] for r in results} == {
        "user_estimated_initial_age"}
    assert len({r["predicted_readiness_date"] for r in results}) == 1
    assert all(r["estimate_basis"] == "historical_timing" for r in results)


# --------------------------------------------------------------------------- #
# Honest labels / data guards
# --------------------------------------------------------------------------- #


def test_no_age_is_labelled_as_a_fallback(cohort):
    repo, models = cohort
    _live_plant(repo, [
        ("2026-10-01", 4.0),
        ("2026-10-08", 4.7),
        ("2026-10-15", 5.4),
    ])

    result = _predict(repo, models, "2026-10-15")

    assert result["available"] is True
    assert result["provisional"] is True
    assert result["method"] == config.METHOD_REFERENCE_BASELINE
    assert result["age_source"] == "unknown"
    assert "Provisional" in result["message"]


def test_height_jump_is_excluded_but_still_stored(cohort):
    repo, models = cohort
    plant_id = _live_plant(repo, [
        ("2026-10-01", 4.0),
        ("2026-10-08", 4.7),
        ("2026-10-09", 1.0),   # >50% drop from the previous accepted height
        ("2026-10-15", 5.4),
    ])

    suspect_day = _predict(repo, models, "2026-10-09")
    assert suspect_day["available"] is False
    assert suspect_day["suspect_rows_excluded"] == 1
    assert "suspect" in suspect_day["message"].lower()

    healthy_day = _predict(repo, models, "2026-10-15")
    assert healthy_day["available"] is True
    assert healthy_day["estimate_basis"] == "growth_trajectory"
    assert healthy_day["n_history_readings"] == 3
    assert healthy_day["suspect_rows_excluded"] == 0

    # The suspect row is flagged out of the trend, never deleted.
    stored = {o["observation_date"]: o for o in repo.get_observations(plant_id)}
    assert stored["2026-10-09"]["height_cm"] == 1.0


def test_initial_age_is_not_a_fabricated_planting_date(cohort):
    repo, _ = cohort
    plant_id = repo.register_plant(
        species=SPECIES, external_ref="NURSERY",
        initial_age_days=21, initial_age_measured_on="2026-10-29")

    plant = repo.get_plant(plant_id)
    assert plant["date_started"] is None
    assert plant["initial_age_days"] == 21
    assert dbm.age_days_for_plant(plant, "2026-11-05") == 28
    assert dbm.age_source_for_plant(plant) == "user_estimated_initial_age"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_register_cli_accepts_current_age(tmp_path):
    conn = dbm.init_db(tmp_path / "cli.db")
    try:
        args = plant_feedback.build_parser().parse_args([
            "register", "--species", "Rose (Red)", "--ref", "Plant0x",
            "--age-days", "21", "--as-of", "2026-10-29",
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            assert plant_feedback.cmd_register(conn, args) == 0

        plant = dbm.list_plants(conn)[0]
        assert plant["date_started"] is None
        assert plant["initial_age_days"] == 21
        assert plant["initial_age_measured_on"] == "2026-10-29"
        assert (dbm.age_days_for_plant(dict(plant), "2026-11-05")
                == 28)
    finally:
        conn.close()


def test_register_rejects_ambiguous_age_sources(tmp_path):
    conn = dbm.init_db(tmp_path / "cli.db")
    try:
        with pytest.raises(ValidationError):
            dbm.register_plant(
                conn, species="Rose (Red)", date_started="2026-09-01",
                initial_age_days=21)
    finally:
        conn.close()


def test_register_cli_rejects_as_of_without_age(tmp_path):
    conn = dbm.init_db(tmp_path / "cli.db")
    try:
        args = plant_feedback.build_parser().parse_args([
            "register", "--species", "Rose (Red)", "--as-of", "2026-10-29",
        ])
        with contextlib.redirect_stderr(io.StringIO()):
            assert plant_feedback.cmd_register(conn, args) == 2
    finally:
        conn.close()


def test_camera_auto_registration_accepts_age(tmp_path):
    from plantreadiness import camera_import

    repo = create_sqlite_repository(tmp_path / "camera.db")
    try:
        report = camera_import.import_camera_measurements(
            repo,
            io.StringIO(
                "plant_id,average_height_cm,first_capture_timestamp\n"
                "Nursery01,4.2,2026-10-29T10:00:00\n"),
            default_species=SPECIES, auto_register=True,
            initial_age_days=21, initial_age_measured_on="2026-10-29")
        assert report["plants_registered"] == 1
        plant = repo.list_plants()[0]
        assert plant["initial_age_days"] == 21
        assert plant["initial_age_measured_on"] == "2026-10-29"
        obs = repo.get_observations(plant["plant_id"])[0]
        assert obs["age_days"] == 21
    finally:
        repo.close()


def test_camera_auto_registration_without_age_does_not_crash(tmp_path):
    from plantreadiness import camera_import

    repo = create_sqlite_repository(tmp_path / "camera.db")
    try:
        report = camera_import.import_camera_measurements(
            repo,
            io.StringIO(
                "plant_id,average_height_cm,first_capture_timestamp\n"
                "Nursery02,4.2,2026-10-29T10:00:00\n"),
            default_species=SPECIES, auto_register=True)
        assert report["plants_registered"] == 1
        plant = repo.list_plants()[0]
        assert plant["initial_age_days"] is None
        assert repo.get_observations(plant["plant_id"])[0]["age_days"] is None
    finally:
        repo.close()
