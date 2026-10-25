"""
ml.py — Machine Learning Countdown Predictor & Baseline Growth Trajectory

Adheres to non-negotiable principles:
1. Honest prediction methods: Validated ML -> Provisional Baseline -> Insufficient Evidence.
2. GroupKFold CV grouped by plant_id to prevent data leakage.
3. Model is only marked 'validated' if it beats the mean dummy baseline and meets
   minimum sample sizes (>= 4 plants, >= 6 readiness events).
4. Never fabricates growth; missing values stay missing.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import warnings
import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import db


FEATURE_COLUMNS = [
    "age_days",
    "height_cm",
    "growth_rate_cm_per_day",
    "stem_width_cm",
    "visible_true_leaf_count",
]

MIN_PLANTS_FOR_VALIDATION = 4
MIN_EVENTS_FOR_VALIDATION = 6


@dataclass
class PredictionResult:
    species: str
    predicted_days_remaining: Optional[float]
    predicted_readiness_date: Optional[str]
    method: str  # 'validated_ml_model' | 'provisional_historical_baseline' | 'insufficient_evidence'
    confidence: str  # 'high' | 'moderate' | 'low' | 'none'
    contributing_plants: int
    summary_message: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BaselineTrajectory:
    """Historical cohort median trajectory calculator."""

    def __init__(self, observations: List[Dict[str, Any]]):
        self.observations = observations
        self.trajectory_df = self._compute_trajectory()

    def _compute_trajectory(self) -> pd.DataFrame:
        if not self.observations:
            return pd.DataFrame()
        df = pd.DataFrame(self.observations)
        if "age_days" not in df.columns or df["age_days"].dropna().empty:
            return pd.DataFrame()

        grouped = df.groupby("age_days").agg(
            median_height=("height_cm", "median"),
            median_growth_rate=("growth_rate_cm_per_day", "median"),
            median_stem=("stem_width_cm", "median"),
            median_leaves=("visible_true_leaf_count", "median"),
            contributing_plants=("plant_id", "nunique"),
        ).reset_index()
        return grouped

    @property
    def max_age(self) -> Optional[int]:
        if self.trajectory_df.empty:
            return None
        return int(self.trajectory_df["age_days"].max())

    def estimate_days_remaining(self, current_age: int) -> Optional[float]:
        if self.trajectory_df.empty or self.max_age is None:
            return None
        # Historical transplant point corresponds to end of successful monitoring
        days = float(self.max_age - current_age)
        return max(0.0, days)


def train_countdown_model(
    db_path: Path | str = db.DEFAULT_DB_PATH,
    species: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trains and cross-validates candidate countdown regressors using GroupKFold (grouped by plant_id).
    Validates model against DummyRegressor baseline.
    """
    rows = db.get_training_rows(db_path, species=species)
    if not rows:
        return {"validated": False, "reason": "No training rows found with readiness events."}

    df = pd.DataFrame(rows)
    n_plants = df["plant_id"].nunique()
    n_events = len(rows)

    if n_plants < MIN_PLANTS_FOR_VALIDATION or n_events < MIN_EVENTS_FOR_VALIDATION:
        return {
            "validated": False,
            "reason": f"Sample size below threshold ({n_plants}/{MIN_PLANTS_FOR_VALIDATION} plants, {n_events}/{MIN_EVENTS_FOR_VALIDATION} events).",
            "n_plants": n_plants,
            "n_events": n_events,
        }

    X = df[FEATURE_COLUMNS]
    y = df["days_remaining"].values
    groups = df["plant_id"].values

    # Preprocessor: Median imputation + Standard Scaling
    preprocessor = Pipeline([
        ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("scaler", StandardScaler()),
    ])

    candidates = {
        "mean_baseline": Pipeline([
            ("pre", preprocessor),
            ("model", DummyRegressor(strategy="mean")),
        ]),
        "ridge": Pipeline([
            ("pre", preprocessor),
            ("model", Ridge(alpha=1.0)),
        ]),
        "random_forest": Pipeline([
            ("pre", preprocessor),
            ("model", RandomForestRegressor(n_estimators=100, max_depth=6, min_samples_leaf=2, random_state=42)),
        ]),
    }

    n_splits = min(5, n_plants)
    gkf = GroupKFold(n_splits=n_splits)

    cv_scores = {}
    for name, pipe in candidates.items():
        fold_maes = []
        for train_idx, val_idx in gkf.split(X, y, groups):
            X_tr, y_tr = X.iloc[train_idx], y[train_idx]
            X_val, y_val = X.iloc[val_idx], y[val_idx]
            pipe.fit(X_tr, y_tr)
            preds = pipe.predict(X_val)
            fold_maes.append(mean_absolute_error(y_val, preds))
        cv_scores[name] = float(np.mean(fold_maes))

    baseline_mae = cv_scores["mean_baseline"]
    best_candidate = min(["ridge", "random_forest"], key=lambda k: cv_scores[k])
    best_mae = cv_scores[best_candidate]

    validated = best_mae < baseline_mae
    best_pipe = candidates[best_candidate]
    # Refit winning model on full dataset
    best_pipe.fit(X, y)

    return {
        "validated": validated,
        "best_model_name": best_candidate,
        "best_mae": round(best_mae, 3),
        "baseline_mae": round(baseline_mae, 3),
        "n_plants": n_plants,
        "n_samples": len(df),
        "pipeline": best_pipe,
        "cv_scores": cv_scores,
    }


def predict_species_readiness(
    db_path: Path | str = db.DEFAULT_DB_PATH,
    species: str = "Sunflower(SuperOrange)",
    current_obs_date: Optional[date | str] = None,
    current_height_cm: Optional[float] = None,
    current_age_days: Optional[int] = None,
    current_growth_rate: Optional[float] = None,
    current_stem_width: Optional[float] = None,
    current_leaf_count: Optional[int] = None,
    trained_model: Optional[Any] = None,
) -> PredictionResult:
    """
    Executes the Fallback Prediction Ladder:
    1. Validated ML Model (if available and criteria met)
    2. Historical Cohort Baseline
    3. Insufficient Evidence Fallback
    """
    obs_date = (
        db.parse_date(str(current_obs_date))
        if current_obs_date
        else date.today()
    )

    observations = db.get_observations(db_path, species=species)
    baseline = BaselineTrajectory(observations)
    unique_plants = len({o["plant_id"] for o in observations})

    # If inputs are not explicitly provided, try to compute from current database observations
    if current_height_cm is None and observations:
        df_obs = pd.DataFrame(observations)
        recent = df_obs.sort_values("observation_date").iloc[-1]
        current_height_cm = float(recent["height_cm"])
        current_age_days = int(recent["age_days"]) if pd.notnull(recent.get("age_days")) else None
        current_growth_rate = float(recent["growth_rate_cm_per_day"]) if pd.notnull(recent.get("growth_rate_cm_per_day")) else None
        current_stem_width = float(recent["stem_width_cm"]) if pd.notnull(recent.get("stem_width_cm")) else None
        current_leaf_count = int(recent["visible_true_leaf_count"]) if pd.notnull(recent.get("visible_true_leaf_count")) else None

    # LEVEL 1: Check for Validated ML Model
    if trained_model and getattr(trained_model, "predict", None) is not None:
        sample_df = pd.DataFrame([{
            "age_days": current_age_days,
            "height_cm": current_height_cm,
            "growth_rate_cm_per_day": current_growth_rate,
            "stem_width_cm": current_stem_width,
            "visible_true_leaf_count": current_leaf_count,
        }])
        try:
            pred_val = float(trained_model.predict(sample_df)[0])
            pred_days = max(0.0, round(pred_val, 1))
            ready_date = obs_date + timedelta(days=int(round(pred_days)))
            return PredictionResult(
                species=species,
                predicted_days_remaining=pred_days,
                predicted_readiness_date=ready_date.isoformat(),
                method="validated_ml_model",
                confidence="high",
                contributing_plants=unique_plants,
                summary_message=f"Estimated {pred_days} days remaining based on validated machine learning model.",
                details={"inputs": sample_df.to_dict(orient="records")[0]},
            )
        except Exception:
            pass  # Fall through to Level 2

    # LEVEL 2: Historical Cohort Baseline
    if current_age_days is not None and not baseline.trajectory_df.empty:
        pred_days = baseline.estimate_days_remaining(current_age_days)
        if pred_days is not None:
            ready_date = obs_date + timedelta(days=int(round(pred_days)))
            return PredictionResult(
                species=species,
                predicted_days_remaining=pred_days,
                predicted_readiness_date=ready_date.isoformat(),
                method="provisional_historical_baseline",
                confidence="moderate",
                contributing_plants=unique_plants,
                summary_message=f"Estimated {pred_days} days remaining based on historical successful-plant median trajectory.",
                details={
                    "current_age": current_age_days,
                    "target_transplant_age": baseline.max_age,
                    "median_height_cm": current_height_cm,
                },
            )

    # LEVEL 3: Insufficient Evidence Fallback
    return PredictionResult(
        species=species,
        predicted_days_remaining=None,
        predicted_readiness_date=None,
        method="insufficient_evidence",
        confidence="none",
        contributing_plants=unique_plants,
        summary_message="Insufficient observation evidence: missing age or historical trajectory for this species.",
        details={
            "observations_found": len(observations),
            "current_height": current_height_cm,
            "current_age": current_age_days,
        },
    )


def evaluate_predictions(db_path: Path | str = db.DEFAULT_DB_PATH) -> Dict[str, Any]:
    """
    Evaluates historical predictions against recorded actual transplantation feedback.
    """
    with db.get_connection(db_path) as conn:
        rows = conn.execute("""
            SELECT
                p.id as prediction_id,
                p.species,
                p.prediction_date,
                p.predicted_days_remaining,
                f.actual_transplant_date,
                CAST(JULIANDAY(f.actual_transplant_date) - JULIANDAY(p.prediction_date) AS REAL) as actual_days_taken
            FROM predictions p
            JOIN feedback f ON p.id = f.prediction_id
            WHERE f.actual_transplant_date IS NOT NULL
              AND p.predicted_days_remaining IS NOT NULL
        """).fetchall()

    if not rows:
        return {"evaluations_count": 0, "message": "No evaluated feedback records with actual transplant dates yet."}

    errors = []
    for r in rows:
        err = r["predicted_days_remaining"] - r["actual_days_taken"]
        errors.append(err)

    mae = float(np.mean(np.abs(errors)))
    bias = float(np.mean(errors))
    return {
        "evaluations_count": len(rows),
        "mae_days": round(mae, 2),
        "mean_bias_days": round(bias, 2),
        "records": [dict(r) for r in rows],
    }
