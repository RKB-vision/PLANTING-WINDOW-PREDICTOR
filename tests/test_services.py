"""Tests: service layer shared by the CLI and the web app.

Covers:

* auto-calculation of age / growth rate (derived only, never invented),
* CSV preview / validation / import (dry-run, idempotent re-run) and export,
* prediction fallback order incl. the exact honest-refusal message,
* species/model selection and minimum-evidence gating,
* real/synthetic separation in every summary.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from plantreadiness import config
from plantreadiness import db as dbm
from plantreadiness import model as mdl
from plantreadiness import services as svc
from plantreadiness.db import ValidationError
from plantreadiness.repository import NotFoundError, create_sqlite_repository


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models = Path(self.tmp.name) / "models"
        self.repo = create_sqlite_repository(
            Path(self.tmp.name) / "svc.db")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _plant(self, **kw):
        kw.setdefault("species", "Super Orange")
        kw.setdefault("variety", "Early")
        kw.setdefault("date_started", "2083-04-24")
        kw.setdefault("data_source", "real")
        return self.repo.register_plant(**kw)


class TestAutoCalculation(ServiceBase):
    def test_age_derived_from_start_date(self):
        self.assertEqual(svc.auto_age_days("2083-04-24", "2083-05-10"), 16)

    def test_age_none_without_start_date(self):
        self.assertIsNone(svc.auto_age_days(None, "2083-05-10"))

    def test_growth_rate_needs_both_heights(self):
        rate = svc.auto_growth_rate(3.5, "2083-05-10", None, "2083-05-26")
        self.assertIsNone(rate["value"])
        self.assertFalse(rate["derived"])
        rate = svc.auto_growth_rate(None, "2083-05-10", 8.0, "2083-05-26")
        self.assertIsNone(rate["value"])

    def test_growth_rate_computed_from_previous(self):
        rate = svc.auto_growth_rate(3.5, "2083-05-10", 8.0, "2083-05-26")
        self.assertTrue(rate["derived"])
        self.assertAlmostEqual(rate["value"], 4.5 / 16, places=4)

    def test_growth_rate_needs_positive_interval(self):
        rate = svc.auto_growth_rate(3.5, "2083-05-26", 8.0, "2083-05-10")
        self.assertIsNone(rate["value"])
        rate = svc.auto_growth_rate(3.5, "2083-05-10", 8.0, "2083-05-10")
        self.assertIsNone(rate["value"])

    def test_record_observation_derives_and_reports(self):
        pid = self._plant()
        r1 = svc.record_observation(self.repo, plant_id=pid,
                                    observation_date="2083-05-10",
                                    height_cm=3.5)
        self.assertEqual(r1["age_days"], 16)
        self.assertIn("age_days", r1["derived"])
        # first observation: no previous height -> rate NOT derived
        self.assertNotIn("growth_rate_cm_per_day", r1["derived"])
        self.assertIsNone(r1["growth_rate_cm_per_day"])

        r2 = svc.record_observation(self.repo, plant_id=pid,
                                    observation_date="2083-05-26",
                                    height_cm=8.0)
        self.assertIsNotNone(r2["growth_rate_cm_per_day"])
        self.assertIn("growth_rate_cm_per_day", r2["derived"])

    def test_manual_values_win_over_derivation(self):
        pid = self._plant()
        r = svc.record_observation(self.repo, plant_id=pid,
                                   observation_date="2083-05-10",
                                   height_cm=3.5, age_days=99,
                                   growth_rate_cm_per_day=0.5)
        self.assertEqual(r["age_days"], 99)
        self.assertEqual(r["growth_rate_cm_per_day"], 0.5)
        self.assertEqual(r["derived"], {})

    def test_no_start_date_leaves_age_empty(self):
        pid = self._plant(date_started=None)
        r = svc.record_observation(self.repo, plant_id=pid,
                                   observation_date="2083-05-10",
                                   height_cm=3.5)
        self.assertIsNone(r["age_days"])
        self.assertNotIn("age_days", r["derived"])

    def test_unknown_plant_raises(self):
        with self.assertRaises(NotFoundError):
            svc.record_observation(self.repo, plant_id=9999,
                                   observation_date="2083-05-10",
                                   height_cm=1)


class TestFeedbackService(ServiceBase):
    def test_state_machine(self):
        pid = self._plant()
        fb = svc.record_transplant(self.repo, plant_id=pid,
                                   transplant_date="2083-05-20",
                                   height_at_transplant_cm=6.4)
        self.assertEqual(fb["feedback_due_date"], "2083-06-03")
        cases = [
            (date(2083, 6, 1), "pending"),
            (date(2083, 6, 3), "due"),
            (date(2083, 6, 4), "overdue"),
        ]
        for as_of, expected in cases:
            state = svc.feedback_state(fb, as_of=as_of)
            self.assertEqual(state["status"], expected, as_of)
        done = svc.submit_feedback(self.repo, feedback_id=fb["feedback_id"],
                                   feedback_submitted_date="2083-06-03",
                                   survived_after_14d=True,
                                   height_after_14d_cm=7.5,
                                   visible_condition="healthy")
        self.assertEqual(svc.feedback_state(done)["status"], "completed")
        self.assertEqual(done["growth_after_14d_cm"], 1.1)


class TestCsvImportExport(ServiceBase):
    CSV = ("Date,Age_days,Plant_ID,Height_cm,Growth_rate_cm_per_day\n"
           "2083-05-10,16,P-1,3.5,0.22\n"
           "2083-05-11,17,P-1,3.8,0.3\n"
           "2083-05-10,16,P-2,4.1,\n")

    def test_preview_valid(self):
        prev = svc.preview_csv_import(self.CSV)
        self.assertTrue(prev["valid"])
        self.assertEqual(prev["rows_total"], 3)
        self.assertEqual(prev["plants_in_file"], ["P-1", "P-2"])
        self.assertEqual(prev["plants_new"], ["P-1", "P-2"])
        self.assertEqual(len(prev["issues"]), 0)

    def test_preview_flags_bad_rows(self):
        bad = ("Date,Age_days,Plant_ID,Height_cm\n"
               ",16,P-1,3.5\n"          # missing date
               "2083-05-10,, ,3.5\n"    # missing Plant_ID
               "2083-05-10,16,P-1,\n"   # missing height
               "2083-05-10,16,P-2,abc\n")  # bad number
        prev = svc.preview_csv_import(bad)
        self.assertFalse(prev["valid"])
        self.assertEqual(len(prev["issues"]), 4)
        self.assertEqual(prev["rows_total"], 0)

    def test_preview_missing_columns_raises(self):
        with self.assertRaises(ValidationError):
            svc.preview_csv_import("Date,Plant_ID\n2083-05-10,P-1\n")

    def test_dry_run_writes_nothing(self):
        report = svc.run_csv_import(self.repo, self.CSV,
                                    species="Super Orange", dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["observations_inserted"], 0)
        self.assertEqual(self.repo.list_plants(), [])

    def test_apply_then_rerun_is_idempotent(self):
        first = svc.run_csv_import(self.repo, self.CSV,
                                   species="Super Orange", dry_run=False)
        self.assertEqual(first["observations_inserted"], 3)
        self.assertEqual(first["preview"]["plants_new"], ["P-1", "P-2"])
        plants = self.repo.list_plants()
        self.assertEqual(len(plants), 2)
        self.assertTrue(all(p["data_source"] == "real" for p in plants))

        second = svc.run_csv_import(self.repo, self.CSV,
                                    species="Super Orange", dry_run=False)
        self.assertEqual(second["observations_inserted"], 0)
        self.assertEqual(second["observations_skipped_duplicate"], 3)
        self.assertEqual(len(self.repo.list_plants()), 2)
        obs = sum(len(self.repo.get_observations(p["plant_id"]))
                  for p in plants)
        self.assertEqual(obs, 3)

    def test_import_never_creates_readiness_labels(self):
        svc.run_csv_import(self.repo, self.CSV, species="Super Orange",
                           dry_run=False)
        for p in self.repo.list_plants():
            self.assertIsNone(
                self.repo.get_readiness_event(p["plant_id"]))

    def test_export_roundtrip(self):
        svc.run_csv_import(self.repo, self.CSV, species="Super Orange",
                           dry_run=False)
        out = svc.export_table_csv(self.repo, "plants")
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)  # header + 2 plants
        self.assertIn("P-1", out)
        obs_out = svc.export_table_csv(self.repo, "observations")
        self.assertEqual(len(obs_out.strip().splitlines()), 4)
        fb_out = svc.export_table_csv(self.repo, "feedback_14d")
        self.assertEqual(fb_out, "")  # none yet
        with self.assertRaises(ValidationError):
            svc.export_table_csv(self.repo, "nope")

    def test_export_unknown_table_rejected(self):
        with self.assertRaises(ValidationError):
            svc.export_table_csv(self.repo, "readiness_events")


class TestPredictionFallback(ServiceBase):
    def _seed_observations(self, pid):
        svc.record_observation(self.repo, plant_id=pid,
                               observation_date="2083-05-10", height_cm=3.5)
        svc.record_observation(self.repo, plant_id=pid,
                               observation_date="2083-05-26", height_cm=8.0)

    def test_no_observations_is_not_a_prediction(self):
        pid = self._plant()
        res = svc.predict_for_plant(self.repo, pid, self.models)
        self.assertFalse(res["available"])
        self.assertIn("No observations recorded", res["message"])

    def test_real_plant_without_model_gets_honest_refusal(self):
        pid = self._plant()
        self._seed_observations(pid)
        res = svc.predict_for_plant(self.repo, pid, self.models)
        self.assertFalse(res["available"])
        self.assertEqual(res["message"], config.UNAVAILABLE_MESSAGE)
        self.assertFalse(res["synthetic"])
        self.assertIn("readiness_event_counts", res)

    def test_unknown_plant_raises(self):
        with self.assertRaises(NotFoundError):
            svc.predict_for_plant(self.repo, 9999, self.models)

    def test_synthetic_not_used_without_opt_in(self):
        """Even with a trained synthetic model, the default is refusal."""
        pid = self._plant()
        self._seed_observations(pid)
        self._train_synthetic_model()
        res = svc.predict_for_plant(self.repo, pid, self.models)
        self.assertFalse(res["available"])
        # honest refusal, plus the explanation that demo models exist
        # but were deliberately NOT used
        self.assertTrue(res["message"].startswith(config.UNAVAILABLE_MESSAGE))
        self.assertIn("NOT used", res["message"])
        self.assertFalse(res["synthetic"])

        opted = svc.predict_for_plant(self.repo, pid, self.models,
                                      allow_synthetic=True)
        self.assertTrue(opted["available"])
        self.assertTrue(opted["synthetic"])
        self.assertIn("SYNTHETIC DEMO", opted["message"])
        self.assertIn("cross_source_warning", opted)

    def _train_synthetic_model(self):
        """Train + save a small validated synthetic artefact for Level 3."""
        import random
        rng = random.Random(7)
        rows = []
        for pid in range(1, 13):
            ready_age = rng.randint(26, 34)
            for age in range(8, ready_age + 1, 3):
                rows.append({
                    "plant_id": pid,
                    "species": "Super Orange",
                    "variety": "Early",
                    "age_days": age,
                    "height_cm": 0.3 + 0.25 * age + rng.gauss(0, 0.1),
                    "growth_rate_cm_per_day": 0.25 + rng.gauss(0, 0.05),
                    "stem_width_cm": None,
                    "visible_true_leaf_count": None,
                    "days_remaining": ready_age - age,
                })
        df = mdl.build_training_frame(rows)
        artifact = mdl.train_countdown_model(
            df, data_source="synthetic", species="Super Orange",
            variety="Early", source_label="unit test")
        mdl.save_artifact(artifact, self.models)

    def test_species_plan_reports_evidence_shortfall(self):
        plan = svc.species_model_plan(self.repo, "Super Orange", "Early",
                                      self.models)
        self.assertEqual(plan["level"], 4)
        self.assertFalse(plan["meets_min_requirements"])
        self.assertEqual(plan["real_readiness_event_counts"]
                         ["readiness_events"], 0)
        self.assertEqual(plan["min_requirements"]["readiness_events"],
                         config.MIN_READINESS_EVENTS)

    def test_species_plan_with_real_evidence(self):
        pid = self._plant()
        self._seed_observations(pid)
        self.repo.record_readiness_event(
            plant_id=pid, readiness_observed_date="2083-06-05",
            readiness_definition_version="so-v1",
            assessment_source="expert_assessment", data_source="real")
        plan = svc.species_model_plan(self.repo, "Super Orange", "Early",
                                      self.models)
        self.assertEqual(plan["real_readiness_event_counts"]
                         ["readiness_events"], 1)
        # 1 event < 6 -> still not enough
        self.assertFalse(plan["meets_min_requirements"])


class TestDashboardSeparation(ServiceBase):
    def test_dashboard_groups_by_source(self):
        real = self._plant(data_source="real")
        syn = self._plant(species="Demo Bean", variety="Bush",
                          data_source="synthetic")
        svc.record_observation(self.repo, plant_id=real,
                               observation_date="2083-05-10", height_cm=3)
        svc.record_observation(self.repo, plant_id=syn,
                               observation_date="2083-05-10", height_cm=3,
                               data_source="synthetic")
        summary = svc.dashboard_summary(self.repo, self.models)
        self.assertEqual(summary["n_plants"], 2)
        sources = {r["data_source"]
                   for r in summary["readiness_by_species"]}
        self.assertEqual(sources, {"real", "synthetic"})
        self.assertEqual(summary["model_status"]["real"]["active"], 0)
        self.assertEqual(summary["model_status"]["synthetic"]["active"], 0)

    def test_model_status_handles_missing_registry(self):
        status = svc.model_status(Path("/nonexistent/models"))
        self.assertEqual(status["real"]["total"], 0)
        self.assertEqual(status["synthetic"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
