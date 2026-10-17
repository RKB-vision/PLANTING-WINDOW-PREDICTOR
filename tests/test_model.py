"""Tests: model validation (leakage, MAE), retraining gate, CSV import."""

import tempfile
import unittest
from pathlib import Path

from plantreadiness import csv_import
from plantreadiness import db as dbm
from plantreadiness import model as mdl
from plantreadiness import synthetic

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "super_orange_growth_data.csv"


def _synthetic_frame(n_plants=12, seed=0):
    """Small deterministic labelled dataset for training tests."""
    import random
    rng = random.Random(seed)
    rows = []
    for pid in range(1, n_plants + 1):
        ready_age = rng.randint(26, 34)
        start = 8
        for age in range(start, ready_age + 1, 3):
            rows.append({
                "plant_id": pid,
                "species": "Super Orange",
                "variety": "Early" if pid % 2 else "Late",
                "age_days": age,
                "height_cm": 0.3 + 0.25 * age + rng.gauss(0, 0.1),
                "growth_rate_cm_per_day": 0.25 + rng.gauss(0, 0.05),
                "stem_width_cm": None,
                "visible_true_leaf_count": None,
                "days_remaining": ready_age - age,
            })
    return mdl.build_training_frame(rows)


class TestGroupedValidation(unittest.TestCase):
    """No plant may appear in both training and test folds."""

    def test_group_kfold_never_splits_a_plant(self):
        import numpy as np
        from sklearn.model_selection import GroupKFold
        df = _synthetic_frame()
        groups = df["plant_id"].values
        gkf = GroupKFold(n_splits=4)
        for train_idx, test_idx in gkf.split(df, df["target_days_remaining"],
                                             groups):
            train_plants = set(groups[train_idx])
            test_plants = set(groups[test_idx])
            self.assertEqual(train_plants & test_plants, set())

    def test_training_metadata_lists_disjoint_plants_and_metrics(self):
        df = _synthetic_frame()
        artifact = mdl.train_countdown_model(df, data_source="synthetic",
                                             source_label="unit test")
        meta = artifact["metadata"]
        self.assertIn("mae_days", meta["validation_metrics"])
        self.assertGreater(meta["validation_metrics"]["mae_days"], 0)
        self.assertEqual(meta["n_independent_plants"], 12)
        self.assertEqual(meta["n_labelled_readiness_events"], 12)
        self.assertTrue(meta["validated"])
        self.assertEqual(meta["target_definition"], config_target())
        self.assertIn("GroupKFold", meta["validation_method"])
        for key in ("training_data_source", "supported_species",
                    "supported_varieties", "feature_list", "training_date"):
            self.assertIn(key, meta)

    def test_prediction_returns_non_negative_days(self):
        df = _synthetic_frame()
        artifact = mdl.train_countdown_model(df, data_source="synthetic",
                                             source_label="unit test")
        sel = {"level": 3, "data_source": "synthetic", "entry": {},
               "artifact": artifact, "message": "test"}
        res = mdl.predict_days_remaining(sel, {
            "age_days": 20, "height_cm": 5.0, "growth_rate_cm_per_day": 0.25,
            "stem_width_cm": None, "visible_true_leaf_count": None,
            "species": "Super Orange", "variety": "Early"})
        self.assertTrue(res["available"])
        self.assertGreaterEqual(res["days_remaining"], 0)
        self.assertTrue(res["synthetic"])


def config_target():
    from plantreadiness import config
    return config.TARGET_DEFINITION


class TestRetrainGate(unittest.TestCase):
    """Candidate must beat the deployed model on the same holdout."""

    def _artifact(self, validated=True):
        return {"model": None,
                "metadata": {"validated": validated,
                             "training_data_source": "synthetic"}}

    def test_replace_when_candidate_better(self):
        d = mdl.decide_update(self._artifact(), self._artifact(),
                              current_holdout_mae=4.0,
                              candidate_holdout_mae=3.0)
        self.assertTrue(d["replace"])

    def test_keep_when_candidate_not_better(self):
        d = mdl.decide_update(self._artifact(), self._artifact(),
                              current_holdout_mae=3.0,
                              candidate_holdout_mae=4.0)
        self.assertFalse(d["replace"])

    def test_keep_when_equal(self):
        d = mdl.decide_update(self._artifact(), self._artifact(),
                              current_holdout_mae=3.0,
                              candidate_holdout_mae=3.0)
        self.assertFalse(d["replace"])

    def test_keep_when_holdout_missing(self):
        d = mdl.decide_update(self._artifact(), self._artifact(),
                              None, 2.0)
        self.assertFalse(d["replace"])

    def test_keep_when_candidate_unvalidated(self):
        d = mdl.decide_update(self._artifact(validated=True),
                              self._artifact(validated=False),
                              current_holdout_mae=5.0,
                              candidate_holdout_mae=1.0)
        self.assertFalse(d["replace"])

    def test_first_model_always_promoted(self):
        d = mdl.decide_update(None, self._artifact(), None, None)
        self.assertTrue(d["replace"])


class TestCsvImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbm.init_db(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_import_preserves_measurements(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        report = csv_import.import_csv(self.conn, CSV_PATH,
                                       species="Sunflower (SuperOrange)")
        self.assertEqual(report["observations_inserted"], 250)
        self.assertEqual(report["plants_imported"], 10)
        self.assertEqual(report["issues"], [])
        # spot-check a known row: plant 1, 2026-09-02, height 0.5, age 8
        row = self.conn.execute(
            """SELECT o.* FROM observations o
               JOIN plants p ON p.plant_id = o.plant_id
               WHERE p.external_ref = '1'
                 AND o.observation_date = '2026-09-02'""").fetchone()
        self.assertEqual(row["height_cm"], 0.5)
        self.assertEqual(row["age_days"], 8)
        self.assertIsNone(row["growth_rate_cm_per_day"])
        # all rows are real
        n_syn = self.conn.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE data_source='synthetic'").fetchone()[0]
        self.assertEqual(n_syn, 0)

    def test_reimport_is_idempotent_no_duplicates(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        r1 = csv_import.import_csv(self.conn, CSV_PATH)
        r2 = csv_import.import_csv(self.conn, CSV_PATH)
        self.assertEqual(r2["observations_inserted"], 0)
        self.assertEqual(r2["observations_skipped_duplicate"],
                         r1["observations_inserted"])
        total = self.conn.execute("SELECT COUNT(*) FROM observations") \
            .fetchone()[0]
        self.assertEqual(total, 250)

    def test_real_data_has_no_readiness_events(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        csv_import.import_csv(self.conn, CSV_PATH)
        counts = dbm.readiness_event_counts(self.conn, "Super Orange",
                                            data_source="real")
        self.assertEqual(counts["readiness_events"], 0)

    def test_analysis_runs_and_flags_plants_without_readiness(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        csv_import.import_csv(self.conn, CSV_PATH)
        analysis = csv_import.analyse_real_data(self.conn,
                                                species="Sunflower (SuperOrange)")
        self.assertEqual(analysis["n_plants"], 10)
        self.assertEqual(analysis["n_observations"], 250)
        self.assertEqual(analysis["n_plants_with_readiness_event"], 0)
        self.assertIsNotNone(
            analysis["overall_mean_growth_rate_cm_per_day"])
        for p in analysis["per_plant"]:
            self.assertIsNotNone(p["linear_slope_cm_per_day"])

    def test_bad_rows_reported_not_crashing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                         delete=False) as fh:
            fh.write("Date,Age_days,Plant_ID,Height_cm\n")
            fh.write("2083-05-02,8,1,0.5\n")
            fh.write("bad-date,9,1,0.6\n")      # unparsable date
            fh.write("2083-05-03,9,,0.7\n")     # missing plant id
            fh.write("2083-05-04,10,2,\n")      # missing height
            path = Path(fh.name)
        try:
            report = csv_import.import_csv(self.conn, path)
            self.assertEqual(report["observations_inserted"], 1)
            self.assertGreaterEqual(len(report["issues"]), 3)
        finally:
            path.unlink()


class TestSyntheticGenerator(unittest.TestCase):
    def test_reproducible_with_seed(self):
        cfg = synthetic.DemoConfig(seed=42)
        a = synthetic.generate_demo_cohort(cfg, label="initial")
        b = synthetic.generate_demo_cohort(synthetic.DemoConfig(seed=42),
                                           label="initial")
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):
            self.assertEqual(x.readiness_date, y.readiness_date)
            self.assertEqual(len(x.observations), len(y.observations))

    def test_different_seed_differs(self):
        a = synthetic.generate_demo_cohort(synthetic.DemoConfig(seed=1),
                                           label="initial")
        b = synthetic.generate_demo_cohort(synthetic.DemoConfig(seed=2),
                                           label="initial")
        self.assertNotEqual(
            [p.readiness_date for p in a],
            [p.readiness_date for p in b])

    def test_multiple_species_and_varieties_generated(self):
        plants = synthetic.generate_demo_cohort(synthetic.DemoConfig())
        combos = {(p.species, p.variety) for p in plants}
        self.assertGreaterEqual(len(combos), 4)   # >= 2 species x 2 varieties

    def test_every_demo_plant_has_readiness_and_feedback(self):
        plants = synthetic.generate_demo_cohort(synthetic.DemoConfig())
        for p in plants:
            self.assertIsNotNone(p.readiness_date)
            self.assertIsNotNone(p.feedback)
            self.assertIn("survived", p.feedback)

    def test_post_readiness_observations_exist_but_excluded_in_training(self):
        # generator records a few days AFTER readiness for monitoring;
        # readiness_training_rows must exclude them (verified via DB below).
        conn = dbm.init_db(":memory:")
        plants = synthetic.generate_demo_cohort(
            synthetic.DemoConfig(plants_per_species_variety=3))
        synthetic.load_cohort_into_db(conn, plants)
        rows = dbm.readiness_training_rows(conn, data_source="synthetic")
        self.assertTrue(all(r["days_remaining"] >= 0 for r in rows))
        obs_after = conn.execute(
            """SELECT COUNT(*) FROM observations o
               JOIN readiness_events r ON r.plant_id = o.plant_id
               WHERE o.observation_date > r.readiness_observed_date"""
        ).fetchone()[0]
        self.assertGreater(obs_after, 0)          # they exist in storage...
        train_dates = {(r["plant_id"], r["observation_date"])
                       for r in rows}
        self.assertTrue(all(
            r["observation_date"] <= r["readiness_observed_date"]
            for r in rows))                        # ...but never in training


if __name__ == "__main__":
    unittest.main()
