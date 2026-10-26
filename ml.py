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

from dataclasses import dataclass, asdict, field
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
MIN_PLANTS_FOR_CONFIDENT_BASELINE = 3


@dataclass
class PredictionResult:
    species: str
    predicted_days_remaining: Optional[float]
    predicted_readiness_date: Optional[str]
    method: str  # 'validated_ml_model' | 'provisional_historical_baseline' | 'provisional_height_matched_baseline' | 'insufficient_evidence'
    confidence: str  # 'high' | 'moderate' | 'low' | 'none'
    contributing_plants: int
    summary_message: str
    details: Dict[str, Any]
    data_sufficient: bool = False
    limitations: List[str] = field(default_factory=list)
    age_source: str = "unknown"

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

    def estimate_days_remaining_by_height(self, height_cm: Optional[float]) -> Optional[Dict[str, Any]]:
        """Match the current height to the cohort's median-height trajectory.

        Used only when the plant's age is unknown (no planted_date). The
        height source is a camera measurement, so this is deliberately
        reported as a provisional, lower-confidence baseline — never as a
        validated model.
        """
        if height_cm is None or self.trajectory_df.empty or self.max_age is None:
            return None
        df = self.trajectory_df.dropna(subset=["median_height"])
        if df.empty:
            return None
        df = df.assign(distance=(df["median_height"] - float(height_cm)).abs())
        match = df.sort_values("distance").iloc[0]
        matched_age = int(match["age_days"])
        return {
            "days_remaining": max(0.0, float(self.max_age - matched_age)),
            "matched_age_days": matched_age,
            "matched_median_height_cm": round(float(match["median_height"]), 2),
            "height_delta_cm": round(abs(float(match["median_height"]) - float(height_cm)), 2),
            "contributing_plants": int(match["contributing_plants"]),
            "target_transplant_age": int(self.max_age),
        }


_MODEL_CACHE: Dict[Tuple[str, str, int, int, str], Dict[str, Any]] = {}


def _training_fingerprint(db_path: Path | str, species: Optional[str]) -> Tuple[str, str, int, int, str]:
    """Cache key that changes whenever observations or readiness events change."""
    stats = db.get_cohort_stats(db_path, species=species)
    path_key = ":memory:" if str(db_path) == ":memory:" else str(Path(db_path).resolve())
    return (
        path_key,
        species or "*",
        int(stats["n_observations"]),
        int(stats["n_readiness_events"]),
        str(stats["latest_observation_date"] or ""),
    )


def train_countdown_model(
    db_path: Path | str = db.DEFAULT_DB_PATH,
    species: Optional[str] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Train/validate the countdown model, reusing the result while data is unchanged."""
    fingerprint = _training_fingerprint(db_path, species)
    if use_cache and fingerprint in _MODEL_CACHE:
        return _MODEL_CACHE[fingerprint]
    result = _train_countdown_model_uncached(db_path, species)
    if use_cache:
        _MODEL_CACHE[fingerprint] = result
    return result


def _train_countdown_model_uncached(
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
    plant_id: Optional[str] = None,
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

    limitations: List[str] = []
    age_source = "unknown"

    # Resolve the plant's own latest measurement only. Never borrow another
    # plant's row just to fill a gap: missing values stay missing.
    if plant_id and any(
        value is None
        for value in (
            current_height_cm,
            current_age_days,
            current_growth_rate,
            current_stem_width,
            current_leaf_count,
        )
    ):
        latest = db.get_latest_observation(db_path, plant_id)
        if latest:
            if current_obs_date is None:
                obs_date = db.parse_date(str(latest["observation_date"]))
            if current_height_cm is None and latest.get("height_cm") is not None:
                current_height_cm = float(latest["height_cm"])
            if current_age_days is None and latest.get("age_days") is not None:
                current_age_days = int(latest["age_days"])
                age_source = "observation"
            if current_growth_rate is None and latest.get("growth_rate_cm_per_day") is not None:
                current_growth_rate = float(latest["growth_rate_cm_per_day"])
            if current_stem_width is None and latest.get("stem_width_cm") is not None:
                current_stem_width = float(latest["stem_width_cm"])
            if current_leaf_count is None and latest.get("visible_true_leaf_count") is not None:
                current_leaf_count = int(latest["visible_true_leaf_count"])

    # Cohort quarantine: the plant being predicted never contributes to its own
    # reference, and unverified camera rows are excluded from the cohort.
    all_observations = db.get_observations(db_path, species=species)
    observations = [
        o
        for o in all_observations
        if (not plant_id or str(o["plant_id"]) != str(plant_id))
        and not (o.get("source") == "camera" and not o.get("calibration_verified"))
    ]
    baseline = BaselineTrajectory(observations)
    unique_plants = len({o["plant_id"] for o in observations})
    cohort_stats = db.get_cohort_stats(db_path, species=species, exclude_plant_id=plant_id)

    if cohort_stats["n_plants"] == 0:
        limitations.append("No historical reference plants exist for this species.")
    elif cohort_stats["n_plants"] < MIN_PLANTS_FOR_CONFIDENT_BASELINE:
        limitations.append(
            f"Small reference cohort: {cohort_stats['n_plants']} plant(s)."
        )
    if current_age_days is None:
        limitations.append(
            "Planting date (age) is unknown, so age-based methods are unavailable."
        )

    # LEVEL 1: Validated ML model (requires an observed age and height)
    if (
        trained_model is not None
        and getattr(trained_model, "predict", None) is not None
        and current_age_days is not None
        and current_height_cm is not None
    ):
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
            if current_growth_rate is None:
                limitations.append(
                    "Growth rate missing; the model imputed it from the training cohort."
                )
            return PredictionResult(
                species=species,
                predicted_days_remaining=pred_days,
                predicted_readiness_date=ready_date.isoformat(),
                method="validated_ml_model",
                confidence="high",
                contributing_plants=unique_plants,
                summary_message=(
                    f"Estimated {pred_days} days remaining from a validated machine "
                    f"learning model trained on {unique_plants} plants."
                ),
                details={
                    "inputs": sample_df.to_dict(orient="records")[0],
                    "cohort_stats": cohort_stats,
                },
                data_sufficient=True,
                limitations=list(limitations),
                age_source=age_source or "supplied",
            )
        except Exception as exc:  # noqa: BLE001 - fall through to the baseline
            limitations.append(f"ML model could not score this plant ({exc}).")

    # LEVEL 2: Historical cohort baseline, age-based
    if current_age_days is not None and not baseline.trajectory_df.empty:
        pred_days = baseline.estimate_days_remaining(current_age_days)
        if pred_days is not None:
            ready_date = obs_date + timedelta(days=int(round(pred_days)))
            confidence = (
                "moderate"
                if cohort_stats["n_plants"] >= MIN_PLANTS_FOR_CONFIDENT_BASELINE
                else "low"
            )
            if baseline.max_age is not None and current_age_days >= baseline.max_age:
                limitations.append(
                    "Age is at or past the reference cohort's transplant age."
                )
            return PredictionResult(
                species=species,
                predicted_days_remaining=pred_days,
                predicted_readiness_date=ready_date.isoformat(),
                method="provisional_historical_baseline",
                confidence=confidence,
                contributing_plants=unique_plants,
                summary_message=(
                    f"Estimated {pred_days} days remaining by comparing age "
                    f"{current_age_days} d to the historical successful-plant trajectory "
                    f"(transplant at ~{baseline.max_age} d)."
                ),
                details={
                    "current_age": current_age_days,
                    "target_transplant_age": baseline.max_age,
                    "median_height_cm": current_height_cm,
                    "cohort_stats": cohort_stats,
                },
                data_sufficient=True,
                limitations=list(limitations),
                age_source=age_source if age_source != "unknown" else "supplied",
            )

    # LEVEL 2b: Historical cohort matched by height (age unknown)
    if current_height_cm is not None and not baseline.trajectory_df.empty:
        match = baseline.estimate_days_remaining_by_height(current_height_cm)
        if match is not None:
            pred_days = float(match["days_remaining"])
            ready_date = obs_date + timedelta(days=int(round(pred_days)))
            limitations.append(
                "Age unknown: days remaining inferred by matching current height to "
                f"the cohort median height at age {match['matched_age_days']} d."
            )
            limitations.append(
                "Height comes from a camera measurement; scale or segmentation error "
                "shifts the matched age."
            )
            return PredictionResult(
                species=species,
                predicted_days_remaining=pred_days,
                predicted_readiness_date=ready_date.isoformat(),
                method="provisional_height_matched_baseline",
                confidence="low",
                contributing_plants=unique_plants,
                summary_message=(
                    f"Estimated {pred_days} days remaining by matching the measured "
                    f"height {current_height_cm:.2f} cm to the cohort median "
                    f"{match['matched_median_height_cm']:.2f} cm at age "
                    f"{match['matched_age_days']} d."
                ),
                details={
                    "current_height_cm": current_height_cm,
                    "match": match,
                    "cohort_stats": cohort_stats,
                },
                data_sufficient=True,
                limitations=list(limitations),
                age_source="height_matched",
            )

    # LEVEL 3: Insufficient evidence
    missing: List[str] = []
    if current_height_cm is None:
        missing.append("a measured height")
    if cohort_stats["n_plants"] == 0:
        missing.append("historical reference plants for this species")
    elif current_age_days is None:
        missing.append("the planting date (age)")
    limitations.extend(f"Missing {item}." for item in missing)
    return PredictionResult(
        species=species,
        predicted_days_remaining=None,
        predicted_readiness_date=None,
        method="insufficient_evidence",
        confidence="none",
        contributing_plants=unique_plants,
        summary_message=(
            "Insufficient evidence to predict: "
            + (", ".join(missing) if missing else "measurements and reference data are required")
            + "."
        ),
        details={
            "observations_found": len(all_observations),
            "cohort_observations": len(observations),
            "current_height": current_height_cm,
            "current_age": current_age_days,
            "cohort_stats": cohort_stats,
        },
        data_sufficient=False,
        limitations=list(limitations),
        age_source=age_source,
    )



