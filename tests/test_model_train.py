"""The real training CLI must scope stored labels to the requested species."""

import unittest
from unittest import mock

import model_train
from plantreadiness import config


class TestRealTrainingCLI(unittest.TestCase):
    @mock.patch("model_train.run_real_training", return_value={"trained": False})
    def test_custom_species_uses_db_without_importing_default_csv(
        self, run_real_training
    ):
        species = config.SPECIES_MARIGOLD_APSARA_YELLOW

        status = model_train.main([
            "--mode", "real", "--species", species,
        ])

        self.assertEqual(status, 0)
        args, kwargs = run_real_training.call_args
        self.assertIsNone(args[2])
        self.assertEqual(kwargs["species"], species)
        self.assertFalse(kwargs["retrain"])

    @mock.patch("model_train.run_real_training", return_value={"trained": False})
    def test_default_species_keeps_default_csv_import(self, run_real_training):
        model_train.main(["--mode", "real"])

        args, kwargs = run_real_training.call_args
        self.assertEqual(args[2], config.DEFAULT_REAL_CSV)
        self.assertEqual(kwargs["species"], config.DEFAULT_SPECIES)

    @mock.patch("model_train.run_real_training", return_value={"trained": False})
    def test_real_retrain_flag_is_forwarded(self, run_real_training):
        species = config.SPECIES_MARIGOLD_APSARA_YELLOW

        model_train.main([
            "--mode", "real", "--species", species, "--retrain",
        ])

        self.assertTrue(run_real_training.call_args.kwargs["retrain"])


class TestRealRetrainDecision(unittest.TestCase):
    def setUp(self):
        self.current = {
            "model_name": "random_forest",
            "training_plant_ids": [1, 2, 3, 4],
            "n_training_rows": 40,
            "n_labelled_readiness_events": 4,
        }
        self.candidate = {
            "model_name": "random_forest",
            "training_plant_ids": [1, 2, 3, 4, 5],
            "n_training_rows": 50,
            "n_labelled_readiness_events": 5,
            "validated": True,
        }

    def test_same_model_can_refresh_with_more_data_without_error_regression(self):
        decision = model_train.real_retrain_decision(
            self.current, self.candidate, 1.2, 1.2)

        self.assertTrue(decision["promote"])

    def test_different_model_type_must_strictly_improve_cv(self):
        self.candidate["model_name"] = "ridge"

        tied = model_train.real_retrain_decision(
            self.current, self.candidate, 1.2, 1.2)
        better = model_train.real_retrain_decision(
            self.current, self.candidate, 1.2, 1.1)

        self.assertFalse(tied["promote"])
        self.assertTrue(better["promote"])

    def test_no_new_evidence_keeps_current_model(self):
        self.candidate["training_plant_ids"] = [1, 2, 3, 4]
        self.candidate["n_training_rows"] = 40
        self.candidate["n_labelled_readiness_events"] = 4

        decision = model_train.real_retrain_decision(
            self.current, self.candidate, 1.2, 0.8)

        self.assertFalse(decision["promote"])


if __name__ == "__main__":
    unittest.main()
