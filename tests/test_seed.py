"""Tests: seeding the shared state from committed sources.

The repository does not commit ``plant_readiness.db``. ``plantreadiness.seed``
must be able to recreate the same reference state a collaborator needs, from
the committed CSV, without inventing data and without creating readiness
events.
"""

import tempfile
import unittest
from pathlib import Path

from plantreadiness import db as dbm
from plantreadiness import seed

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "super_orange_growth_data.csv"


class TestSeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db = base / "plant_readiness.db"
        self.models = base / "models"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        return seed.seed_database(
            db_path=self.db, csv_path=CSV_PATH,
            models_dir=self.models, verbose=False)

    def test_seed_builds_reference_state(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        report = self._seed()

        self.assertTrue(self.db.exists())
        self.assertEqual(report["import"]["observations_inserted"], 250)
        self.assertEqual(report["import"]["plants_imported"], 10)
        self.assertEqual(report["baseline"]["n_plants"], 10)
        self.assertTrue(Path(report["baseline"]["path"]).exists())

    def test_seed_is_idempotent(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()
        second = self._seed()

        self.assertEqual(second["import"]["observations_inserted"], 0)
        self.assertEqual(second["import"]["observations_skipped_duplicate"],
                         250)
        self.assertEqual(second["baseline"]["n_plants"], 10)

    def test_seed_does_not_churn_an_existing_baseline(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        first = self._seed()
        path = Path(first["baseline"]["path"])
        before = path.read_bytes()

        second = self._seed()

        self.assertFalse(second["baseline"]["rebuilt"])
        self.assertEqual(before, path.read_bytes())

    def test_force_rebuilds_the_baseline(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()
        forced = seed.seed_database(
            db_path=self.db, csv_path=CSV_PATH,
            models_dir=self.models, force=True, verbose=False)
        self.assertTrue(forced["baseline"]["rebuilt"])
        self.assertEqual(forced["baseline"]["n_plants"], 10)

    def test_seed_creates_no_readiness_events(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()

        conn = dbm.connect(self.db)
        try:
            n_events = conn.execute(
                "SELECT COUNT(*) FROM readiness_events").fetchone()[0]
            self.assertEqual(n_events, 0)
        finally:
            conn.close()

    def test_seed_tags_the_reference_cohort(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()

        conn = dbm.connect(self.db)
        try:
            cohorts = {r["cohort"] for r in
                       conn.execute("SELECT DISTINCT cohort FROM plants")}
            self.assertEqual(cohorts, {"reference"})
        finally:
            conn.close()
