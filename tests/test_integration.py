"""End-to-end integration tests: import -> predict -> transplant ->
feedback -> demo retrain, plus real/synthetic separation guarantees."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from plantreadiness import config
from plantreadiness import csv_import
from plantreadiness import db as dbm
from plantreadiness import model as mdl

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "super_orange_growth_data.csv"


class TestRealWorkflow(unittest.TestCase):
    """Mode B: import real CSV -> analysis -> honest 'unavailable' result."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "real.db"
        self.models = self.base / "models"

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_pipeline_no_readiness_model_no_synthetic_fallback(self):
        import model_train
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = model_train.run_real_training(
                self.db, self.models, CSV_PATH)
        out = buf.getvalue()

        # measurements preserved
        conn = dbm.init_db(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM observations") \
                .fetchone()[0]
            n_syn = conn.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE data_source='synthetic'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 250)
        self.assertEqual(n_syn, 0)

        # honest refusal, required message present
        self.assertFalse(res["trained"])
        self.assertIn(config.UNAVAILABLE_MESSAGE, out)
        # no synthetic artefacts created in real mode
        self.assertFalse((self.models / "real").exists())
        self.assertFalse((self.models / "synthetic").exists())

    def test_real_plant_prediction_unavailable_by_default(self):
        import prediction
        csv_import.import_csv(dbm.init_db(self.db), CSV_PATH)
        conn = dbm.init_db(self.db)
        try:
            # plant 1 = first imported real plant
            plant = conn.execute(
                "SELECT plant_id FROM plants ORDER BY plant_id LIMIT 1"
            ).fetchone()
            res = prediction.predict_for_plant(
                conn, int(plant["plant_id"]), self.models)
        finally:
            conn.close()
        self.assertFalse(res["available"])
        self.assertFalse(res["synthetic"])
        self.assertEqual(res["message"], config.UNAVAILABLE_MESSAGE)

    def test_real_plant_never_silently_uses_synthetic_models(self):
        """Even with synthetic models on disk, default prediction is level 4."""
        import prediction
        csv_import.import_csv(dbm.init_db(self.db), CSV_PATH)
        # train a genuine (tiny) synthetic model under the same models dir
        rows = [
            {"plant_id": i, "species": "Sunflower (SuperOrange)", "variety": "Early",
             "age_days": age, "height_cm": 0.3 + 0.25 * age,
             "growth_rate_cm_per_day": 0.25, "stem_width_cm": None,
             "visible_true_leaf_count": None,
             "days_remaining": 30 - age}
            for i in range(1, 9)
            for age in (10, 18, 26)
        ]
        df = mdl.build_training_frame(rows)
        artifact = mdl.train_countdown_model(df, data_source="synthetic",
                                             species="Sunflower (SuperOrange)",
                                             variety="Early",
                                             source_label="test")
        mdl.save_artifact(artifact, self.models)

        conn = dbm.init_db(self.db)
        try:
            plant = conn.execute(
                "SELECT plant_id FROM plants ORDER BY plant_id LIMIT 1"
            ).fetchone()
            default_res = prediction.predict_for_plant(
                conn, int(plant["plant_id"]), self.models)
            optin_res = prediction.predict_for_plant(
                conn, int(plant["plant_id"]), self.models,
                allow_synthetic=True)
        finally:
            conn.close()

        # default: no model used
        self.assertFalse(default_res["available"])
        # explicit opt-in: allowed but loudly labelled + warning
        self.assertTrue(optin_res["available"])
        self.assertTrue(optin_res["synthetic"])
        self.assertIn("cross_source_warning", optin_res)
        self.assertIn("SYNTHETIC DEMO", optin_res["message"])

    def test_species_status_reports_collecting_data(self):
        import plant_feedback
        csv_import.import_csv(dbm.init_db(self.db), CSV_PATH)
        conn = dbm.init_db(self.db)
        try:
            class A:
                species = "Sunflower (SuperOrange)"
                variety = None
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = plant_feedback.cmd_species_status(conn, A())
            out = buf.getvalue()
        finally:
            conn.close()
        self.assertEqual(rc, 0)
        self.assertIn(config.UNAVAILABLE_MESSAGE, out)
        self.assertIn("collecting data", out)


class TestDemoEndToEnd(unittest.TestCase):
    """Mode A: train -> predict -> transplant -> feedback -> retrain."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "demo.db"
        self.models = self.base / "models"

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_demo_workflow(self):
        import demo_workflow
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary = demo_workflow.run(self.db, self.models, seed=42)
        out = buf.getvalue()
        self.assertTrue(summary["ok"], msg=out[-3000:])
        self.assertIn("SYNTHETIC DEMO", out)
        steps = {s["step"]: s for s in summary["steps"]}
        self.assertTrue(steps["train_initial"]["ok"])
        self.assertTrue(steps["predict"]["ok"])
        self.assertEqual(steps["predict"]["level"], 3)
        self.assertTrue(steps["predict"]["synthetic"])
        self.assertTrue(steps["feedback"]["ok"])
        self.assertTrue(steps["retrain_compare"]["ok"])

    def test_demo_prediction_countdown_reasonable(self):
        import model_train
        import prediction
        with contextlib.redirect_stdout(io.StringIO()):
            model_train.run_demo_training(self.db, self.models, seed=42)
        conn = dbm.init_db(self.db)
        try:
            plant = conn.execute(
                """SELECT plant_id FROM plants
                   WHERE data_source='synthetic' AND variety='Early'
                   ORDER BY plant_id LIMIT 1""").fetchone()
            res = prediction.predict_for_plant(
                conn, int(plant["plant_id"]), self.models,
                allow_synthetic=True)
        finally:
            conn.close()
        self.assertTrue(res["available"])
        self.assertTrue(res["synthetic"])
        # The demo keeps a few days of post-readiness monitoring, so the
        # latest observation can sit just past the readiness date. An honest
        # model then reports a small countdown that may be slightly negative
        # ("past_readiness"); it must never be silently clamped to zero.
        self.assertLess(abs(res["days_remaining"]), 30)
        self.assertFalse(res["clamped"])
        self.assertIn(res["readiness_status"],
                      {"ready_soon", "ready_today", "past_readiness"})

    def test_retrain_never_trains_on_holdout(self):
        import model_train
        with contextlib.redirect_stdout(io.StringIO()):
            model_train.run_demo_training(self.db, self.models, seed=42)
            res = model_train.run_demo_retrain(self.db, self.models, seed=42)
        self.assertTrue(res["compared"])
        self.assertTrue(len(res["comparisons"]) >= 1)
        for comp in res["comparisons"]:
            if comp.get("compared"):
                self.assertIn("current_mae_days", comp)
                self.assertIn("candidate_mae_days", comp)
                # decision must be evidence-based
                self.assertIn("decision", comp)
                if not comp["decision"]["replace"]:
                    # candidate not better -> keep current
                    self.assertGreater(
                        comp["candidate_mae_days"],
                        comp["current_mae_days"] - 1e-9)

    def test_holdout_plants_never_in_training_metadata(self):
        import model_train
        with contextlib.redirect_stdout(io.StringIO()):
            model_train.run_demo_training(self.db, self.models, seed=42)
        conn = dbm.init_db(self.db)
        try:
            holdout_ids = {int(r["plant_id"]) for r in conn.execute(
                "SELECT plant_id FROM plants WHERE cohort='holdout'")}
            initial_ids = {int(r["plant_id"]) for r in conn.execute(
                "SELECT plant_id FROM plants WHERE cohort='initial'")}
        finally:
            conn.close()
        self.assertTrue(holdout_ids)
        self.assertFalse(holdout_ids & initial_ids)
        for entry in mdl.list_artifacts(self.models, "synthetic"):
            trained = set(entry["metadata"]["training_plant_ids"])
            self.assertFalse(trained & holdout_ids,
                             "holdout plant leaked into training")

    def test_feedback_and_readiness_do_not_mix_sources(self):
        """A synthetic plant's feedback stays synthetic; real stays real."""
        import demo_workflow
        with contextlib.redirect_stdout(io.StringIO()):
            demo_workflow.run(self.db, self.models, seed=42)
        conn = dbm.init_db(self.db)
        try:
            mixed = conn.execute(
                """SELECT COUNT(*) FROM feedback_14d f
                   JOIN plants p ON p.plant_id = f.plant_id
                   WHERE f.data_source != p.data_source""").fetchone()[0]
            obs_mixed = conn.execute(
                """SELECT COUNT(*) FROM observations o
                   JOIN plants p ON p.plant_id = o.plant_id
                   WHERE o.data_source != p.data_source""").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mixed, 0)
        self.assertEqual(obs_mixed, 0)

    def test_demo_output_always_labelled_synthetic(self):
        import model_train
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            model_train.run_demo_training(self.db, self.models, seed=42)
        out = buf.getvalue()
        self.assertIn("SYNTHETIC DEMO", out)
        self.assertIn("SIMULATED", out.upper())


class TestFeedbackReminders(unittest.TestCase):
    def test_reminders_printed_on_open(self):
        import plant_feedback
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "r.db")
            conn = dbm.init_db(db_path)
            pid = dbm.register_plant(conn, species="Super Orange",
                                     data_source="synthetic",
                                     external_ref="R-1")
            dbm.record_transplant(conn, pid, "2083-01-01",
                                  data_source="synthetic")
            conn.close()

            class A:
                as_of = "2083-01-20"   # 5 days past due
            buf = io.StringIO()
            conn = dbm.init_db(db_path)
            try:
                with contextlib.redirect_stdout(buf):
                    rc = plant_feedback.cmd_due(conn, A())
            finally:
                conn.close()
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("OVERDUE", out)
            self.assertIn("5 days", out)


if __name__ == "__main__":
    unittest.main()
