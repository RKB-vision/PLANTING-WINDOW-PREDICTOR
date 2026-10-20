"""Tests: ESP32-CAM Plant ID -> registered plant resolution.

Verifies that camera measurements are attached to the correct registered plant
(species + variety come from the DB record, never from the ID), that harmless
ID spelling variations resolve consistently, that unknown/ambiguous IDs are
quarantined, and that four captures on one day produce one daily aggregate.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plantreadiness import camera_import
from plantreadiness import camera_refresh
from plantreadiness import services as svc
from plantreadiness.repository import create_sqlite_repository
from plantreadiness import config

import plant_measurement as pm


def _row(ref="Plant-01", day="2026-10-15", height="13.3758"):
    return {
        "day": day,
        "plant_id": ref,
        "captures_used": 4,
        "average_height_cm": height,
        "average_width_cm": "1.3376",
        "average_visible_leaves": "2.0000",
        "average_cm_per_pixel": "0.027298",
        "average_confidence": "0.6641",
        "first_capture_timestamp": f"{day}T19:26:40",
        "last_capture_timestamp": f"{day}T19:26:40",
    }


class PlantIdResolutionBase(unittest.TestCase):
    def setUp(self):
        camera_refresh.reset_refresh_throttle()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.repo = create_sqlite_repository(self.db_path)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, species, variety, ref, **kw):
        return self.repo.register_plant(
            species=species, variety=variety, external_ref=ref,
            data_source="real", **kw)


class TestBasicResolution(PlantIdResolutionBase):
    def test_plant01_receives_own_measurements(self):
        pid1 = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        pid2 = self._register("Marigold (Apsara Yellow)", "Apsara Yellow", "Plant02")
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant01")],
            puller=lambda s, c: [_row(ref="Plant01", height="10.0")],
            min_interval=0,
        )
        self.assertEqual(report["sources"][0]["status"], "imported")
        obs1 = self.repo.get_observations(pid1)
        obs2 = self.repo.get_observations(pid2)
        self.assertEqual(len(obs1), 1)
        self.assertEqual(len(obs2), 0)
        self.assertAlmostEqual(obs1[0]["height_cm"], 10.0, places=4)
        self.assertEqual(obs1[0]["data_source"], "real")

    def test_plant02_receives_own_measurements(self):
        pid1 = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        pid2 = self._register("Marigold (Apsara Yellow)", "Apsara Yellow", "Plant02")
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant02_", "Plant02")],
            puller=lambda s, c: [_row(ref="Plant02", height="5.0")],
            min_interval=0,
        )
        self.assertEqual(report["sources"][0]["status"], "imported")
        obs1 = self.repo.get_observations(pid1)
        obs2 = self.repo.get_observations(pid2)
        self.assertEqual(len(obs1), 0)
        self.assertEqual(len(obs2), 1)
        self.assertAlmostEqual(obs2[0]["height_cm"], 5.0, places=4)

    def test_unknown_id_is_not_assigned(self):
        self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant99_", "Plant99")],
            puller=lambda s, c: [_row(ref="Plant99", height="7.0")],
            min_interval=0,
        )
        entry = report["sources"][0]
        self.assertIn(entry["status"], ("unregistered", "imported"))
        # Either way no observation should be attached to Plant01.
        pid1 = self.repo.list_plants()[0]["plant_id"]
        self.assertEqual(self.repo.get_observations(pid1), [])
        # No plant auto-created.
        self.assertEqual(len(self.repo.list_plants()), 1)

    def test_species_comes_from_registered_record(self):
        pid = self._register("Marigold (Apsara Yellow)", "Apsara Yellow", "Plant02")
        plant = self.repo.get_plant(pid)
        self.assertEqual(plant["species"], "Marigold (Apsara Yellow)")
        self.assertEqual(plant["variety"], "Apsara Yellow")


class TestNormalization(PlantIdResolutionBase):
    def test_equivalent_spellings_resolve(self):
        pid = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant-01")
        for spelling in ("Plant01", "plant01", "Plant-01", "Plant_01", "Plant 01"):
            # normalize_plant_ref collapses them all
            self.assertEqual(config.normalize_plant_ref(spelling),
                             config.normalize_plant_ref("Plant-01"))
        # camera_import resolution: feed a row whose plant_id uses a different spelling
        report = camera_import.import_camera_measurements(
            self.repo,
            __import__("io").StringIO(
                "plant_id,average_height_cm,first_capture_timestamp\n"
                f"Plant01,12.0,2026-10-15T10:00:00\n"),
            auto_register=False, data_source="real",
        )
        self.assertEqual(report["observations_inserted"], 1)
        obs = self.repo.get_observations(pid)
        self.assertEqual(len(obs), 1)
        self.assertAlmostEqual(obs[0]["height_cm"], 12.0, places=4)

    def test_zero_padding_is_significant(self):
        self.assertEqual(config.normalize_plant_ref("Plant01"),
                         config.normalize_plant_ref("Plant-01"))
        self.assertNotEqual(config.normalize_plant_ref("Plant01"),
                            config.normalize_plant_ref("Plant1"))
        self.assertNotEqual(config.normalize_plant_ref("Plant01"),
                            config.normalize_plant_ref("Plant001"))

    def test_ambiguous_mapping_is_quarantined(self):
        self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant-01")
        self._register("Marigold (Apsara Yellow)", "Apsara Yellow", "Plant_01")
        # Both normalize to "plant01" -> ambiguous.
        report = camera_import.import_camera_measurements(
            self.repo,
            __import__("io").StringIO(
                "plant_id,average_height_cm,first_capture_timestamp\n"
                "Plant01,12.0,2026-10-15T10:00:00\n"),
            auto_register=False, data_source="real",
        )
        self.assertIn("Plant01", report["unmatched_plants"])
        self.assertTrue(any("Ambiguous" in i["problem"] for i in report["issues"]))
        # No observation created for either plant.
        for p in self.repo.list_plants():
            self.assertEqual(self.repo.get_observations(p["plant_id"]), [])


class TestIdempotency(PlantIdResolutionBase):
    def test_reprocessing_same_capture_is_idempotent(self):
        self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        kwargs = dict(
            sources=[("Plant01_", "Plant01")],
            puller=lambda source, captures: [_row(ref="Plant01")],
            min_interval=0,
        )
        first = svc.refresh_camera_observations(self.repo, **kwargs)
        second = svc.refresh_camera_observations(self.repo, **kwargs)
        self.assertEqual(first["observations_inserted"], 1)
        self.assertEqual(second["observations_inserted"], 0)
        self.assertEqual(second["observations_skipped_duplicate"], 1)
        self.assertEqual(len(self.repo.get_observations(
            self.repo.list_plants()[0]["plant_id"])), 1)

    def test_refresh_quarantines_ambiguous_source(self):
        self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant-01")
        self._register("Marigold (Apsara Yellow)", "Apsara Yellow", "Plant_01")
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant01")],
            puller=lambda s, c: [_row(ref="Plant01")],
            min_interval=0,
        )
        entry = report["sources"][0]
        self.assertEqual(entry["status"], "ambiguous")
        self.assertEqual(report["observations_inserted"], 0)


class TestAggregation(PlantIdResolutionBase):
    def test_four_captures_one_day_one_observation(self):
        records = []
        for i in range(4):
            records.append(pm.CaptureRecord(
                public_id=f"Plant01_20261015_09{i:02d}00",
                capture_date="2026-10-15",
                capture_timestamp=f"2026-10-15T09:{i:02d}:00",
                validity="valid",
                height_cm=10.0 + i,
                width_cm=1.0,
                leaf_count=2,
                cm_per_pixel=0.027,
                confidence=0.6,
                rod_length_cm=10.0,
            ))
        grouped = pm.group_records_by_date(records)
        self.assertEqual(len(grouped), 1)
        day, grp = next(iter(grouped.items()))
        self.assertEqual(day, "2026-10-15")
        self.assertEqual(len(grp), 4)
        row = pm.median_daily_record("Day-01", "Plant01", day, grp,
                                     rod_length_cm=10.0)
        self.assertEqual(row["captures_used"], 4)
        # mean/median height of [10,11,12,13] = 11.5
        self.assertAlmostEqual(float(row["average_height_cm"]), 11.5, places=3)
        self.assertAlmostEqual(float(row["median_height_cm"]), 11.5, places=3)
        self.assertEqual(row["plant_id"], "Plant01")


class TestCalibrationProvenance(PlantIdResolutionBase):
    def test_unverified_camera_data_quarantined_from_baseline(self):
        pid = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant01")],
            puller=lambda s, c: [_row(ref="Plant01")],
            min_interval=0,
        )
        self.assertEqual(report["observations_inserted"], 1)
        obs = self.repo.get_observations(pid)[0]
        # camera rows carry calibration_source='camera_configured' (unverified)
        self.assertEqual(obs["calibration_source"], "camera_configured")
        # rod length defaults to configured value
        self.assertIsNotNone(obs["rod_length_cm"])
        # unverified -> not baseline-usable (camera_unverified group)
        from plantreadiness import calibration as cal
        grp = cal.calibration_group(
            calibration_source=obs["calibration_source"],
            rod_length_cm=obs["rod_length_cm"],
            calibration_verified=obs.get("calibration_verified"),
        )
        self.assertFalse(cal.is_baseline_usable(grp))


class TestSpeciesCohortIsolation(PlantIdResolutionBase):
    def test_predictions_use_correct_species_cohort(self):
        self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        self._register("Marigold (Apsara Yellow)", "Apsara Yellow", "Plant02")
        # import measurements for each under its own species via camera refresh
        svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant01")],
            puller=lambda s, c: [_row(ref="Plant01", height="10.0")],
            min_interval=0,
        )
        svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant02_", "Plant02")],
            puller=lambda s, c: [_row(ref="Plant02", height="5.0")],
            min_interval=0,
        )
        # species cohort snapshot for Sunflower should only reflect Plant01
        snap = self.repo.species_cohort_snapshot(
            "Sunflower (SuperOrange)", variety=None)
        self.assertEqual(snap["plants_represented"], 1)
        heights = [r["height_cm"] for r in snap["rows"]]
        self.assertAlmostEqual(sorted(heights)[len(heights)//2], 10.0, places=3) \
            if len(heights) == 1 else None
        self.assertAlmostEqual(heights[0], 10.0, places=3)
        snap2 = self.repo.species_cohort_snapshot(
            "Marigold (Apsara Yellow)", variety=None)
        self.assertEqual(snap2["plants_represented"], 1)
        self.assertAlmostEqual(snap2["rows"][0]["height_cm"], 5.0, places=3)


class TestLiveVsHistoricalSeparation(PlantIdResolutionBase):
    def test_historical_and_live_plant01_are_distinct(self):
        hist = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01",
                              cohort="reference")
        live = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        self.assertNotEqual(hist, live)
        self.assertEqual(self.repo.get_plant(hist)["cohort"], "reference")
        self.assertNotEqual(self.repo.get_plant(live)["cohort"], "reference")

    def test_camera_resolves_to_live_not_historical(self):
        hist = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01",
                              cohort="reference")
        live = self._register("Sunflower (SuperOrange)", "SuperOrange", "Plant01")
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant01")],
            puller=lambda s, c: [_row(ref="Plant01")],
            min_interval=0,
        )
        self.assertEqual(report["sources"][0]["status"], "imported")
        self.assertEqual(len(self.repo.get_observations(hist)), 0)
        self.assertEqual(len(self.repo.get_observations(live)), 1)

    def test_idempotent_reimport_same_csv(self):
        # Simulate importing the same CSV twice -> same plants, no duplicates.
        conn = self.repo.conn
        import plantreadiness.csv_import as ci
        import tempfile, os
        csv_path = os.path.join(self.tmp.name, "t.csv")
        with open(csv_path, "w") as f:
            f.write("Date,Age,Plant_ID,Height_cm\n")
            f.write("2083-05-02,2,Plant01,1.0\n")
            f.write("2083-05-03,3,Plant01,1.5\n")
        r1 = ci.import_csv(conn, csv_path, species="Sunflower (SuperOrange)",
                           variety="SuperOrange", cohort="reference")
        r2 = ci.import_csv(conn, csv_path, species="Sunflower (SuperOrange)",
                           variety="SuperOrange", cohort="reference")
        self.assertEqual(r1["plants_imported"], 1)
        self.assertEqual(r2["plants_imported"], 0)
        self.assertEqual(r2["plants_existing"], 1)
        self.assertEqual(r2["observations_skipped_duplicate"], 2)


if __name__ == "__main__":
    unittest.main()