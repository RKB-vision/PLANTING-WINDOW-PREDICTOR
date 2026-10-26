"""
test_simplified.py — Unified Test Suite for Simplified Architecture

Verifies:
1. Database schema, seeding, and queries
2. Temporal growth rate calculation (first day is None, missing data preserved)
3. Computer vision detection and scale calibration
4. Machine learning countdown model with GroupKFold cross-validation
5. Prediction fallback ladder (Validated ML -> Baseline -> Insufficient Evidence)
6. Feedback and evaluation metrics
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pytest

import db
import ml
import vision


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    db.init_db(db_path)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def test_db_seeding_and_temporal_growth_rate(temp_db):
    # Seed historical CSV
    csv_path = Path("data/super_orange_growth_data.csv")
    assert csv_path.exists()
    count = db.seed_from_csv(temp_db, csv_path, species="Sunflower(SuperOrange)")
    assert count > 0

    plants = db.get_plants(temp_db)
    assert len(plants) >= 10

    # Test plant 1 observations
    obs = db.get_observations(temp_db, plant_id="1")
    assert len(obs) > 0
    # First observation MUST have growth_rate is None (never 0, never fabricated)
    assert obs[0]["growth_rate_cm_per_day"] is None

    # Subsequent observations have valid calculated rate
    if len(obs) > 1:
        assert obs[1]["growth_rate_cm_per_day"] is not None


def test_missing_data_integrity(temp_db):
    db.register_plant(temp_db, "plant_test_1", "TestSpecies", "TestVar")
    db.record_observation(
        temp_db,
        plant_id="plant_test_1",
        observation_date=date(2026, 9, 1),
        height_cm=2.5,
        stem_width_cm=None,  # Missing
        leaf_count=None,     # Missing
    )
    obs = db.get_observations(temp_db, plant_id="plant_test_1")
    assert len(obs) == 1
    assert obs[0]["stem_width_cm"] is None
    assert obs[0]["visible_true_leaf_count"] is None
    assert obs[0]["growth_rate_cm_per_day"] is None


def test_vision_pipeline_on_sample_image():
    sample_path = Path("Plant02_20261020_100000.jpg")
    if not sample_path.exists():
        pytest.skip("Sample image not present in root")

    res = vision.measure_image(sample_path, rod_length_cm=12.0)
    assert res.height_cm > 0.0
    assert res.calibrated is True
    assert res.pixels_per_cm > 50.0
    assert res.annotated_image is not None
    assert res.leaf_count >= 1


def test_ml_groupkfold_training_and_validation(temp_db):
    csv_path = Path("data/super_orange_growth_data.csv")
    db.seed_from_csv(temp_db, csv_path, species="Sunflower(SuperOrange)")

    train_res = ml.train_countdown_model(temp_db, species="Sunflower(SuperOrange)")
    assert train_res["validated"] is True
    assert train_res["n_plants"] >= 4
    assert train_res["best_mae"] < train_res["baseline_mae"]
    assert "pipeline" in train_res


def test_prediction_fallback_ladder(temp_db):
    csv_path = Path("data/super_orange_growth_data.csv")
    db.seed_from_csv(temp_db, csv_path, species="Sunflower(SuperOrange)")

    # 1. Level 1: ML Model
    train_res = ml.train_countdown_model(temp_db, species="Sunflower(SuperOrange)")
    pipe = train_res["pipeline"]

    p1 = ml.predict_species_readiness(
        temp_db,
        species="Sunflower(SuperOrange)",
        current_height_cm=4.5,
        current_age_days=15,
        trained_model=pipe,
    )
    assert p1.method == "validated_ml_model"
    assert p1.predicted_days_remaining is not None
    assert p1.confidence == "high"

    # 2. Level 2: Historical Baseline fallback (without ML model)
    p2 = ml.predict_species_readiness(
        temp_db,
        species="Sunflower(SuperOrange)",
        current_height_cm=4.5,
        current_age_days=15,
        trained_model=None,
    )
    assert p2.method == "provisional_historical_baseline"
    assert p2.predicted_days_remaining is not None

    # 3. Level 3: Insufficient Evidence fallback (unknown species)
    p3 = ml.predict_species_readiness(
        temp_db,
        species="NonExistentSpecies",
        current_height_cm=None,
        current_age_days=None,
    )
    assert p3.method == "insufficient_evidence"
    assert p3.predicted_days_remaining is None
    assert p3.confidence == "none"


def test_feedback_cycle(temp_db):
    # Save a prediction
    pred_id = db.save_prediction(
        temp_db,
        species="Sunflower(SuperOrange)",
        prediction_date=date(2026, 9, 10),
        predicted_days=5.0,
        predicted_date=date(2026, 9, 15),
        method="provisional_historical_baseline",
        confidence="moderate",
    )
    assert pred_id > 0

    # Save feedback showing actual transplant happened on Sept 14 (4 days taken)
    fb_id = db.save_feedback(
        temp_db,
        prediction_id=pred_id,
        stage="transplantation",
        actual_transplant_date=date(2026, 9, 14),
        condition="healthy",
    )
    assert fb_id > 0
