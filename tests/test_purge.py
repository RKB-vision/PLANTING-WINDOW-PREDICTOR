"""Tests: the synthetic purge removes synthetic rows and nothing else.

Real records are append-only (see ``AGENTS.md``), so the only thing this tool
is allowed to delete is data explicitly tagged ``data_source='synthetic'``.
"""

import tempfile
import unittest
from pathlib import Path

from plantreadiness import db as dbm
from plantreadiness import purge
from plantreadiness.repository import SQLiteRepository


class TestPurgeSynthetic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbm.init_db(Path(self.tmp.name) / "test.db")

        # --- real data -----------------------------------------------------
        self.real_pid = dbm.register_plant(
            self.conn, species="Super Orange", data_source="real",
            external_ref="R1")
        dbm.add_observation(
            self.conn, plant_id=self.real_pid,
            observation_date="2026-09-02", age_days=7, height_cm=0.5,
            data_source="real")
        dbm.save_prediction_snapshot(
            self.conn, plant_id=self.real_pid, species="Super Orange",
            method="historical_reference_baseline", available=True,
            data_source="real")

        # --- synthetic data ------------------------------------------------
        self.syn_pid = dbm.register_plant(
            self.conn, species="DemoBean", data_source="synthetic",
            external_ref="S1")
        dbm.add_observation(
            self.conn, plant_id=self.syn_pid,
            observation_date="2083-01-01", age_days=3, height_cm=0.2,
            data_source="synthetic")
        dbm.save_prediction_snapshot(
            self.conn, plant_id=self.syn_pid, species="DemoBean",
            method="synthetic_demo", available=True, synthetic=True,
            data_source="synthetic")
        fb = dbm.record_transplant(
            self.conn, self.syn_pid, "2083-01-20", data_source="synthetic")
        self.syn_feedback_id = fb["feedback_id"]
        # feedback_corrections is created lazily by the repository layer.
        SQLiteRepository(self.conn).ensure_feedback_revisions_table()
        self.conn.execute(
            """INSERT INTO feedback_corrections
               (feedback_id, previous_values, corrected_at, reason)
               VALUES (?, '{}', '2083-01-21', 'test correction')""",
            (self.syn_feedback_id,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _count(self, table, source):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE data_source = ?",
            (source,)).fetchone()[0]

    def test_dry_run_deletes_nothing(self):
        report = purge.purge_synthetic(self.conn, dry_run=True)

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["tables"]["plants"], 1)
        self.assertEqual(report["tables"]["observations"], 1)
        self.assertEqual(report["tables"]["prediction_history"], 1)
        self.assertEqual(report["tables"]["feedback_14d"], 1)
        self.assertEqual(report["tables"]["feedback_corrections"], 1)
        self.assertGreater(report["total"], 0)
        # nothing removed
        self.assertEqual(self._count("observations", "synthetic"), 1)

    def test_purge_removes_only_synthetic_rows(self):
        purge.purge_synthetic(self.conn, dry_run=False)

        for table in ("plants", "observations", "prediction_history"):
            self.assertEqual(self._count(table, "synthetic"), 0, table)
            self.assertEqual(self._count(table, "real"), 1, table)
        self.assertEqual(self._count("feedback_14d", "synthetic"), 0)

        # the real plant and its history survive intact
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM plants").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM feedback_corrections").fetchone()[0], 0)

    def test_second_purge_is_a_noop(self):
        purge.purge_synthetic(self.conn, dry_run=False)
        second = purge.purge_synthetic(self.conn, dry_run=False)
        self.assertEqual(second["total"], 0)
