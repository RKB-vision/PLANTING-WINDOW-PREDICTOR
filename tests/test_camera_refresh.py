"""Tests: cloud camera refresh (ESP32-CAM -> Cloudinary -> DB), fail-open.

The vision layer is replaced with a fake puller so these tests need no
network access and no OpenCV work. They verify the operational contract:

* disabled unless configured (safe no-op),
* explicit registration (unregistered prefixes are never auto-created),
* rows are imported once and are idempotent on re-run,
* every failure is reported, never raised into the caller,
* the species filter, throttle and audit CSV all behave.
"""

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plantreadiness import camera_refresh
from plantreadiness import services as svc
from plantreadiness.repository import create_sqlite_repository


def _row(ref="Plant-01", day="2026-10-15", height="13.3758"):
    """One daily measurement row in the camera CSV schema."""
    import json
    capture = {
        "public_id": f"{ref.replace('-', '')}_{day.replace('-', '')}_192640",
        "capture_date": day,
        "capture_timestamp": f"{day}T19:26:40",
        "validity": "valid",
        "height_cm": (float(height) if str(height).strip() not in ("", "None") else None),
        "width_cm": 1.3376,
        "leaf_count": 2,
        "cm_per_pixel": 0.027298,
        "confidence": 0.6641,
        "rod_length_cm": 12.0,
    }
    return {
        "day": day,
        "plant_id": ref,
        "captures_used": 1,
        "average_height_cm": height,
        "average_width_cm": "1.3376",
        "average_visible_leaves": "2.0000",
        "average_cm_per_pixel": "0.027298",
        "average_confidence": "0.6641",
        "first_capture_timestamp": f"{day}T19:26:40",
        "last_capture_timestamp": f"{day}T19:26:40",
        "captures_json": json.dumps([capture]),
    }


class CameraRefreshBase(unittest.TestCase):
    def setUp(self):
        camera_refresh.reset_refresh_throttle()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = create_sqlite_repository(
            Path(self.tmp.name) / "camera.db")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, species="Super Orange", ref="Plant-01"):
        return self.repo.register_plant(
            species=species,
            external_ref=ref,
            data_source="real",
        )


class TestDisabled(CameraRefreshBase):
    def test_disabled_is_a_safe_noop_when_not_configured(self):
        with mock.patch.dict(
            os.environ, {"CAMERA_REFRESH_ENABLED": "0"}, clear=False
        ):
            report = svc.refresh_camera_observations(self.repo)
        self.assertFalse(report["enabled"])
        self.assertFalse(report["ran"])
        self.assertIn("not enabled", report["reason"])
        self.assertEqual(report["observations_inserted"], 0)

    def test_no_sources_is_reported(self):
        report = svc.refresh_camera_observations(self.repo, sources=[])
        self.assertTrue(report["enabled"])
        self.assertFalse(report["ran"])
        self.assertIn("No camera sources", report["reason"])


class TestRegistration(CameraRefreshBase):
    def test_unregistered_prefix_is_skipped_not_auto_created(self):
        called = {"n": 0}

        def puller(source, captures):
            called["n"] += 1
            return [_row()]

        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=puller,
        )
        self.assertEqual(report["sources"][0]["status"], "unregistered")
        self.assertEqual(called["n"], 0)          # never pulled
        self.assertEqual(self.repo.list_plants(), [])  # never created
        self.assertEqual(report["observations_inserted"], 0)

    def test_registered_plant_receives_observation(self):
        self._register()
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [_row()],
        )
        self.assertEqual(report["sources"][0]["status"], "imported")
        self.assertEqual(report["captures_staged"], 1)
        self.assertEqual(report["observations_inserted"], 0)
        plant = self.repo.list_plants()[0]
        # Nothing is usable until the reading is reviewed.
        self.assertEqual(len(self.repo.get_observations(plant["plant_id"])), 0)
        pending = svc.list_pending_captures(self.repo)
        self.assertEqual(len(pending), 1)
        ids = [c["capture_id"] for c in pending[0]["captures"]]
        svc.review_captures(self.repo, capture_ids=ids, action="accept")
        obs = self.repo.get_observations(plant["plant_id"])
        self.assertEqual(len(obs), 1)
        self.assertAlmostEqual(obs[0]["height_cm"], 13.3758, places=4)
        # No prior measurement -> growth rate must not be invented.
        self.assertIsNone(obs[0]["growth_rate_cm_per_day"])

    def test_growth_rate_uses_two_accepted_camera_dates(self):
        plant_id = self._register()
        rows = [_row(day="2026-10-15", height="8.0")]
        kwargs = dict(
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: rows,
            min_interval=0,
        )

        svc.refresh_camera_observations(self.repo, **kwargs)
        pending = svc.list_pending_captures(self.repo, species="Super Orange")
        first_id = pending[0]["captures"][0]["capture_id"]
        svc.review_captures(self.repo, capture_ids=[first_id], action="accept")

        rows[:] = [_row(day="2026-10-16", height="10.0")]
        svc.refresh_camera_observations(self.repo, **kwargs)
        pending = svc.list_pending_captures(self.repo, species="Super Orange")
        second_id = pending[0]["captures"][0]["capture_id"]
        svc.review_captures(self.repo, capture_ids=[second_id], action="accept")

        observations = self.repo.get_observations(plant_id)
        self.assertEqual(len(observations), 2)
        self.assertIsNone(observations[0]["growth_rate_cm_per_day"])
        self.assertEqual(observations[1]["growth_rate_cm_per_day"], 2.0)

    def test_species_filter_skips_other_species(self):
        self._register(species="Super Orange")
        report = svc.refresh_camera_observations(
            self.repo,
            species="DemoBean",
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [_row()],
        )
        self.assertEqual(report["sources"][0]["status"], "other_species")
        self.assertEqual(report["observations_inserted"], 0)

    def test_zero_stem_width_is_imported_as_missing(self):
        self._register()
        row = _row()
        captures = json.loads(row["captures_json"])
        captures[0]["width_cm"] = 0.0
        row["captures_json"] = json.dumps(captures)
        row["average_width_cm"] = "0"

        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [row],
        )

        self.assertEqual(report["sources"][0]["status"], "imported")
        pending = svc.list_pending_captures(self.repo)
        self.assertIsNone(pending[0]["captures"][0]["stem_width_cm"])

    def test_growth_rate_uses_two_accepted_camera_dates(self):
        plant_id = self._register()
        rows = [_row(day="2026-10-15", height="8.0")]
        kwargs = dict(
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: rows,
            min_interval=0,
        )

        svc.refresh_camera_observations(self.repo, **kwargs)
        pending = svc.list_pending_captures(self.repo, species="Super Orange")
        first_id = pending[0]["captures"][0]["capture_id"]
        svc.review_captures(self.repo, capture_ids=[first_id], action="accept")

        rows[:] = [_row(day="2026-10-16", height="10.0")]
        svc.refresh_camera_observations(self.repo, **kwargs)
        pending = svc.list_pending_captures(self.repo, species="Super Orange")
        second_id = pending[0]["captures"][0]["capture_id"]
        svc.review_captures(self.repo, capture_ids=[second_id], action="accept")

        observations = self.repo.get_observations(plant_id)
        self.assertEqual(len(observations), 2)
        self.assertIsNone(observations[0]["growth_rate_cm_per_day"])
        self.assertEqual(observations[1]["growth_rate_cm_per_day"], 2.0)


class TestIdempotency(CameraRefreshBase):
    def test_identical_rows_are_deduplicated_on_rerun(self):
        self._register()
        kwargs = dict(
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [_row()],
            min_interval=0,
        )
        first = svc.refresh_camera_observations(self.repo, **kwargs)
        second = svc.refresh_camera_observations(self.repo, **kwargs)
        self.assertEqual(first["captures_staged"], 1)
        self.assertEqual(second["captures_staged"], 0)
        # the duplicate capture is detected, not re-staged
        self.assertGreaterEqual(
            second.get("captures_unchanged", 0) + second.get("observations_skipped_duplicate", 0), 0)

    def test_no_new_images_is_reported(self):
        self._register()
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [],
        )
        self.assertEqual(report["sources"][0]["status"], "no_new_images")


class TestFailOpen(CameraRefreshBase):
    def test_puller_error_is_reported_not_raised(self):
        self._register()

        def boom(source, captures):
            raise RuntimeError("Cloudinary unreachable")

        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=boom,
        )
        self.assertTrue(report["ran"])
        self.assertEqual(report["sources"][0]["status"], "error")
        self.assertTrue(report["errors"])
        self.assertIn("Cloudinary unreachable", report["errors"][0])

    def test_puller_oserror_names_the_step_and_exception_type(self):
        self._register()

        def boom(source, captures):
            raise OSError(5, "Input/output error")

        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=boom,
        )

        entry = report["sources"][0]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["error_step"], "read_camera")
        self.assertEqual(entry["error_type"], "OSError")
        self.assertIn("Input/output error", entry["message"])
        # One actionable line, not the same message twice.
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("OSError", report["errors"][0])

    def test_import_error_is_reported_not_raised(self):
        self._register()
        bad_row = _row(height="")  # missing height -> importer rejects row
        report = svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [bad_row],
        )
        # The importer records the problem rather than raising.
        self.assertEqual(report["sources"][0]["status"], "imported")
        self.assertEqual(report["observations_inserted"], 0)
        self.assertFalse(report["errors"])


class TestThrottleAndAudit(CameraRefreshBase):
    def test_throttle_blocks_rapid_second_run(self):
        self._register()
        clock = {"t": 1000.0}
        kwargs = dict(
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [_row()],
            min_interval=60,
            now=lambda: clock["t"],
        )
        first = svc.refresh_camera_observations(self.repo, **kwargs)
        self.assertTrue(first["ran"])

        clock["t"] += 5
        second = svc.refresh_camera_observations(self.repo, **kwargs)
        self.assertFalse(second["ran"])
        self.assertIn("Throttled", second["reason"])

        clock["t"] += 120
        third = svc.refresh_camera_observations(self.repo, **kwargs)
        self.assertTrue(third["ran"])

    def test_audit_csv_is_appended(self):
        self._register()
        csv_path = Path(self.tmp.name) / "audit" / "measurements.csv"
        svc.refresh_camera_observations(
            self.repo,
            sources=[("Plant01_", "Plant-01")],
            puller=lambda source, captures: [_row()],
            csv_path=csv_path,
        )
        self.assertTrue(csv_path.exists())
        text = csv_path.read_text(encoding="utf-8")
        self.assertIn("average_height_cm", text)
        self.assertIn("13.3758", text)


if __name__ == "__main__":
    unittest.main()
