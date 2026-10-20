"""Tests: reset the runtime database back to the committed CSV cohort.

``plantreadiness.reset`` must remove data that was added after the seed
(camera plants, experiment fixtures, synthetic rows) and return the database to
exactly the reference state the committed CSV describes - without inventing
data, without creating readiness events, and without destroying the only copy
of data it cannot rebuild (it refuses to run when the CSV is absent).
"""

import tempfile
import unittest
from pathlib import Path

from plantreadiness import db as dbm
from plantreadiness import reset

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "super_orange_growth_data.csv"

# The committed cohort: 10 plants, 250 observations.
CSV_PLANTS = 10
CSV_OBSERVATIONS = 250


class TestReset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db = base / "plant_readiness.db"
        self.models = base / "models"
        self.backups = base / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def _reset(self, **kwargs):
        base_dir = Path(self.tmp.name) / "project"
        base_dir.mkdir(exist_ok=True)
        kwargs.setdefault("db_path", self.db)
        kwargs.setdefault("csv_path", CSV_PATH)
        kwargs.setdefault("models_dir", self.models)
        kwargs.setdefault("backup_dir", self.backups)
        kwargs.setdefault("base_dir", base_dir)
        kwargs.setdefault("verbose", False)
        return reset.reset_database(**kwargs)

    def _seed(self):
        return self._reset()

    def _add_test_residue(self):
        """Mimic data created during testing: a camera plant + capture."""
        conn = dbm.connect(self.db)
        try:
            pid = dbm.register_plant(
                conn, species="Sunflower (SuperOrange)", data_source="real",
                external_ref="Plant-01")
            dbm.add_observation(conn, plant_id=pid,
                                observation_date="2026-10-16", age_days=40,
                                height_cm=6.5854, data_source="real")
            dbm.add_capture(conn, plant_id=pid,
                            public_id="Plant01_20261016_105000",
                            observation_date="2026-10-16", validity="valid",
                            rod_length_cm=10.0, height_cm=6.5854,
                            confidence=0.66)
        finally:
            conn.close()

    def _counts(self):
        conn = dbm.connect(self.db)
        try:
            return {
                "plants": conn.execute(
                    "SELECT COUNT(*) FROM plants").fetchone()[0],
                "observations": conn.execute(
                    "SELECT COUNT(*) FROM observations").fetchone()[0],
                "captures": conn.execute(
                    "SELECT COUNT(*) FROM camera_captures").fetchone()[0],
                "cohorts": {r["cohort"] for r in conn.execute(
                    "SELECT DISTINCT cohort FROM plants")},
            }
        finally:
            conn.close()

    def test_reset_removes_data_added_after_seed(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()
        self._add_test_residue()
        self.assertEqual(self._counts()["plants"], CSV_PLANTS + 1)
        self.assertEqual(self._counts()["captures"], 1)

        report = self._reset()

        counts = self._counts()
        self.assertEqual(counts["plants"], CSV_PLANTS)
        self.assertEqual(counts["observations"], CSV_OBSERVATIONS)
        self.assertEqual(counts["captures"], 0)
        self.assertEqual(counts["cohorts"], {"reference"})
        self.assertEqual(report["after"]["plants_outside_reference_cohort"], 0)

    def test_reset_keeps_a_timestamped_backup(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()
        report = self._reset(backup=True)

        backup = Path(report["backup"])
        self.assertTrue(backup.exists())
        self.assertIn(".bak-", backup.name)
        # The backup lives in the configured (git-ignored) backup directory,
        # never next to the database in the repo root.
        self.assertEqual(backup.parent, self.backups)
        # ...and it is still a readable database.
        conn = dbm.connect(backup)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM plants").fetchone()[0],
                CSV_PLANTS)
        finally:
            conn.close()

    def test_reset_without_backup_writes_no_backup(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()
        report = self._reset(backup=False)
        self.assertIsNone(report["backup"])
        self.assertFalse(self.backups.exists())

    def test_reset_works_when_database_absent(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self.assertFalse(self.db.exists())
        report = self._reset()
        self.assertFalse(report["before"]["exists"])
        self.assertEqual(report["after"]["plants"], CSV_PLANTS)

    def test_reset_is_idempotent(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()
        second = self._reset()
        self.assertEqual(second["import"]["observations_inserted"],
                         CSV_OBSERVATIONS)
        self.assertEqual(self._counts()["plants"], CSV_PLANTS)

    def test_reset_clears_processed_cloudinary_ids(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        camera_dir = Path(self.tmp.name) / "project"
        camera_dir.mkdir()
        ledger = camera_dir / "processed_cloudinary_ids.txt"
        ledger.write_text("Plant02_20261020_100000\n", encoding="utf-8")

        report = self._reset(base_dir=camera_dir)

        self.assertTrue(report["processed_cloudinary_ids_cleared"])
        self.assertFalse(ledger.exists())

    def test_reset_refuses_when_csv_missing_and_keeps_the_database(self):
        if not CSV_PATH.exists():
            self.skipTest("real CSV not present")
        self._seed()
        missing = self.db.parent / "does_not_exist.csv"

        with self.assertRaises(FileNotFoundError):
            self._reset(csv_path=missing)

        # The database and its reference data must be untouched.
        self.assertTrue(self.db.exists())
        self.assertEqual(self._counts()["observations"], CSV_OBSERVATIONS)

    def test_camera_cleanup_targets_only_runtime_artifacts(self):
        base = Path(self.tmp.name) / "proj"
        base.mkdir()
        (base / "processed_cloudinary_ids.txt").write_text("id1\n")
        (base / "Plant01_20261016_105000.jpg").write_bytes(b"x")
        (base / "plant_result.jpg").write_bytes(b"x")
        (base / "plant_result_1.jpg").write_bytes(b"annotated result")
        (base / "plant_captured.jpg").write_bytes(b"x")
        (base / "captures").mkdir()
        (base / "captures" / "a.jpg").write_bytes(b"x")
        (base / "keepme.txt").write_text("keep")

        removed = reset._clear_camera_artifacts(base)

        self.assertEqual((base / "keepme.txt").read_text(), "keep")
        self.assertTrue((base / "processed_cloudinary_ids.txt").exists())
        self.assertFalse((base / "Plant01_20261016_105000.jpg").exists())
        self.assertTrue((base / "plant_result.jpg").exists())
        self.assertTrue((base / "plant_result_1.jpg").exists())
        self.assertFalse((base / "plant_captured.jpg").exists())
        self.assertFalse((base / "captures").exists())
        self.assertNotIn("processed_cloudinary_ids.txt", removed)
        self.assertIn("captures/", removed)


if __name__ == "__main__":
    unittest.main()
