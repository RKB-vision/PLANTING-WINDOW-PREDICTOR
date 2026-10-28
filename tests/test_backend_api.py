"""Tests: Flask backend API for the reference baseline + full feedback flow.

Uses the Flask test client against a temporary SQLite database so the real
``plant_readiness.db`` is never touched.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import app as backend  # noqa: E402
from plantreadiness import db as dbm  # noqa: E402
from plantreadiness.repository import SQLiteRepository  # noqa: E402


class BackendApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        backend.DB_PATH = self.base / "api.db"
        backend.MODELS_DIR = self.base / "models"
        self.client = backend.app.test_client()
        self.conn = dbm.init_db(backend.DB_PATH)
        self.repo = SQLiteRepository(self.conn)
        for i in range(1, 5):
            pid = self.repo.register_plant(
                species="Super Orange", data_source="real",
                external_ref=str(i))
            self.repo.add_observation(plant_id=pid,
                                      observation_date="2083-05-02",
                                      age_days=7, height_cm=0.5,
                                      calibration_source="csv_import_no_calibration")
            self.repo.add_observation(plant_id=pid,
                                      observation_date="2083-05-10",
                                      age_days=15, height_cm=2.0,
                                      calibration_source="csv_import_no_calibration")
        self.plant_id = self.repo.list_plants(
            species="Super Orange")[0]["plant_id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestReferenceApi(BackendApiBase):
    def test_build_and_get_reference(self):
        resp = self.client.post("/api/reference/build",
                                json={"species": "Super Orange"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["n_plants"], 4)
        self.assertIn("by_age_days", body)

        resp = self.client.get("/api/reference?species=Super Orange")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["n_plants"], 4)

    def test_missing_baseline_is_404(self):
        resp = self.client.get("/api/reference?species=Unknown")
        self.assertEqual(resp.status_code, 404)

    def test_species_requires_name(self):
        self.assertEqual(
            self.client.get("/api/reference").status_code, 400)


class TestPredictAndFeedbackApi(BackendApiBase):
    def test_species_prediction_is_provisional_without_a_model(self):
        self.client.post("/api/reference/build",
                         json={"species": "Super Orange"})
        resp = self.client.post("/api/predict/species",
                                json={"species": "Super Orange"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["available"])
        self.assertTrue(body["provisional"])
        self.assertEqual(body["method"], "historical_reference_baseline")

    def test_species_prediction_uses_baseline_when_registry_model_was_deleted(self):
        self.client.post("/api/reference/build",
                         json={"species": "Super Orange"})
        real_dir = backend.MODELS_DIR / "real"
        real_dir.mkdir(parents=True, exist_ok=True)
        (real_dir / "registry.json").write_text(json.dumps({
            "data_source": "real",
            "artifacts": [{
                "file": "deleted-model.joblib",
                "active": True,
                "metadata": {
                    "validated": True,
                    "scope_species": "Super Orange",
                    "scope_variety": "Early",
                    "supported_species": ["Super Orange"],
                },
            }],
        }))

        resp = self.client.post("/api/predict/species",
                                json={"species": "Super Orange"})

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["available"])
        self.assertTrue(body["provisional"])
        self.assertEqual(body["method"], "historical_reference_baseline")
        self.assertIn("model file for Super Orange is missing",
                      body["model_warning"])

    def test_assessment_transplant_followup_flow(self):
        self.client.post("/api/reference/build",
                         json={"species": "Super Orange"})
        # a plant-scoped prediction snapshots the plant_id so evaluation can
        # link it to that plant's transplant (species-cohort predictions have
        # no single plant and are therefore not evaluated this way).
        pred = self.client.post("/predict", json={
            "plantId": self.plant_id,
            "allow_provisional_baseline": True}).get_json()
        self.assertTrue(pred["available"])
        history = self.client.get(
            f"/api/predictions?plant_id={self.plant_id}").get_json()
        pred_id = history[0]["prediction_id"]

        resp = self.client.post("/api/assessments", json={
            "plant_id": self.plant_id, "assessment_date": "2083-05-11",
            "readiness_status": "ready", "prediction_id": pred_id})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["readiness_status"], "ready")

        resp = self.client.post("/api/transplant", json={
            "plant_id": self.plant_id, "transplant_date": "2083-05-11",
            "height_at_transplant_cm": 2.0,
            "condition_at_transplant": "healthy"})
        self.assertEqual(resp.status_code, 201)
        transplant = resp.get_json()
        feedback_id = transplant["feedback_id"]
        self.assertTrue(transplant["training_label_recorded"])
        self.assertEqual(dbm.readiness_event_counts(
            self.conn, "Super Orange", None, data_source="real"
        )["readiness_events"], 1)

        pending = self.client.get("/api/feedback?pending=1").get_json()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["feedback_state"]["status"], "pending")

        resp = self.client.post("/api/feedback", json={
            "feedback_id": feedback_id,
            "feedback_submitted_date": "2083-05-25",
            "survived_after_14d": True,
            "height_after_14d_cm": 3.0,
            "visible_condition": "healthy"})
        self.assertEqual(resp.status_code, 201)
        # A 14-day outcome is recorded for evaluation; it does not create a
        # second readiness-date label.
        self.assertEqual(dbm.readiness_event_counts(
            self.conn, "Super Orange", None, data_source="real"
        )["readiness_events"], 1)

        # prediction history is preserved (species prediction was snapshotted)
        history = self.client.get(
            "/api/predictions?species=Super Orange").get_json()
        self.assertGreaterEqual(len(history), 1)

    def test_validation_errors_return_400(self):
        self.assertEqual(self.client.post(
            "/api/assessments", json={"readiness_status": "ready"}
        ).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/transplant", json={"plant_id": self.plant_id}
        ).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/feedback", json={"feedback_id": 1}
        ).status_code, 400)

    def test_unknown_plant_returns_404(self):
        resp = self.client.post("/api/transplant", json={
            "plant_id": 99999, "transplant_date": "2083-05-11"})
        self.assertEqual(resp.status_code, 404)


class TestCameraCaptureReviewApi(BackendApiBase):
    def _add_pending_capture(self, plant_id, public_id, day="2083-05-10"):
        return self.repo.add_capture(
            plant_id=plant_id,
            public_id=public_id,
            observation_date=day,
            capture_timestamp=f"{day}T10:00:00",
            height_cm=2.0,
            stem_width_cm=0.3,
            visible_true_leaf_count=4,
            validity="valid",
            rod_length_cm=30.0,
            review_status="pending",
        )

    def test_discard_marks_reading_and_keeps_it_out_of_observations(self):
        capture = self._add_pending_capture(
            self.plant_id, "Plant01_20830510_100000")

        response = self.client.post("/api/captures/review", json={
            "ids": [capture["capture_id"]], "action": "discard"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "discarded")
        stored = self.repo.list_captures(
            self.plant_id, observation_date="2083-05-10")[0]
        self.assertEqual(stored["review_status"], "discarded")
        observations = self.repo.get_observations(self.plant_id)
        self.assertEqual(len(observations), 2)

    def test_accept_rebuilds_observation_then_prediction_uses_it(self):
        plant_id = self.repo.register_plant(
            species="Super Orange", data_source="real",
            external_ref="camera-plant", date_started="2083-04-25")
        capture = self._add_pending_capture(
            plant_id, "PlantCamera_20830510_100000")
        self.client.post("/api/reference/build",
                         json={"species": "Super Orange"})

        response = self.client.post("/api/captures/review", json={
            "ids": [capture["capture_id"]], "action": "accept"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "accepted")
        observations = self.repo.get_observations(plant_id)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["observation_date"], "2083-05-10")
        self.assertEqual(observations[0]["age_days"], 15)

        with mock.patch.object(backend.svc, "refresh_camera_observations") as refresh:
            prediction = self.client.post("/api/predict/species", json={
                "species": "Super Orange",
                "observation_date": "2083-05-10",
                "refresh_camera": False,
            })
        refresh.assert_not_called()
        self.assertEqual(prediction.status_code, 200)
        body = prediction.get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["cohort_snapshot"]["observation_date"], "2083-05-10")


if __name__ == "__main__":
    unittest.main()
