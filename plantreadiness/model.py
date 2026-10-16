"""Countdown model training, artefacts and species-aware selection.

Design rules (task sections 8-10):

* target: ``days_remaining = readiness_observed_date - observation_date``
  computed ONLY from observations at/before the readiness event (see
  :func:`plantreadiness.db.readiness_training_rows`),
* labels come exclusively from recorded readiness events - never from
  rules or LLM-generated criteria,
* simple regression baselines first (mean dummy, Ridge, Random Forest),
* validation is plant-grouped (GroupKFold on ``plant_id``) so repeated
  observations from one plant never leak across train/test,
* errors are reported in days (MAE),
* synthetic-trained and real-trained models live in separate directories
  with separate registries,
* artefacts carry metadata: data source, supported species/varieties,
  target definition, feature list, training date, validation method and
  metrics, number of independent plants and labelled readiness events.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.base import clone as sklearn_clone

from . import config

FEATURE_COLUMNS = [
    "age_days",
    "height_cm",
    "growth_rate_cm_per_day",
    "stem_width_cm",
    "visible_true_leaf_count",
    "species",
    "variety",
]
NUMERIC_FEATURES = [
    "age_days",
    "height_cm",
    "growth_rate_cm_per_day",
    "stem_width_cm",
    "visible_true_leaf_count",
]
CATEGORICAL_FEATURES = ["species", "variety"]


class InsufficientDataError(Exception):
    """Raised when labelled data does not meet the minimum requirements."""


# --------------------------------------------------------------------------- #
# Frame building
# --------------------------------------------------------------------------- #


def build_training_frame(rows: list[dict]) -> pd.DataFrame:
    """Turn readiness-target rows into a feature/target frame."""
    if not rows:
        return pd.DataFrame(columns=FEATURE_COLUMNS + [
            "target_days_remaining", "plant_id"])
    df = pd.DataFrame(rows)
    df["variety"] = df["variety"].fillna("unknown")
    df = df.rename(columns={"days_remaining": "target_days_remaining"})
    out = df[FEATURE_COLUMNS + ["target_days_remaining", "plant_id"]].copy()
    # non-negative target guard (readiness must be >= observation; enforced
    # in SQL via observation_date <= readiness_observed_date)
    out = out[out["target_days_remaining"] >= 0]
    return out.reset_index(drop=True)


def check_min_requirements(df: pd.DataFrame,
                           source_label: str = "data") -> dict:
    """Enforce configured minimums before any supervised training."""
    if df.empty:
        raise InsufficientDataError(
            f"No labelled readiness-event rows available in {source_label}. "
            + config.UNAVAILABLE_MESSAGE
        )
    n_plants = int(df["plant_id"].nunique())
    n_events = n_plants  # one earliest readiness event per plant in the frame
    if n_plants < config.MIN_INDEPENDENT_PLANTS or \
            n_events < config.MIN_READINESS_EVENTS:
        raise InsufficientDataError(
            f"Insufficient labelled data in {source_label}: "
            f"{n_plants} independent plants / {n_events} readiness events "
            f"(minimum {config.MIN_INDEPENDENT_PLANTS} plants and "
            f"{config.MIN_READINESS_EVENTS} events). "
            + config.UNAVAILABLE_MESSAGE
        )
    return {"n_independent_plants": n_plants,
            "n_labelled_readiness_events": n_events}


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #


def _preprocessor() -> ColumnTransformer:
    numeric = Pipeline([
        ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("scale", StandardScaler()),
    ])
    categorical = Pipeline([
        ("impute", SimpleImputer(strategy="most_frequent",
                                 keep_empty_features=True)),
        ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
    ])
    return ColumnTransformer([
        ("num", numeric, NUMERIC_FEATURES),
        ("cat", categorical, CATEGORICAL_FEATURES),
    ])


def make_pipelines(random_state: int = 42) -> dict:
    """Candidate regressors, baselines first."""
    return {
        "mean_baseline": Pipeline([
            ("prep", _preprocessor()),
            ("reg", DummyRegressor(strategy="mean")),
        ]),
        "ridge": Pipeline([
            ("prep", _preprocessor()),
            ("reg", Ridge(alpha=1.0)),
        ]),
        "random_forest": Pipeline([
            ("prep", _preprocessor()),
            ("reg", RandomForestRegressor(
                n_estimators=200, max_depth=8, min_samples_leaf=3,
                random_state=random_state)),
        ]),
    }


# --------------------------------------------------------------------------- #
# Plant-grouped cross-validation
# --------------------------------------------------------------------------- #


def grouped_cv_mae(pipeline, df: pd.DataFrame, n_splits: int) -> dict:
    """Grouped CV MAE (days). Observations of one plant stay in one fold."""
    X = df[FEATURE_COLUMNS]
    y = df["target_days_remaining"]
    groups = df["plant_id"].values
    n_groups = len(np.unique(groups))
    n_splits = max(2, min(n_splits, n_groups))
    gkf = GroupKFold(n_splits=n_splits)
    fold_maes = []
    for train_idx, test_idx in gkf.split(X, y, groups):
        est = sklearn_clone(pipeline)  # fresh copy, no refit leakage
        est.fit(X.iloc[train_idx], y.iloc[train_idx])
        pred = est.predict(X.iloc[test_idx])
        fold_maes.append(mean_absolute_error(y.iloc[test_idx], pred))
    return {
        "mae_days": float(np.mean(fold_maes)),
        "mae_days_std": float(np.std(fold_maes)),
        "n_splits": int(n_splits),
    }


def train_countdown_model(df: pd.DataFrame,
                          data_source: str,
                          species: Optional[str] = None,
                          variety: Optional[str] = None,
                          random_state: int = 42,
                          source_label: Optional[str] = None) -> dict:
    """Train the best of several baselines with grouped CV.

    Returns an artefact dict ``{"model": pipeline, "metadata": {...}}``.
    Raises :class:`InsufficientDataError` when minimums are not met.
    """
    label = source_label or f"{data_source} data"
    reqs = check_min_requirements(df, label)

    species_list = sorted(df["species"].dropna().unique().tolist())
    variety_list = sorted(
        df.loc[df["variety"] != "unknown", "variety"].unique().tolist())

    results = {}
    for name, pipe in make_pipelines(random_state).items():
        results[name] = grouped_cv_mae(
            pipe, df, n_splits=min(5, reqs["n_independent_plants"]))
        results[name]["model_name"] = name

    baseline_mae = results["mean_baseline"]["mae_days"]
    best_name = min(results, key=lambda k: results[k]["mae_days"])
    best_mae = results[best_name]["mae_days"]

    validated = (
        best_name != "mean_baseline"
        and best_mae < baseline_mae
        and reqs["n_independent_plants"] >= config.MIN_INDEPENDENT_PLANTS
        and reqs["n_labelled_readiness_events"] >= config.MIN_READINESS_EVENTS
    )

    # refit the winning pipeline on all provided rows
    final_model = make_pipelines(random_state)[best_name]
    final_model.fit(df[FEATURE_COLUMNS], df["target_days_remaining"])

    metadata = {
        "training_data_source": data_source,
        "is_synthetic": data_source == "synthetic",
        "supported_species": species_list,
        "supported_varieties": variety_list,
        "scope_species": species,
        "scope_variety": variety,
        "target_definition": config.TARGET_DEFINITION,
        "feature_list": list(FEATURE_COLUMNS),
        "training_date": datetime.now(timezone.utc).isoformat(),
        "validation_method": (
            f"GroupKFold by plant_id (n={results[best_name]['n_splits']}), "
            "repeated observations of a plant never split across folds"
        ),
        "validation_metrics": {
            "mae_days": round(best_mae, 4),
            "mae_days_std": round(results[best_name]["mae_days_std"], 4),
            "baseline_mean_mae_days": round(baseline_mae, 4),
            "all_candidates": {
                k: round(v["mae_days"], 4) for k, v in results.items()
            },
        },
        "n_independent_plants": reqs["n_independent_plants"],
        "n_labelled_readiness_events": reqs["n_labelled_readiness_events"],
        "n_training_rows": int(len(df)),
        "validated": bool(validated),
        "model_name": best_name,
        "training_plant_ids": sorted(int(p) for p in df["plant_id"].unique()),
        "limitations": (
            "SYNTHETIC DEMO model - trained on simulated data with assumed "
            "readiness rules; not a botanical finding."
            if data_source == "synthetic" else
            "Trained on recorded readiness events only; see validation "
            "metrics for expected error in days."
        ),
    }

    return {"model": final_model, "metadata": metadata}


# --------------------------------------------------------------------------- #
# Artefact storage + registry (separate dirs per data source)
# --------------------------------------------------------------------------- #


def _registry_path(models_dir: Path, data_source: str) -> Path:
    return Path(models_dir) / data_source / "registry.json"


def _load_registry(models_dir: Path, data_source: str) -> dict:
    path = _registry_path(models_dir, data_source)
    if path.exists():
        return json.loads(path.read_text())
    return {"data_source": data_source, "artifacts": []}


def _save_registry(models_dir: Path, data_source: str, registry: dict) -> None:
    path = _registry_path(models_dir, data_source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2))


def save_artifact(artifact: dict, models_dir: Path) -> dict:
    """Persist an artefact under ``models_dir/<source>/`` + registry entry."""
    meta = artifact["metadata"]
    source = meta["training_data_source"]
    out_dir = Path(models_dir) / source
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = (meta.get("scope_species") or "ALL").replace(" ", "")
    var = (meta.get("scope_variety") or "ANY").replace(" ", "")
    fname = f"countdown_{scope}_{var}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.joblib"
    joblib.dump(artifact, out_dir / fname)

    registry = _load_registry(models_dir, source)
    registry["artifacts"].append({
        "file": fname,
        "metadata": meta,
        "active": True,
    })
    _save_registry(models_dir, source, registry)
    return {"file": str(out_dir / fname), "metadata": meta}


def retire_artifacts(models_dir: Path, data_source: str,
                     scope_species: Optional[str],
                     scope_variety: Optional[str]) -> int:
    """Mark older artefacts of the same scope inactive (kept for history)."""
    registry = _load_registry(models_dir, data_source)
    retired = 0
    for entry in registry["artifacts"]:
        meta = entry["metadata"]
        same_species = (meta.get("scope_species") or None) == scope_species
        same_variety = (meta.get("scope_variety") or None) == scope_variety
        if same_species and same_variety and entry.get("active", True):
            entry["active"] = False
            retired += 1
    if retired:
        _save_registry(models_dir, data_source, registry)
    return retired


def load_artifact(entry: dict, models_dir: Path, data_source: str) -> dict:
    path = Path(models_dir) / data_source / entry["file"]
    return joblib.load(path)


def list_artifacts(models_dir: Path, data_source: str) -> list[dict]:
    return _load_registry(models_dir, data_source)["artifacts"]


# --------------------------------------------------------------------------- #
# Species-aware selection (fallback order)
# --------------------------------------------------------------------------- #


def select_model(species: str,
                 variety: Optional[str],
                 models_dir: Path,
                 allow_synthetic: bool = False) -> dict:
    """Apply the fallback order of task section 8.

    1. validated species/variety model (real),
    2. broader validated real model for the species,
    3. synthetic demo model - ONLY when explicitly enabled, clearly labelled,
    4. otherwise: insufficient evidence (level 4, no model).

    Returns a dict with ``level`` (1-4), ``artifact`` (or None),
    ``entry`` (or None) and a human-readable ``message``.
    """
    species_l = (species or "").strip().lower()
    variety_l = (variety or "").strip().lower() or None

    real_entries = [e for e in list_artifacts(models_dir, "real")
                    if e.get("active", True)]
    synth_entries = [e for e in list_artifacts(models_dir, "synthetic")
                     if e.get("active", True)]

    def _validated(entry) -> bool:
        return bool(entry["metadata"].get("validated"))

    def _species_covered(entry, sp) -> bool:
        """Model was trained on data covering this species."""
        if not _validated(entry):
            return False
        meta = entry["metadata"]
        scope_sp = (meta.get("scope_species") or "").lower() or None
        supported = [s.lower() for s in meta.get("supported_species", [])]
        return scope_sp == sp or sp in supported

    def _exact_scope(entry, sp, var) -> bool:
        """Model scoped to this exact species AND variety (Level 1)."""
        if var is None or not _validated(entry):
            return False
        meta = entry["metadata"]
        scope_sp = (meta.get("scope_species") or "").lower() or None
        scope_var = (meta.get("scope_variety") or "").lower() or None
        return scope_sp == sp and scope_var == var

    def _prefer_species_specific(entries, sp):
        """Species-scoped models first, then broader multi-species ones."""
        specific = [e for e in entries
                    if (e["metadata"].get("scope_species") or "").lower() == sp]
        broad = [e for e in entries if e not in specific]
        return list(reversed(specific)) + list(reversed(broad))

    # ---- Level 1: exact species + variety (real) --------------------------
    for entry in reversed(real_entries):
        if _exact_scope(entry, species_l, variety_l):
            return {"level": 1, "data_source": "real", "entry": entry,
                    "artifact": None,
                    "message": "Level 1: validated species/variety model."}

    # ---- Level 2: broader real species model -----------------------------
    covered = [e for e in real_entries if _species_covered(e, species_l)]
    if covered:
        entry = _prefer_species_specific(covered, species_l)[0]
        return {"level": 2, "data_source": "real", "entry": entry,
                "artifact": None,
                "message": ("Level 2: broader real model for this species "
                            "(no validated species/variety model yet); "
                            "uncertainty and limitations apply.")}

    # ---- Level 3: synthetic demo, explicit opt-in only --------------------
    if allow_synthetic:
        synth_specific_variety = [e for e in synth_entries
                                  if _exact_scope(e, species_l, variety_l)]
        synth_species = [e for e in synth_entries
                         if e not in synth_specific_variety
                         and _species_covered(e, species_l)]
        for entry in synth_specific_variety + _prefer_species_specific(
                synth_species, species_l):
            scope = "species/variety" if entry in synth_specific_variety \
                else "broader species"
            return {"level": 3, "data_source": "synthetic", "entry": entry,
                    "artifact": None,
                    "message": (f"Level 3: SYNTHETIC DEMO prediction "
                                f"({scope} simulated model) - clearly "
                                f"labelled, not a botanical finding.")}

    # ---- Level 4: insufficient evidence -----------------------------------
    note = ""
    if not allow_synthetic and synth_entries:
        note = (" Synthetic demo models exist but were NOT used: synthetic "
                "fallback must be explicitly enabled for this plant.")
    return {"level": 4, "data_source": None, "entry": None, "artifact": None,
            "message": config.UNAVAILABLE_MESSAGE + note}


def load_selection(selection: dict, models_dir: Path) -> dict:
    """Load the model for a selection result (no-op for level 4)."""
    if selection["level"] == 4 or selection["entry"] is None:
        selection["artifact"] = None
        return selection
    selection["artifact"] = load_artifact(
        selection["entry"], models_dir, selection["data_source"])
    return selection


# --------------------------------------------------------------------------- #
# Prediction
# --------------------------------------------------------------------------- #


def predict_days_remaining(selection: dict, observation: dict) -> dict:
    """Predict days remaining and estimated readiness date from one observation.

    ``observation`` must provide the plant's species/variety and the feature
    values of its latest (pre-readiness) measurement.

    If an ``observation_date`` is present, calculates:
        predicted_readiness_date = observation_date + timedelta(days=round(raw))

    Negative prediction policy:
    Does NOT clamp negative values to 0.0 to hide model behavior. If raw < 0,
    past_readiness is True, status is 'past_readiness', and a descriptive note
    is included indicating readiness was reached prior to this observation.
    """
    if selection["level"] == 4 or not selection.get("artifact"):
        return {
            "available": False,
            # selection.message already equals config.UNAVAILABLE_MESSAGE,
            # plus an explanatory note (e.g. "synthetic models exist but were
            # NOT used") - surfacing it keeps the refusal honest.
            "message": selection.get("message") or config.UNAVAILABLE_MESSAGE,
            "synthetic": False,
            "predicted_readiness_date": None,
        }

    meta = selection["artifact"]["metadata"]
    row = {col: observation.get(col) for col in FEATURE_COLUMNS}
    row["species"] = observation.get("species")
    row["variety"] = observation.get("variety") or "unknown"
    X = pd.DataFrame([row], columns=FEATURE_COLUMNS)

    raw = float(selection["artifact"]["model"].predict(X)[0])
    metrics = meta.get("validation_metrics", {})

    obs_date_str = observation.get("observation_date")
    predicted_date_str = None
    if obs_date_str:
        try:
            obs_d = datetime.strptime(str(obs_date_str).split("T")[0].strip(), "%Y-%m-%d").date()
            pred_d = obs_d + timedelta(days=int(round(raw)))
            predicted_date_str = pred_d.isoformat()
        except Exception:
            predicted_date_str = None

    past_readiness = raw < 0
    readiness_status = "past_readiness" if past_readiness else ("ready_today" if round(raw, 1) == 0.0 else "ready_soon")
    status_note = None
    if past_readiness:
        status_note = (
            f"Readiness passed: model estimates seedling passed transplant-ready "
            f"stage approximately {abs(round(raw, 1))} days before this observation."
        )

    return {
        "available": True,
        "days_remaining_raw": round(raw, 2),
        "days_remaining": round(raw, 1),
        "past_readiness": past_readiness,
        "readiness_status": readiness_status,
        "status_note": status_note,
        "clamped": False,
        "predicted_readiness_date": predicted_date_str,
        "observation_date": obs_date_str,
        "mae_days": metrics.get("mae_days"),
        "mae_days_std": metrics.get("mae_days_std"),
        "baseline_mae_days": metrics.get("baseline_mean_mae_days"),
        "level": selection["level"],
        "message": selection["message"],
        "data_source": selection["data_source"],
        "synthetic": bool(meta.get("is_synthetic")),
        "validated": bool(meta.get("validated")),
        "model_name": meta.get("model_name"),
        "n_independent_plants": meta.get("n_independent_plants"),
        "n_labelled_readiness_events": meta.get("n_labelled_readiness_events"),
        "limitations": meta.get("limitations"),
    }


# --------------------------------------------------------------------------- #
# Retraining gate: candidate must beat deployed model on the same holdout
# --------------------------------------------------------------------------- #


def evaluate_on_holdout(artifact: dict, holdout_df: pd.DataFrame) -> float:
    """MAE (days) of an artefact on a holdout frame it never trained on."""
    X = holdout_df[FEATURE_COLUMNS]
    y = holdout_df["target_days_remaining"]
    pred = artifact["model"].predict(X)
    return float(mean_absolute_error(y, pred))


def decide_update(current: Optional[dict], candidate: dict,
                  current_holdout_mae: Optional[float],
                  candidate_holdout_mae: Optional[float]) -> dict:
    """Compare candidate vs deployed model on the SAME held-out set.

    Keeps the deployed model unless the candidate is strictly better AND the
    candidate is validated AND both evaluations exist.
    """
    if current is None:
        return {"replace": True,
                "reason": "No deployed model; promoting candidate."}
    if current_holdout_mae is None or candidate_holdout_mae is None:
        return {"replace": False,
                "reason": "Evidence insufficient: holdout evaluation missing."}
    if not candidate["metadata"].get("validated"):
        return {"replace": False,
                "reason": "Candidate failed validation; keeping current model."}
    if candidate_holdout_mae < current_holdout_mae:
        return {"replace": True,
                "reason": (f"Candidate holdout MAE {candidate_holdout_mae:.3f} d "
                           f"< current {current_holdout_mae:.3f} d.")}
    return {"replace": False,
            "reason": (f"Candidate not better "
                       f"({candidate_holdout_mae:.3f} d vs "
                       f"{current_holdout_mae:.3f} d); keeping current model.")}
