"""OpenCV measurement edge cases that must stay missing, not become zero."""

import unittest
from types import SimpleNamespace
from unittest import mock

import plant_measurement

from plant_measurement import (
    CaptureResult,
    calculate_measurements,
    capture_record_from_result,
)


class _UnreadableLedger:
    """A ledger path whose reads and writes fail like a flaky disk."""

    def exists(self) -> bool:
        return True

    def open(self, *args, **kwargs):
        raise OSError(5, "Input/output error")

    def __str__(self) -> str:
        return "/tmp/processed_cloudinary_ids.txt"


class TestCameraListingErrors(unittest.TestCase):
    def test_network_oserror_is_wrapped_with_context(self):
        # A dropped connection can raise a bare OSError (Errno 5) from
        # response.read(); it must not escape as an unlabelled message.
        with mock.patch.object(
            plant_measurement, "CLOUDINARY_CLOUD_NAME", "demo"
        ), mock.patch.object(
            plant_measurement, "CLOUDINARY_API_KEY", "key"
        ), mock.patch.object(
            plant_measurement, "CLOUDINARY_API_SECRET", "secret"
        ), mock.patch.object(
            plant_measurement.urllib.request,
            "urlopen",
            side_effect=OSError(5, "Input/output error"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                plant_measurement.get_cloudinary_images(prefix="Plant02_")

        message = str(caught.exception)
        self.assertIn("Could not contact Cloudinary", message)
        self.assertIn("Input/output error", message)

    def test_unreadable_ledger_produces_an_actionable_error(self):
        with mock.patch.object(
            plant_measurement, "PROCESSED_IDS_FILE", _UnreadableLedger()
        ):
            with self.assertRaises(RuntimeError) as caught:
                plant_measurement.get_processed_ids()

        self.assertIn("processed-capture ledger", str(caught.exception))
        self.assertIn("Input/output error", str(caught.exception))

    def test_ledger_write_failure_does_not_fail_the_measurement(self):
        with mock.patch.object(
            plant_measurement, "PROCESSED_IDS_FILE", _UnreadableLedger()
        ):
            # No exception: the capture itself succeeded.
            plant_measurement.mark_processed("Plant02_20261020_100000")


class TestStemWidthMeasurement(unittest.TestCase):
    def test_zero_detected_pixels_means_width_is_missing(self):
        plant = SimpleNamespace(
            height_top_y=10,
            height_base_y=30,
            stem_width_pixels=0,
        )
        reference = SimpleNamespace(width=20, height=100)

        scale, height, stem_width = calculate_measurements(
            plant, reference, rod_length_cm=10.0)

        self.assertAlmostEqual(scale, 0.1)
        self.assertAlmostEqual(height, 2.1)
        self.assertIsNone(stem_width)

    def test_positive_detected_pixels_produce_a_width(self):
        plant = SimpleNamespace(
            height_top_y=10,
            height_base_y=30,
            stem_width_pixels=3,
        )
        reference = SimpleNamespace(width=20, height=100)

        _, _, stem_width = calculate_measurements(
            plant, reference, rod_length_cm=10.0)

        self.assertAlmostEqual(stem_width, 0.3)

    def test_legacy_zero_width_is_normalized_to_missing(self):
        result = CaptureResult(
            timestamp="2026-10-20T11:00:00",
            height_cm=7.39,
            width_cm=0.0,
            leaf_count=8,
            cm_per_pixel=0.0094,
            confidence=0.91,
        )

        record = capture_record_from_result(result)

        self.assertEqual(record.validity, "valid")
        self.assertIsNone(record.width_cm)


if __name__ == "__main__":
    unittest.main()
