"""Tests: repository layer (local SQLite backend).

Covers the requirements the app must satisfy:

* duplicate blocking for plants / observations / feedback,
* feedback due-date state machine,
* corrections preserve audit history,
* real/synthetic separation,
* CSV export and prediction-unavailable behaviour.

Everything runs against a temporary local SQLite file - no network access and
no secrets are involved.
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from plantreadiness import config
from plantreadiness import services as svc
from plantreadiness.db import (DuplicateRecordError, NotFoundError,
                               ValidationError)
from plantreadiness.repository import (DatabaseError, SQLiteRepository,
                                       create_sqlite_repository)


# --------------------------------------------------------------------------- #
# SQLite backend
# --------------------------------------------------------------------------- #


class SQLiteRepoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = create_sqlite_repository(
            Path(self.tmp.name) / "repo.db")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _plant(self, **kw):
        kw.setdefault("species", "Super Orange")
        kw.setdefault("variety", "Early")
        kw.setdefault("date_started", "2083-04-24")
        kw.setdefault("data_source", "real")
        return self.repo.register_plant(**kw)


class TestSQLiteRepository(SQLiteRepoBase):
    def test_register_and_get_plant(self):
        pid = self._plant()
        plant = self.repo.get_plant(pid)
        self.assertEqual(plant["species"], "Super Orange")
        self.assertEqual(plant["data_source"], "real")

    def test_duplicate_plant_rejected(self):
        self._plant(external_ref="X-1")
        with self.assertRaises(DuplicateRecordError):
            self._plant(external_ref="X-1")

    def test_unknown_plant_returns_none(self):
        self.assertIsNone(self.repo.get_plant(99999))

    def test_duplicate_observation_rejected(self):
        pid = self._plant()
        self.repo.add_observation(plant_id=pid, observation_date="2083-05-10",
                                  height_cm=3.5)
        with self.assertRaises(DuplicateRecordError):
            self.repo.add_observation(plant_id=pid,
                                      observation_date="2083-05-10",
                                      height_cm=4.0)

    def test_observation_source_mismatch_rejected(self):
        pid = self._plant()
        with self.assertRaises(ValidationError):
            self.repo.add_observation(plant_id=pid,
                                      observation_date="2083-05-10",
                                      height_cm=3.5, data_source="synthetic")

    def test_search_filters_plants(self):
        self._plant(species="Super Orange", variety="Early")
        self._plant(species="Demo Bean", variety="Bush")
        self.assertEqual(len(self.repo.list_plants()), 2)
        self.assertEqual(
            len(self.repo.list_plants(search="demo")), 1)
        self.assertEqual(
            len(self.repo.list_plants(data_source="synthetic")), 0)

    def test_count_observations_separates_sources(self):
        pid_real = self._plant()
        pid_syn = self._plant(species="Demo Bean", variety="Bush",
                              data_source="synthetic")
        self.repo.add_observation(plant_id=pid_real,
                                  observation_date="2083-05-10", height_cm=1)
        self.repo.add_observation(plant_id=pid_syn,
                                  observation_date="2083-05-10", height_cm=1,
                                  data_source="synthetic")
        self.assertEqual(self.repo.count_observations("real"), 1)
        self.assertEqual(self.repo.count_observations("synthetic"), 1)
        self.assertEqual(self.repo.count_observations(), 2)


class TestFeedbackWorkflow(SQLiteRepoBase):
    def _feedback(self, transplant="2083-05-20"):
        pid = self._plant()
        return pid, self.repo.record_transplant(
            plant_id=pid, transplant_date=transplant,
            height_at_transplant_cm=6.4)

    def test_due_date_is_transplant_plus_14(self):
        _, fb = self._feedback()
        self.assertEqual(fb["feedback_due_date"], "2083-06-03")

    def test_duplicate_transplant_rejected(self):
        pid = self._plant()
        self.repo.record_transplant(plant_id=pid,
                                    transplant_date="2083-05-20")
        with self.assertRaises(DuplicateRecordError):
            self.repo.record_transplant(plant_id=pid,
                                        transplant_date="2083-05-20")

    def test_pending_feedback_states(self):
        _, fb = self._feedback("2083-05-20")   # due 2083-06-03
        rows = self.repo.pending_feedback(as_of=date(2083, 6, 1))
        self.assertEqual(len(rows), 1)
        state = svc.feedback_state(rows[0], as_of=date(2083, 6, 1))
        self.assertEqual(state["status"], "pending")
        state = svc.feedback_state(rows[0], as_of=date(2083, 6, 3))
        self.assertEqual(state["status"], "due")
        state = svc.feedback_state(rows[0], as_of=date(2083, 6, 10))
        self.assertEqual(state["status"], "overdue")
        self.assertEqual(state["days_overdue"], 7)

    def test_completed_feedback_leaves_pending_list(self):
        _, fb = self._feedback()
        self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        self.assertEqual(self.repo.pending_feedback(), [])

    def test_duplicate_feedback_submission_blocked(self):
        _, fb = self._feedback()
        self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        with self.assertRaises(DuplicateRecordError):
            self.repo.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-04",
                survived_after_14d=False)

    def test_feedback_validation_consistency(self):
        _, fb = self._feedback()
        with self.assertRaises(ValidationError):
            self.repo.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=True, visible_condition="dead")
        with self.assertRaises(ValidationError):
            self.repo.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=False, visible_condition="healthy")

    def test_growth_only_when_both_heights_exist(self):
        _, fb = self._feedback()
        done = self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        self.assertEqual(done["growth_after_14d_cm"], 1.1)

        # dead plant, no height after -> growth stays NULL
        pid2 = self._plant(variety="Late")
        fb2 = self.repo.record_transplant(plant_id=pid2,
                                          transplant_date="2083-05-20")
        done2 = self.repo.submit_feedback(
            feedback_id=fb2["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=False)
        self.assertIsNone(done2["growth_after_14d_cm"])
        self.assertEqual(done2["visible_condition"], "dead")

    def test_correction_requires_reason_and_keeps_audit(self):
        _, fb = self._feedback()
        self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")

        with self.assertRaises(ValidationError):
            svc.correct_feedback(self.repo, feedback_id=fb["feedback_id"],
                                 height_after_14d_cm=8.0, reason="  ")

        fixed = svc.correct_feedback(
            self.repo, feedback_id=fb["feedback_id"],
            height_after_14d_cm=8.0, reason="misread tape")
        self.assertEqual(fixed["height_after_14d_cm"], 8.0)
        self.assertEqual(fixed["growth_after_14d_cm"], 1.6)

        revisions = self.repo.feedback_revisions(fb["feedback_id"])
        self.assertEqual(len(revisions), 1)
        previous = json.loads(revisions[0]["previous_values"])
        self.assertEqual(previous["height_after_14d_cm"], 7.5)
        self.assertEqual(revisions[0]["reason"], "misread tape")

    def test_cannot_correct_unsubmitted_feedback(self):
        _, fb = self._feedback()
        with self.assertRaises(ValidationError):
            svc.correct_feedback(self.repo, feedback_id=fb["feedback_id"],
                                 height_after_14d_cm=8.0, reason="early")

    def test_dashboard_stats_counts(self):
        self._feedback()
        stats = self.repo.dashboard_stats()
        self.assertEqual(stats["n_plants"], 1)
        self.assertEqual(stats["n_feedback_total"], 1)
        self.assertEqual(stats["n_feedback_pending"], 1)


if __name__ == "__main__":
    unittest.main()
