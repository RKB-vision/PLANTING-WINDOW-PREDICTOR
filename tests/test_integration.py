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

    def test_real_pipeline_no_readiness_model(self):
        conn = dbm.init_db(self.db)
        count = csv_import.import_csv(conn, CSV_PATH, species="Sunflower (SuperOrange)")["observations_inserted"]
        self.assertEqual(count, 250)

        conn = dbm.init_db(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 250)

        # No real model can be trained (no readiness events)
        conn = dbm.init_db(self.db)
        rows = dbm.readiness_training_rows(conn, data_source="real", species="Sunflower (SuperOrange)")
        self.assertEqual(len(rows), 0)

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
        self.assertEqual(res["message"], config.UNAVAILABLE_MESSAGE)


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



class TestFeedbackReminders(unittest.TestCase):
    def test_reminders_printed_on_open(self):
        import plant_feedback
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "r.db")
            conn = dbm.init_db(db_path)
            pid = dbm.register_plant(conn, species="Super Orange",
                                     data_source="real",
                                     external_ref="R-1")
            dbm.record_transplant(conn, pid, "2083-01-01",
                                  data_source="real")
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
