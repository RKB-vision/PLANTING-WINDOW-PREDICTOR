"""Tests: species-aware fallback order and data-source separation."""

import tempfile
import unittest
from pathlib import Path

from plantreadiness import config
from plantreadiness import model as mdl


def _make_artifact(species, variety, data_source, validated=True,
                   mae=2.0, scope_species=None, scope_variety=None):
    meta = {
        "training_data_source": data_source,
        "is_synthetic": data_source == "synthetic",
        "supported_species": [species],
        "supported_varieties": [variety] if variety else [],
        "scope_species": scope_species if scope_species is not None else species,
        "scope_variety": scope_variety if scope_variety is not None else variety,
        "target_definition": config.TARGET_DEFINITION,
        "feature_list": list(mdl.FEATURE_COLUMNS),
        "training_date": "2026-10-13T00:00:00+00:00",
        "validation_method": "GroupKFold by plant_id",
        "validation_metrics": {"mae_days": mae, "mae_days_std": 0.4,
                               "baseline_mean_mae_days": 5.0},
        "n_independent_plants": 12,
        "n_labelled_readiness_events": 12,
        "validated": validated,
        "model_name": "ridge",
        "limitations": "test artifact",
    }
    return {"model": None, "metadata": meta}


def _register(models_dir, data_source, entries):
    """Write synthetic registry entries without training (fast unit tests)."""
    import json
    d = Path(models_dir) / data_source
    d.mkdir(parents=True, exist_ok=True)
    payload = {"data_source": data_source,
               "artifacts": [{"file": f"{i}.joblib", "metadata": m,
                              "active": True}
                             for i, m in enumerate(entries)]}
    (d / "registry.json").write_text(json.dumps(payload))
    for i in range(len(entries)):
        (d / f"{i}.joblib").touch()


class TestFallbackOrder(unittest.TestCase):
    """Level 1 -> 2 -> 3 (explicit) -> 4, per task section 8."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_level3_when_no_models(self):
        sel = mdl.select_model("Super Orange", "Early", self.models)
        self.assertEqual(sel["level"], 3)
        self.assertEqual(sel["message"], config.UNAVAILABLE_MESSAGE)

    def test_level1_exact_species_variety_real_model(self):
        _register(self.models, "real", [
            _make_artifact("Super Orange", "Early", "real")["metadata"],
            _make_artifact("Super Orange", "Late", "real")["metadata"],
        ])
        sel = mdl.select_model("Super Orange", "Early", self.models)
        self.assertEqual(sel["level"], 1)
        self.assertEqual(sel["data_source"], "real")

    def test_level2_when_variety_model_missing(self):
        _register(self.models, "real", [
            _make_artifact("Super Orange", None, "real",
                           scope_variety=None)["metadata"],
        ])
        sel = mdl.select_model("Super Orange", "Early", self.models)
        self.assertEqual(sel["level"], 2)

    def test_level2_multispecies_model_covers_other_species(self):
        meta = _make_artifact("Demo Bean", "Bush", "real")["metadata"]
        meta["supported_species"] = ["Super Orange", "Demo Bean"]
        meta["scope_species"] = None   # broad model
        _register(self.models, "real", [meta])
        sel = mdl.select_model("Super Orange", "Early", self.models)
        self.assertEqual(sel["level"], 2)

    def test_unvalidated_model_is_skipped(self):
        _register(self.models, "real", [
            _make_artifact("Super Orange", "Early", "real",
                           validated=False)["metadata"],
        ])
        sel = mdl.select_model("Super Orange", "Early", self.models)
        self.assertEqual(sel["level"], 3)

    def test_unknown_species_is_level3(self):
        _register(self.models, "real", [
            _make_artifact("Super Orange", "Early", "real")["metadata"],
        ])
        sel = mdl.select_model("Tomato", "Roma", self.models)
        self.assertEqual(sel["level"], 3)




    def test_retired_artifacts_not_selected(self):
        meta = _make_artifact("Super Orange", "Early", "real")["metadata"]
        import json
        d = self.models / "real"
        d.mkdir(parents=True, exist_ok=True)
        (d / "registry.json").write_text(json.dumps(
            {"data_source": "real",
             "artifacts": [{"file": "old.joblib", "metadata": meta,
                            "active": False}]}))
        sel = mdl.select_model("Super Orange", "Early", self.models)
        self.assertEqual(sel["level"], 3)

    def test_deleted_latest_artifact_falls_back_to_older_model(self):
        _register(self.models, "real", [
            _make_artifact("Super Orange", "Early", "real")["metadata"],
            _make_artifact("Super Orange", "Early", "real",
                           mae=1.0)["metadata"],
        ])
        (self.models / "real" / "1.joblib").unlink()

        sel = mdl.select_model("Super Orange", "Early", self.models)

        self.assertEqual(sel["level"], 1)
        self.assertEqual(sel["entry"]["file"], "0.joblib")

    def test_no_existing_artifact_is_treated_as_no_model(self):
        _register(self.models, "real", [
            _make_artifact("Super Orange", "Early", "real")["metadata"],
        ])
        (self.models / "real" / "0.joblib").unlink()

        sel = mdl.select_model("Super Orange", "Early", self.models)

        self.assertEqual(sel["level"], 3)
        self.assertIsNone(sel["entry"])

    def test_corrupt_selected_artifact_degrades_to_no_model(self):
        _register(self.models, "real", [
            _make_artifact("Super Orange", "Early", "real")["metadata"],
        ])
        (self.models / "real" / "0.joblib").write_text("not a joblib artifact")
        sel = mdl.select_model("Super Orange", "Early", self.models)

        loaded = mdl.load_selection(sel, self.models)

        self.assertEqual(loaded["level"], 3)
        self.assertIsNone(loaded["artifact"])
        self.assertIn("could not be loaded", loaded["model_warning"])



class TestTrainingGuards(unittest.TestCase):
    """Minimum-data requirements must block premature training."""

    def test_empty_frame_raises_insufficient(self):
        df = mdl.build_training_frame([])
        with self.assertRaises(mdl.InsufficientDataError):
            mdl.check_min_requirements(df, "test data")

    def test_too_few_plants_raises_insufficient(self):
        rows = [
            {"plant_id": i, "species": "Super Orange", "variety": "Early",
             "age_days": 10, "height_cm": 3.0, "growth_rate_cm_per_day": 0.3,
             "stem_width_cm": None, "visible_true_leaf_count": None,
             "days_remaining": 5}
            for i in range(1, 4)   # only 3 plants < MIN_INDEPENDENT_PLANTS
        ]
        df = mdl.build_training_frame(rows)
        with self.assertRaises(mdl.InsufficientDataError):
            mdl.check_min_requirements(df, "test data")

    def test_build_training_frame_keeps_target(self):
        rows = [
            {"plant_id": 1, "species": "Super Orange", "variety": None,
             "age_days": 10, "height_cm": 3.0, "growth_rate_cm_per_day": 0.3,
             "stem_width_cm": None, "visible_true_leaf_count": None,
             "days_remaining": 7},
        ]
        df = mdl.build_training_frame(rows)
        self.assertEqual(df.iloc[0]["target_days_remaining"], 7)
        self.assertEqual(df.iloc[0]["variety"], "unknown")


if __name__ == "__main__":
    unittest.main()
