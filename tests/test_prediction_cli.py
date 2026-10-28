"""Production prediction CLIs must not expose synthetic prediction opt-ins."""

import contextlib
import io
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import plant_feedback


class TestPredictionCLI(unittest.TestCase):
    def test_predict_command_has_no_synthetic_opt_in(self):
        args = plant_feedback.build_parser().parse_args(
            ["predict", "--plant", "7"])

        self.assertFalse(hasattr(args, "allow_synthetic"))

    @mock.patch("plant_feedback._banner")
    @mock.patch("prediction.format_result", return_value="prediction output")
    @mock.patch("prediction.predict_for_plant", return_value={"available": False})
    def test_predict_command_calls_real_prediction_wrapper(
        self, predict_for_plant, _format_result, _banner
    ):
        conn = object()
        args = Namespace(plant=7, models_dir="models")

        with contextlib.redirect_stdout(io.StringIO()):
            status = plant_feedback.cmd_predict(conn, args)

        self.assertEqual(status, 0)
        predict_for_plant.assert_called_once_with(conn, 7, Path("models"))


if __name__ == "__main__":
    unittest.main()
