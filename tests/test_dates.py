"""Tests: date calculations (days-remaining target, 14-day due dates)."""

import unittest
from datetime import date

from plantreadiness import db as dbm
from plantreadiness import config


class TestFeedbackDueDate(unittest.TestCase):
    def test_due_date_is_transplant_plus_14_days(self):
        due = dbm.feedback_due_date("2083-05-20")
        self.assertEqual(due, date(2083, 6, 3))

    def test_due_date_across_month_boundary(self):
        due = dbm.feedback_due_date("2083-05-30")
        self.assertEqual(due, date(2083, 6, 13))

    def test_window_constant(self):
        self.assertEqual(config.FEEDBACK_WINDOW_DAYS, 14)

    def test_due_date_accepts_date_object(self):
        self.assertEqual(dbm.feedback_due_date(date(2083, 1, 1)),
                         date(2083, 1, 15))


class TestDaysBetween(unittest.TestCase):
    def test_positive_difference(self):
        self.assertEqual(dbm.days_between("2083-05-10", "2083-05-26"), 16)

    def test_zero_difference(self):
        self.assertEqual(dbm.days_between("2083-05-10", "2083-05-10"), 0)

    def test_negative_difference(self):
        self.assertEqual(dbm.days_between("2083-05-26", "2083-05-10"), -16)


class TestDaysRemainingTarget(unittest.TestCase):
    """days_remaining = readiness_observed_date - observation_date."""

    def _seeded_conn(self):
        conn = dbm.init_db(":memory:")
        pid = dbm.register_plant(conn, species="Super Orange",
                                 variety="Early",
                                 date_started="2083-04-24",
                                 data_source="synthetic")
        dbm.add_observation(conn, pid, "2083-05-10", age_days=16,
                            height_cm=3.5, data_source="synthetic")
        dbm.add_observation(conn, pid, "2083-05-26", age_days=32,
                            height_cm=8.0, data_source="synthetic")
        dbm.record_readiness_event(
            conn, pid, "2083-05-24",
            readiness_definition_version="test-v1",
            assessment_source="unit_test",
            data_source="synthetic")
        return conn, pid

    def test_target_uses_elapsed_calendar_days(self):
        conn, pid = self._seeded_conn()
        rows = dbm.readiness_training_rows(conn, data_source="synthetic")
        self.assertEqual(len(rows), 1)  # the 05-26 observation is excluded
        self.assertEqual(rows[0]["observation_date"], "2083-05-10")
        self.assertEqual(rows[0]["days_remaining"], 14)  # 05-10 -> 05-24

    def test_observation_on_readiness_date_has_zero_target(self):
        conn, pid = self._seeded_conn()
        dbm.add_observation(conn, pid, "2083-05-24", age_days=30,
                            height_cm=7.2, data_source="synthetic")
        rows = dbm.readiness_training_rows(conn, data_source="synthetic")
        on_date = [r for r in rows if r["observation_date"] == "2083-05-24"]
        self.assertEqual(len(on_date), 1)
        self.assertEqual(on_date[0]["days_remaining"], 0)

    def test_post_readiness_observations_excluded(self):
        conn, pid = self._seeded_conn()
        rows = dbm.readiness_training_rows(conn, data_source="synthetic")
        self.assertTrue(all(r["days_remaining"] >= 0 for r in rows))
        self.assertTrue(all(r["observation_date"] <= "2083-05-24"
                            for r in rows))

    def test_plant_without_readiness_event_yields_no_target(self):
        conn = dbm.init_db(":memory:")
        pid = dbm.register_plant(conn, species="Super Orange",
                                 data_source="synthetic")
        dbm.add_observation(conn, pid, "2083-05-10", age_days=16,
                            height_cm=3.5, data_source="synthetic")
        rows = dbm.readiness_training_rows(conn, data_source="synthetic")
        self.assertEqual(rows, [])  # unknown target, not guessed

    def test_invalid_date_rejected(self):
        with self.assertRaises(dbm.ValidationError):
            dbm.parse_iso_date("not-a-date", "Date")


if __name__ == "__main__":
    unittest.main()
