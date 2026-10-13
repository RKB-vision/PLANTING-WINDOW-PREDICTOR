"""Tests: growth calculations, missing measurements, feedback integrity."""

import unittest
from datetime import date

from plantreadiness import config
from plantreadiness import db as dbm


def _make_plant(conn, source="synthetic"):
    return dbm.register_plant(conn, species="Super Orange", variety="Early",
                              date_started="2083-04-24",
                              data_source=source)


class TestGrowthCalculation(unittest.TestCase):
    def test_growth_is_after_minus_at_transplant(self):
        self.assertAlmostEqual(
            dbm.compute_growth(6.4, 7.5), 1.1, places=4)

    def test_negative_growth_preserved(self):
        # a wilted plant can shrink; the arithmetic must not be faked upward
        self.assertAlmostEqual(
            dbm.compute_growth(7.5, 6.0), -1.5, places=4)

    def test_missing_after_height_gives_none(self):
        self.assertIsNone(dbm.compute_growth(6.4, None))

    def test_missing_transplant_height_gives_none(self):
        self.assertIsNone(dbm.compute_growth(None, 7.5))

    def test_both_missing_gives_none(self):
        self.assertIsNone(dbm.compute_growth(None, None))


class TestSubmitFeedback(unittest.TestCase):
    def setUp(self):
        self.conn = dbm.init_db(":memory:")
        self.pid = _make_plant(self.conn)
        self.rec = dbm.record_transplant(
            self.conn, self.pid, "2083-05-20",
            height_at_transplant_cm=6.4, data_source="synthetic")

    def test_due_date_created_transplant_plus_14(self):
        self.assertEqual(self.rec["feedback_due_date"], "2083-06-03")

    def test_survival_growth_and_condition_stored(self):
        out = dbm.submit_feedback(
            self.conn, self.rec["feedback_id"],
            feedback_submitted_date="2083-06-03",
            survived_after_14d=True,
            height_after_14d_cm=7.6,
            visible_condition="healthy",
            user_notes="good")
        self.assertEqual(out["survived_after_14d"], 1)
        self.assertAlmostEqual(out["growth_after_14d_cm"], 1.2, places=4)
        self.assertEqual(out["visible_condition"], "healthy")

    def test_dead_plant_keeps_survival_missing_measurements(self):
        out = dbm.submit_feedback(
            self.conn, self.rec["feedback_id"],
            feedback_submitted_date="2083-06-03",
            survived_after_14d=False,
            height_after_14d_cm=None,
            visible_condition="dead",
            user_notes="died overnight")
        self.assertEqual(out["survived_after_14d"], 0)
        self.assertIsNone(out["height_after_14d_cm"])
        self.assertIsNone(out["growth_after_14d_cm"])   # not invented
        self.assertEqual(out["visible_condition"], "dead")

    def test_dead_without_condition_defaults_to_dead(self):
        out = dbm.submit_feedback(
            self.conn, self.rec["feedback_id"],
            feedback_submitted_date="2083-06-03",
            survived_after_14d=False)
        self.assertEqual(out["visible_condition"], "dead")
        self.assertIsNone(out["growth_after_14d_cm"])

    def test_missing_transplant_height_means_growth_missing(self):
        rec2 = dbm.record_transplant(self.conn, self.pid, "2083-07-01",
                                     data_source="synthetic")
        out = dbm.submit_feedback(
            self.conn, rec2["feedback_id"],
            feedback_submitted_date="2083-07-15",
            survived_after_14d=True, height_after_14d_cm=8.0,
            visible_condition="healthy")
        self.assertIsNone(out["growth_after_14d_cm"])

    def test_duplicate_feedback_submission_rejected(self):
        dbm.submit_feedback(
            self.conn, self.rec["feedback_id"],
            feedback_submitted_date="2083-06-03",
            survived_after_14d=True, height_after_14d_cm=7.6,
            visible_condition="healthy")
        with self.assertRaises(dbm.DuplicateRecordError):
            dbm.submit_feedback(
                self.conn, self.rec["feedback_id"],
                feedback_submitted_date="2083-06-04",
                survived_after_14d=False)

    def test_duplicate_transplant_record_rejected(self):
        with self.assertRaises(dbm.DuplicateRecordError):
            dbm.record_transplant(self.conn, self.pid, "2083-05-20",
                                  data_source="synthetic")

    def test_submission_before_transplant_rejected(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.submit_feedback(
                self.conn, self.rec["feedback_id"],
                feedback_submitted_date="2083-05-19",
                survived_after_14d=True, visible_condition="healthy")

    def test_dead_condition_with_survived_true_rejected(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.submit_feedback(
                self.conn, self.rec["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=True, visible_condition="dead")

    def test_dead_with_wrong_condition_rejected(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.submit_feedback(
                self.conn, self.rec["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=False, visible_condition="healthy")

    def test_invalid_condition_rejected(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.submit_feedback(
                self.conn, self.rec["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=True, visible_condition="meh")

    def test_negative_height_rejected(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.submit_feedback(
                self.conn, self.rec["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=True, height_after_14d_cm=-1.0,
                visible_condition="healthy")


class TestObservationIntegrity(unittest.TestCase):
    def setUp(self):
        self.conn = dbm.init_db(":memory:")
        self.pid = _make_plant(self.conn)

    def test_duplicate_observation_same_date_rejected(self):
        dbm.add_observation(self.conn, self.pid, "2083-05-10", age_days=16,
                            height_cm=3.5, data_source="synthetic")
        with self.assertRaises(dbm.DuplicateRecordError):
            dbm.add_observation(self.conn, self.pid, "2083-05-10",
                                age_days=16, height_cm=9.9,
                                data_source="synthetic")

    def test_history_not_overwritten_after_duplicate_attempt(self):
        dbm.add_observation(self.conn, self.pid, "2083-05-10", age_days=16,
                            height_cm=3.5, data_source="synthetic")
        try:
            dbm.add_observation(self.conn, self.pid, "2083-05-10",
                                height_cm=9.9, data_source="synthetic")
        except dbm.DuplicateRecordError:
            pass
        obs = dbm.get_observations(self.conn, self.pid)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["height_cm"], 3.5)  # original preserved

    def test_missing_height_allowed_row_kept_missing(self):
        dbm.add_observation(self.conn, self.pid, "2083-05-11", age_days=17,
                            height_cm=None, data_source="synthetic")
        obs = dbm.get_observations(self.conn, self.pid)
        self.assertIsNone(obs[0]["height_cm"])

    def test_negative_height_rejected(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.add_observation(self.conn, self.pid, "2083-05-11",
                                height_cm=-2.0, data_source="synthetic")

    def test_duplicate_plant_registration_rejected(self):
        dbm.register_plant(conn=self.conn, species="Super Orange",
                           variety="Early", data_source="synthetic",
                           external_ref="X1")
        with self.assertRaises(dbm.DuplicateRecordError):
            dbm.register_plant(conn=self.conn, species="Super Orange",
                               variety="Early", data_source="synthetic",
                               external_ref="X1")

    def test_readiness_event_requires_definition_version(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.record_readiness_event(
                self.conn, self.pid, "2083-05-24",
                readiness_definition_version="  ",
                assessment_source="field_assessor",
                data_source="synthetic")

    def test_readiness_event_requires_assessment_source(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.record_readiness_event(
                self.conn, self.pid, "2083-05-24",
                readiness_definition_version="v1",
                assessment_source="",
                data_source="synthetic")

    def test_pending_feedback_lists_overdue(self):
        dbm.record_transplant(self.conn, self.pid, "2083-05-20",
                              data_source="synthetic")
        rows = dbm.pending_feedback(self.conn, as_of=date(2083, 6, 3))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["is_overdue"])      # due exactly that day
        rows = dbm.pending_feedback(self.conn, as_of=date(2083, 6, 10))
        self.assertTrue(rows[0]["is_overdue"])
        self.assertEqual(rows[0]["days_overdue"], 7)


if __name__ == "__main__":
    unittest.main()
