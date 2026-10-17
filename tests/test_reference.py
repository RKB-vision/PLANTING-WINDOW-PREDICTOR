"""Tests: successful-cohort reference baseline + provisional estimates.

Covers the task's explicit edge cases:

* per-plant growth with missing / irregular / duplicate observations,
* medians across independent plants with contributing-plant counts,
* the transplant reference (recorded date wins over the assumed next-day),
* provisional estimate gating (too few plants / no comparable group),
* reproducibility of the persisted baseline.
"""

import tempfile
import unittest
from pathlib import Path

from plantreadiness import config
from plantreadiness import reference as ref
from plantreadiness import services as svc
from plantreadiness.repository import create_sqlite_repository


class TestPlantTrajectory(unittest.TestCase):
    def _points(self, obs):
        return ref.compute_plant_trajectory(obs)

    def test_first_measurement_has_no_growth_rate(self):
        out = self._points([
            {"observation_date": "2083-05-02", "age_days": 7, "height_cm": 0.5},
        ])
        self.assertIsNone(out["points"][0]["growth_rate_cm_per_day"])
        self.assertIn("first measurement", out["points"][0]["growth_rate_reason"])

    def test_growth_uses_calendar_elapsed_days(self):
        # irregular interval: 10 calendar days apart
        out = self._points([
            {"observation_date": "2083-05-02", "age_days": 7, "height_cm": 0.5},
            {"observation_date": "2083-05-12", "age_days": 11, "height_cm": 1.5},
        ])
        second = out["points"][1]
        self.assertEqual(second["elapsed_days"], 10)
        self.assertAlmostEqual(second["growth_rate_cm_per_day"], 0.1, places=4)
        self.assertTrue(second["growth_rate_derived"])

    def test_missing_height_does_not_invent_growth(self):
        out = self._points([
            {"observation_date": "2083-05-02", "age_days": 7, "height_cm": 0.5},
            {"observation_date": "2083-05-03", "age_days": 8, "height_cm": None},
        ])
        self.assertIsNone(out["points"][1]["growth_rate_cm_per_day"])
        self.assertFalse(out["points"][1]["growth_rate_derived"])

    def test_duplicate_dates_are_reported_not_double_counted(self):
        out = self._points([
            {"observation_date": "2083-05-02", "age_days": 7, "height_cm": 0.5},
            {"observation_date": "2083-05-02", "age_days": 7, "height_cm": 9.9},
        ])
        self.assertEqual(len(out["points"]), 1)
        self.assertTrue(any("duplicate" in i["problem"] for i in out["issues"]))

    def test_missing_date_reported(self):
        out = self._points([{"observation_date": None, "height_cm": 1.0}])
        self.assertEqual(out["points"], [])
        self.assertTrue(out["issues"])

    def test_measured_rate_is_kept(self):
        out = self._points([
            {"observation_date": "2083-05-02", "age_days": 7, "height_cm": 0.5},
            {"observation_date": "2083-05-04", "age_days": 9, "height_cm": 0.9,
             "growth_rate_cm_per_day": 0.42},
        ])
        self.assertEqual(out["points"][1]["growth_rate_cm_per_day"], 0.42)
        self.assertFalse(out["points"][1]["growth_rate_derived"])


class TestTransplantReference(unittest.TestCase):
    POINTS = [{"observation_date": "2083-05-10", "age_days": 15,
               "height_cm": 2.0}]

    def test_recorded_date_wins(self):
        refr = ref.transplant_reference(self.POINTS, "2083-05-12")
        self.assertEqual(refr["transplant_date"], "2083-05-12")
        self.assertFalse(refr["is_assumption"])
        self.assertEqual(refr["source"], "recorded")

    def test_assumed_next_day_is_flagged(self):
        refr = ref.transplant_reference(self.POINTS, None)
        self.assertEqual(refr["transplant_date"], "2083-05-11")
        self.assertTrue(refr["is_assumption"])
        self.assertEqual(refr["source"], "assumed_day_after_last_observation")

    def test_invalid_recorded_date_falls_back_to_assumption(self):
        refr = ref.transplant_reference(self.POINTS, "not-a-date")
        self.assertTrue(refr["is_assumption"])


class TestBaseline(unittest.TestCase):
    def _plant(self, pid, last_date="2083-05-10", ref=None):
        return {
            "plant_id": pid,
            "external_ref": ref or str(pid),
            "recorded_transplant_date": None,
            "observations": [
                {"observation_date": "2083-05-02", "age_days": 7,
                 "height_cm": 0.5 + 0.1 * pid},
                {"observation_date": last_date, "age_days": 15,
                 "height_cm": 2.0 + 0.1 * pid},
            ],
        }

    def test_medians_and_contributing_plant_counts(self):
        plants = [self._plant(i) for i in range(1, 5)]
        base = ref.build_reference_baseline(plants, "Super Orange")
        self.assertEqual(base["n_plants"], 4)
        self.assertEqual(base["n_plants_contributing_to_medians"], 4)
        first = base["by_age_days"][0]
        self.assertEqual(first["age_days"], 7)
        self.assertEqual(first["n_plants"], 4)
        self.check_median_used(base, 7)

    def check_median_used(self, base, age):
        group = next(g for g in base["by_age_days"] if g["age_days"] == age)
        # heights 0.6, 0.7, 0.8, 0.9 -> median 0.75
        self.assertAlmostEqual(group["median_height_cm"], 0.75, places=4)

    def test_all_medians_carry_independent_plant_counts(self):
        plants = [self._plant(i) for i in range(1, 6)]
        base = ref.build_reference_baseline(plants, "Super Orange")
        for group in base["by_age_days"] + base["by_days_before_transplant"]:
            self.assertGreaterEqual(group["n_plants"], 1)
            self.assertLessEqual(group["n_plants"], 5)

    def test_recorded_transplants_are_used_when_present(self):
        plants = [self._plant(i) for i in range(1, 5)]
        plants[0]["recorded_transplant_date"] = "2083-05-20"
        base = ref.build_reference_baseline(plants, "Super Orange")
        self.assertEqual(base["transplant_reference"]["n_recorded"], 1)
        self.assertEqual(base["transplant_reference"]["n_assumed"], 3)

    def test_unsuccessful_plant_without_observations_is_skipped(self):
        plants = [self._plant(i) for i in range(1, 5)]
        plants.append({"plant_id": 99, "external_ref": "99",
                       "recorded_transplant_date": None, "observations": []})
        base = ref.build_reference_baseline(plants, "Super Orange")
        self.assertEqual(base["n_plants"], 4)


class TestProvisionalEstimate(unittest.TestCase):
    def _baseline(self, n_plants=4):
        plants = [{
            "plant_id": i,
            "external_ref": str(i),
            "recorded_transplant_date": None,
            "observations": [
                {"observation_date": "2083-05-02", "age_days": 7,
                 "height_cm": 0.5},
                {"observation_date": "2083-05-10", "age_days": 15,
                 "height_cm": 2.0},
            ],
        } for i in range(1, n_plants + 1)]
        return ref.build_reference_baseline(plants, "Super Orange")

    def test_estimate_available_with_enough_plants(self):
        est = ref.provisional_estimate(
            self._baseline(4),
            {"age_days": 15, "height_cm": 2.0,
             "observation_date": "2083-05-10"})
        self.assertTrue(est["available"])
        self.assertTrue(est["provisional"])
        self.assertEqual(est["method"], config.METHOD_REFERENCE_BASELINE)
        self.assertEqual(est["predicted_readiness_date"], "2083-05-11")
        self.assertGreaterEqual(est["n_independent_plants"], 3)

    def test_estimate_refused_with_too_few_plants(self):
        base = self._baseline(2)
        est = ref.provisional_estimate(
            base, {"age_days": 15, "height_cm": 2.0,
                   "observation_date": "2083-05-10"})
        self.assertFalse(est["available"])
        self.assertIn("independent plants", est["message"])

    def test_estimate_refused_without_comparable_group(self):
        est = ref.provisional_estimate(
            self._baseline(4),
            {"age_days": 400, "height_cm": 99.0,
             "observation_date": "2083-05-10"})
        self.assertFalse(est["available"])

    def test_missing_baseline_refused(self):
        est = ref.provisional_estimate(None, {"age_days": 15})
        self.assertFalse(est["available"])

    def test_single_measurement_flags_insufficient_growth_history(self):
        est = ref.provisional_estimate(
            self._baseline(4),
            {"age_days": 15, "height_cm": 2.0,
             "observation_date": "2083-05-10"})
        self.assertTrue(est["insufficient_growth_history"])
        self.assertIn("growth-rate", est["history_note"])

    def test_growth_history_present_is_not_flagged(self):
        est = ref.provisional_estimate(
            self._baseline(4),
            {"age_days": 15, "height_cm": 2.0,
             "growth_rate_cm_per_day": 0.1,
             "observation_date": "2083-05-10"})
        self.assertFalse(est["insufficient_growth_history"])


class TestPersistenceAndReproducibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models = Path(self.tmp.name) / "models"
        self.repo = create_sqlite_repository(Path(self.tmp.name) / "ref.db")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _cohort(self):
        for i in range(1, 5):
            pid = self.repo.register_plant(
                species="Super Orange", data_source="real",
                external_ref=str(i))
            self.repo.add_observation(
                plant_id=pid, observation_date="2083-05-02", age_days=7,
                height_cm=0.5,
                calibration_source="csv_import_no_calibration")
            self.repo.add_observation(
                plant_id=pid, observation_date="2083-05-10", age_days=15,
                height_cm=2.0,
                calibration_source="csv_import_no_calibration")

    def test_build_save_load_roundtrip(self):
        self._cohort()
        first = svc.build_reference_baseline(
            self.repo, "Super Orange", models_dir=self.models)
        self.assertTrue(Path(first["path"]).exists())
        loaded = svc.load_reference_baseline(self.models, "Super Orange")
        self.assertEqual(loaded["n_plants"], 4)
        self.assertEqual(loaded["by_age_days"], first["by_age_days"])

    def test_rebuild_is_reproducible(self):
        self._cohort()
        first = svc.build_reference_baseline(
            self.repo, "Super Orange", models_dir=self.models)
        second = svc.build_reference_baseline(
            self.repo, "Super Orange", models_dir=self.models)
        self.assertEqual(first["by_age_days"], second["by_age_days"])
        self.assertEqual(first["n_plants"], second["n_plants"])

    def test_provisional_used_only_when_model_unavailable(self):
        self._cohort()
        svc.build_reference_baseline(
            self.repo, "Super Orange", models_dir=self.models)
        pid = self.repo.list_plants(species="Super Orange")[0]["plant_id"]

        default = svc.predict_for_plant(self.repo, pid, self.models)
        self.assertFalse(default["available"])
        self.assertEqual(default["message"], config.UNAVAILABLE_MESSAGE)

        provisional = svc.predict_for_plant(
            self.repo, pid, self.models, allow_provisional_baseline=True)
        self.assertTrue(provisional["available"])
        self.assertTrue(provisional["provisional"])
        self.assertEqual(provisional["method"],
                         config.METHOD_REFERENCE_BASELINE)
        self.assertFalse(provisional["synthetic"])
        self.assertTrue(provisional["limitations"])


if __name__ == "__main__":
    unittest.main()
