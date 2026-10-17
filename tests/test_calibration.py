"""Tests for calibration provenance and the quarantine of unverified data."""

import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plantreadiness import config
from plantreadiness import calibration as calib
from plantreadiness import db as dbm
from plantreadiness import services as svc
from plantreadiness.repository import SQLiteRepository


class TestCalibrationGrouping(unittest.TestCase):
    def test_unverified_camera_is_quarantined(self):
        row = {"calibration_source": config.CALIBRATION_SOURCE_CAMERA,
               "rod_length_cm": 10.0, "calibration_verified": False}
        self.assertEqual(calib.calibration_group(
            calibration_source=row["calibration_source"],
            rod_length_cm=row["rod_length_cm"],
            calibration_verified=row["calibration_verified"]),
            config.CALIBRATION_GROUP_CAMERA_UNVERIFIED)
        self.assertFalse(calib.is_baseline_usable(
            calib.calibration_group(
                calibration_source=row["calibration_source"],
                rod_length_cm=row["rod_length_cm"],
                calibration_verified=row["calibration_verified"])))

    def test_verified_camera_is_usable(self):
        row = {"calibration_source": config.CALIBRATION_SOURCE_CAMERA,
               "rod_length_cm": 10.0, "calibration_verified": True}
        self.assertEqual(calib.calibration_group(
            calibration_source=row["calibration_source"],
            rod_length_cm=row["rod_length_cm"],
            calibration_verified=row["calibration_verified"]),
            config.CALIBRATION_GROUP_CAMERA_VERIFIED)
        self.assertTrue(calib.is_baseline_usable(
            calib.calibration_group(
                calibration_source=row["calibration_source"],
                rod_length_cm=row["rod_length_cm"],
                calibration_verified=row["calibration_verified"])))

    def test_manual_is_usable(self):
        row = {"calibration_source": config.CALIBRATION_SOURCE_CSV,
               "rod_length_cm": None, "calibration_verified": None}
        self.assertEqual(calib.calibration_group(
            calibration_source=row["calibration_source"],
            rod_length_cm=row["rod_length_cm"],
            calibration_verified=row["calibration_verified"]),
            config.CALIBRATION_GROUP_MANUAL)
        self.assertTrue(calib.is_baseline_usable(
            calib.calibration_group(
                calibration_source=row["calibration_source"],
                rod_length_cm=row["rod_length_cm"],
                calibration_verified=row["calibration_verified"])))


class TestBaselineQuarantine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.conn = dbm.init_db(self.base / "c.db")
        self.repo = SQLiteRepository(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _plant(self, ref, cohort="reference"):
        return self.repo.register_plant(
            species="Sunflower (SuperOrange)", data_source="real",
            external_ref=ref, cohort=cohort)

    def test_unverified_camera_cannot_enter_baseline(self):
        # manual reference cohort
        pid = self._plant("m1", cohort="reference")
        self.repo.add_observation(plant_id=pid, observation_date="2083-05-02",
                                  age_days=7, height_cm=0.5,
                                  calibration_source=config.CALIBRATION_SOURCE_CSV)
        self.repo.add_observation(plant_id=pid, observation_date="2083-05-10",
                                  age_days=15, height_cm=2.0,
                                  calibration_source=config.CALIBRATION_SOURCE_CSV)
        # camera-unverified plant
        pid2 = self._plant("c1")
        self.repo.add_observation(plant_id=pid2, observation_date="2083-05-10",
                                  age_days=15, height_cm=2.0,
                                  rod_length_cm=10.0,
                                  calibration_source=config.CALIBRATION_SOURCE_CAMERA,
                                  calibration_verified=False)
        base = svc.build_reference_baseline(
            self.repo, "Sunflower (SuperOrange)", models_dir=self.base / "models")
        self.assertEqual(base["n_plants"], 1)

    def test_manual_reference_supports_provisional_prediction(self):
        for i in range(1, 5):
            p = self.repo.register_plant(
                species="Sunflower (SuperOrange)", data_source="real",
                external_ref=str(100 + i), cohort="reference")
            self.repo.add_observation(plant_id=p, observation_date="2083-05-02",
                                      age_days=7, height_cm=0.5,
                                      calibration_source=config.CALIBRATION_SOURCE_CSV)
            self.repo.add_observation(plant_id=p, observation_date="2083-05-10",
                                      age_days=15, height_cm=2.0,
                                      calibration_source=config.CALIBRATION_SOURCE_CSV)
        base = svc.build_reference_baseline(
            self.repo, "Sunflower (SuperOrange)", models_dir=self.base / "models")
        self.assertGreaterEqual(base["n_plants"], 3)
        pidm = self.repo.list_plants(
            species="Sunflower (SuperOrange)")[0]["plant_id"]
        res = svc.predict_for_plant(self.repo, pidm, self.base / "models",
                                    allow_provisional_baseline=True)
        self.assertTrue(res["available"])
        self.assertTrue(res["provisional"])

    def test_verification_and_reprocess_no_duplicates(self):
        # create a camera capture
        pid = self._plant("c1")
        cap = self.repo.add_capture(
            plant_id=pid, public_id="Plant01_20830510_100000",
            observation_date="2083-05-10", validity="valid",
            rod_length_cm=10.0,
            calibration_source=config.CALIBRATION_SOURCE_CAMERA,
            calibration_verified=False,
            height_cm=2.0, data_source="real")
        before = self.repo.count_captures()
        # reprocess (same public_id) should revise in place
        outcome = self.repo.add_capture(
            plant_id=pid, public_id="Plant01_20830510_100000",
            observation_date="2083-05-10", validity="valid",
            rod_length_cm=10.0,
            calibration_source=config.CALIBRATION_SOURCE_CAMERA,
            calibration_verified=True,
            height_cm=2.1, data_source="real")
        after = self.repo.count_captures()
        self.assertEqual(before, after)
        self.assertFalse(outcome["created"])
        self.assertTrue(outcome["changed"])
        # capture_revisions recorded
        revs = self.repo.capture_revisions(capture_id=outcome["capture_id"])
        self.assertGreaterEqual(len(revs), 1)


if __name__ == "__main__":
    unittest.main()