"""Tests: prediction snapshots, readiness assessments, transplant record and
feedback-driven evaluation (including no-future-leakage and the model gate)."""

import tempfile
import unittest
from pathlib import Path

from plantreadiness import config
from plantreadiness import model as mdl
from plantreadiness import services as svc
from plantreadiness.db import DuplicateRecordError, ValidationError
from plantreadiness.repository import NotFoundError, create_sqlite_repository


class FeedbackBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models = Path(self.tmp.name) / "models"
        self.repo = create_sqlite_repository(Path(self.tmp.name) / "fb.db")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _plant(self, ref="1"):
        return self.repo.register_plant(
            species="Super Orange", data_source="real", external_ref=ref)

    def _snapshot(self, plant_id, *, observation_date="2083-05-10",
                  predicted_date="2083-05-12", method="validated_model",
                  days=2):
        result = {
            "species": "Super Orange", "variety": None, "available": True,
            "method": method, "synthetic": method == config.METHOD_SYNTHETIC_DEMO,
            "provisional": method == config.METHOD_REFERENCE_BASELINE,
            "observation_date": observation_date, "days_remaining": days,
            "predicted_readiness_date": predicted_date, "data_source": "real",
        }
        return svc.save_prediction_snapshot(self.repo, result,
                                            plant_id=plant_id)


class TestSnapshots(FeedbackBase):
    def test_snapshot_is_persisted_and_reconstructible(self):
        pid = self._plant()
        snap = self._snapshot(pid)
        history = svc.list_prediction_history(self.repo, plant_id=pid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["prediction_id"], snap["prediction_id"])
        self.assertEqual(history[0]["predicted_readiness_date"], "2083-05-12")
        self.assertTrue(history[0]["available"])

    def test_new_prediction_does_not_rewrite_history(self):
        pid = self._plant()
        first = self._snapshot(pid, predicted_date="2083-05-12")
        second = self._snapshot(pid, predicted_date="2083-05-14", days=4)
        history = svc.list_prediction_history(self.repo, plant_id=pid)
        self.assertEqual(len(history), 2)
        ids = {h["prediction_id"] for h in history}
        self.assertEqual(ids, {first["prediction_id"],
                               second["prediction_id"]})
        original = next(h for h in history
                        if h["prediction_id"] == first["prediction_id"])
        self.assertEqual(original["predicted_readiness_date"], "2083-05-12")

    def test_unavailable_prediction_snapshot_allowed(self):
        pid = self._plant()
        snap = svc.save_prediction_snapshot(self.repo, {
            "species": "Super Orange", "available": False,
            "method": config.METHOD_UNAVAILABLE, "message": "insufficient",
            "observation_date": "2083-05-10"}, plant_id=pid)
        self.assertFalse(snap["available"])
        self.assertEqual(snap["method"], config.METHOD_UNAVAILABLE)


class TestReadinessAssessments(FeedbackBase):
    def test_assessment_kept_distinct_from_prediction(self):
        pid = self._plant()
        snap = self._snapshot(pid)
        assessment = svc.record_readiness_assessment(
            self.repo, plant_id=pid, assessment_date="2083-05-11",
            readiness_status="ready", notes="looks sturdy",
            prediction_id=snap["prediction_id"])
        self.assertEqual(assessment["readiness_status"], "ready")
        # the prediction shown at the time is preserved with the assessment
        self.assertEqual(assessment["prediction_snapshot"]["method"],
                         "validated_model")
        self.assertEqual(assessment["prediction_snapshot"]["predicted_readiness_date"],
                         "2083-05-12")

    def test_assessment_never_creates_a_training_label(self):
        pid = self._plant()
        svc.record_readiness_assessment(
            self.repo, plant_id=pid, assessment_date="2083-05-11",
            readiness_status="ready")
        self.assertIsNone(self.repo.get_readiness_event(pid))

    def test_assessment_never_implies_a_transplant(self):
        pid = self._plant()
        svc.record_readiness_assessment(
            self.repo, plant_id=pid, assessment_date="2083-05-11",
            readiness_status="ready")
        self.assertEqual(self.repo.all_feedback(plant_id=pid), [])
        self.assertEqual(self.repo.pending_feedback(plant_id=pid), [])

    def test_invalid_status_rejected(self):
        pid = self._plant()
        with self.assertRaises(ValidationError):
            svc.record_readiness_assessment(
                self.repo, plant_id=pid, assessment_date="2083-05-11",
                readiness_status="maybe")

    def test_unknown_prediction_reference_rejected(self):
        pid = self._plant()
        with self.assertRaises(NotFoundError):
            svc.record_readiness_assessment(
                self.repo, plant_id=pid, assessment_date="2083-05-11",
                readiness_status="ready", prediction_id=9999)

    def test_list_scopes_by_plant(self):
        p1, p2 = self._plant("1"), self._plant("2")
        svc.record_readiness_assessment(
            self.repo, plant_id=p1, assessment_date="2083-05-11",
            readiness_status="ready")
        svc.record_readiness_assessment(
            self.repo, plant_id=p2, assessment_date="2083-05-11",
            readiness_status="not_ready")
        self.assertEqual(len(svc.list_readiness_assessments(
            self.repo, plant_id=p1)), 1)


class TestTransplantRecord(FeedbackBase):
    def test_transplant_detail_is_stored(self):
        pid = self._plant()
        rec = svc.record_transplant(
            self.repo, plant_id=pid, transplant_date="2083-05-11",
            height_at_transplant_cm=2.0, condition_at_transplant="healthy",
            transplant_notes="moved to bed 3")
        self.assertTrue(rec["transplant_occurred"])
        self.assertEqual(rec["condition_at_transplant"], "healthy")
        rows = self.repo.all_feedback(plant_id=pid)
        self.assertEqual(rows[0]["transplant_notes"], "moved to bed 3")

    def test_non_occurred_transplant_creates_no_due_feedback(self):
        pid = self._plant()
        rec = svc.record_transplant(
            self.repo, plant_id=pid, transplant_date="2083-05-11",
            transplant_occurred=False, transplant_notes="deferred")
        self.assertFalse(rec["transplant_occurred"])
        self.assertEqual(self.repo.pending_feedback(plant_id=pid), [])
        with self.assertRaises(ValidationError):
            svc.submit_feedback(
                self.repo, feedback_id=rec["feedback_id"],
                feedback_submitted_date="2083-05-25",
                survived_after_14d=True)

    def test_duplicate_transplant_rejected(self):
        pid = self._plant()
        svc.record_transplant(self.repo, plant_id=pid,
                              transplant_date="2083-05-11")
        with self.assertRaises(DuplicateRecordError):
            svc.record_transplant(self.repo, plant_id=pid,
                                  transplant_date="2083-05-11")


class TestModelGate(unittest.TestCase):
    """The existing retraining gate must keep the current model unless the
    candidate is strictly better on the same held-out set."""

    def _artifact(self, validated=True):
        return {"metadata": {"validated": validated}}

    def test_candidate_worse_keeps_current(self):
        decision = mdl.decide_update(
            current=self._artifact(), candidate=self._artifact(),
            current_holdout_mae=1.0, candidate_holdout_mae=2.5)
        self.assertFalse(decision["replace"])

    def test_candidate_better_replaces(self):
        decision = mdl.decide_update(
            current=self._artifact(), candidate=self._artifact(),
            current_holdout_mae=3.0, candidate_holdout_mae=2.0)
        self.assertTrue(decision["replace"])

    def test_missing_holdout_keeps_current(self):
        decision = mdl.decide_update(
            current=self._artifact(), candidate=self._artifact(),
            current_holdout_mae=None, candidate_holdout_mae=1.0)
        self.assertFalse(decision["replace"])

    def test_unvalidated_candidate_kept_out(self):
        decision = mdl.decide_update(
            current=self._artifact(), candidate=self._artifact(False),
            current_holdout_mae=5.0, candidate_holdout_mae=1.0)
        self.assertFalse(decision["replace"])


if __name__ == "__main__":
    unittest.main()
