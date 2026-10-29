"""Service layer - one implementation of every business workflow.

The command-line tools, the Flask API and the Next.js front-end all call these
functions, so behaviour (validation, date arithmetic, fallback rules,
honest-refusal messages) can never drift between front-ends.

Every function takes a :class:`plantreadiness.repository.BaseRepository`
instead of a raw SQLite connection, so the storage backend stays isolated
behind one seam (currently the local SQLite file on this device).

Invariants preserved here (task sections 3, 8, 10):

* measurements are never invented - derived values (age, growth rate) are
  computed only from a plant's own ``date_started`` / prior measurements and
  are returned with an explicit ``derived`` flag,
* readiness labels come only from recorded readiness events,
* the prediction fallback order (Level 1 -> 4) and the exact
  honest-refusal message from :mod:`plantreadiness.config` are reused
  verbatim,
* synthetic output always carries the ``SYNTHETIC DEMO`` banner,
* feedback due date = transplant date + FEEDBACK_WINDOW_DAYS, duplicates
  blocked, corrections allowed only through an audited path.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config
from . import model as mdl
from . import reference as ref
from .db import (DuplicateRecordError, NotFoundError, ValidationError,
                 age_days_for_plant, age_source_for_plant,
                 compute_growth, days_between, feedback_due_date,
                 is_height_outlier, parse_iso_date)
from .repository import BaseRepository
from . import camera_import
from .camera_refresh import refresh_camera_observations

# Re-exported so front-ends import workflow errors from one place.
__all__ = [
    "ConfigurationError", "auto_age_days", "auto_growth_rate",
    "register_plant", "record_observation", "record_transplant",
    "submit_feedback", "correct_feedback", "feedback_state",
    "record_readiness_event", "list_readiness_events",
    "import_camera_measurements", "refresh_camera_observations",
    "list_pending_captures", "review_captures",
    "predict_for_plant", "predict_ad_hoc", "predict_species_cohort",
    "model_status",
    "species_model_plan", "preview_csv_import", "run_csv_import",
    "export_table_csv", "dashboard_summary",
    "build_reference_baseline", "load_reference_baseline",
    "save_prediction_snapshot", "list_prediction_history",
    "record_readiness_assessment", "list_readiness_assessments",
]


class ConfigurationError(Exception):
    """Backend/secrets not configured - front-end must show setup help."""


# --------------------------------------------------------------------------- #
# Derived-value helpers (auto-calculation, never invented)
# --------------------------------------------------------------------------- #


def auto_age_days(date_started: Optional[str],
                  observation_date: str,
                  *,
                  initial_age_days: Optional[int] = None,
                  initial_age_measured_on: Optional[str] = None
                  ) -> Optional[int]:
    """Age in days derived from the plant's own recorded evidence.

    Uses the exact ``date_started`` when present, otherwise a user-supplied
    initial age observed on a known date (nursery seedlings). Returns ``None``
    when neither is available or the observation predates the evidence -
    callers must then treat age as "not measurable" rather than guessing.
    """
    return age_days_for_plant(
        {"date_started": date_started,
         "initial_age_days": initial_age_days,
         "initial_age_measured_on": initial_age_measured_on},
        observation_date)


def resolve_age_days(plant: dict, observation_date: str
                     ) -> tuple[Optional[int], str]:
    """Return ``(age_days, age_source)`` for a plant on a given date."""
    return (age_days_for_plant(plant, observation_date),
            age_source_for_plant(plant))


def auto_growth_rate(previous_height_cm: Optional[float],
                     previous_date: Optional[str],
                     height_cm: Optional[float],
                     observation_date: str) -> dict:
    """Growth rate (cm/day) derived from the previous real measurement.

    Returns ``{"value": float|None, "derived": bool, "reason": str|None}``.
    ``value`` is ``None`` unless *both* heights and *both* dates exist and
    the interval is positive - a dead plant, a first measurement or an
    unmeasurable value never fabricates a rate.
    """
    if height_cm is None or previous_height_cm is None:
        return {"value": None, "derived": False,
                "reason": "no previous measurement with a height - rate "
                          "cannot be derived"}
    if previous_date is None:
        return {"value": None, "derived": False,
                "reason": "previous measurement has no date"}
    try:
        span = days_between(previous_date, observation_date)
    except ValidationError as exc:
        return {"value": None, "derived": False, "reason": str(exc)}
    if span <= 0:
        return {"value": None, "derived": False,
                "reason": "observation date must be after the previous "
                          "measurement"}
    delta = float(height_cm) - float(previous_height_cm)
    return {"value": round(delta / span, 4), "derived": True, "reason": None}


# --------------------------------------------------------------------------- #
# Plant / observation workflows
# --------------------------------------------------------------------------- #


def register_plant(repo: BaseRepository, **fields) -> int:
    """Register a plant (validation and duplicate rules live in the repo)."""
    return repo.register_plant(**fields)


def record_observation(repo: BaseRepository, *, plant_id: int,
                       observation_date: str,
                       height_cm: Optional[float] = None,
                       age_days: Optional[int] = None,
                       growth_rate_cm_per_day: Optional[float] = None,
                       canopy_width_cm: Optional[float] = None,
                       stem_width_cm: Optional[float] = None,
                       visible_true_leaf_count: Optional[int] = None,
                       data_source: Optional[str] = None,
                       derive_missing: bool = True) -> dict:
    """Append one observation, auto-calculating age / growth rate.

    Manual values always win; derived values are only computed when the
    caller did not supply them *and* ``derive_missing`` is on. The returned
    dict reports which values were derived so the UI can label them.
    """
    plant = repo.get_plant(plant_id)
    if plant is None:
        raise NotFoundError(f"No plant with plant_id={plant_id}.")

    derived: dict[str, Optional[str]] = {}

    if age_days is None and derive_missing:
        age, source = resolve_age_days(plant, observation_date)
        if age is not None:
            age_days = age
            derived["age_days"] = f"derived from {source}"

    if growth_rate_cm_per_day is None and derive_missing:
        prev = repo.latest_observation(plant_id)
        rate = auto_growth_rate(
            prev.get("height_cm") if prev else None,
            prev.get("observation_date") if prev else None,
            height_cm, observation_date)
        if rate["value"] is not None:
            growth_rate_cm_per_day = rate["value"]
            derived["growth_rate_cm_per_day"] = rate["reason"] or "derived"

    obs_id = repo.add_observation(
        plant_id=plant_id, observation_date=observation_date,
        age_days=age_days, height_cm=height_cm,
        growth_rate_cm_per_day=growth_rate_cm_per_day,
        canopy_width_cm=canopy_width_cm,
        stem_width_cm=stem_width_cm,
        visible_true_leaf_count=visible_true_leaf_count,
        data_source=data_source)
    return {"observation_id": obs_id, "derived": derived,
            "age_days": age_days,
            "growth_rate_cm_per_day": growth_rate_cm_per_day}


# --------------------------------------------------------------------------- #
# Readiness event workflow (ground truth labels)
# --------------------------------------------------------------------------- #


def record_readiness_event(
    repo: BaseRepository,
    *,
    plant_id: int,
    readiness_observed_date: str,
    readiness_definition_version: str,
    assessment_source: str,
    notes: Optional[str] = None,
    readiness_observed_age_days: Optional[int] = None,
    data_source: Optional[str] = None,
) -> dict:
    """Record an empirical observation of seedling transplant readiness.

    This is the ONLY source of ground truth readiness training labels.
    Readiness is validated against a documented definition version and observer.
    """
    plant = repo.get_plant(plant_id)
    if plant is None:
        raise NotFoundError(f"No plant with plant_id={plant_id}.")

    eid = repo.record_readiness_event(
        plant_id=plant_id,
        readiness_observed_date=readiness_observed_date,
        readiness_definition_version=readiness_definition_version,
        assessment_source=assessment_source,
        readiness_observed_age_days=readiness_observed_age_days,
        data_source=data_source or plant.get("data_source", "real"),
        notes=notes,
    )
    return {
        "readiness_event_id": eid,
        "plant_id": plant_id,
        "species": plant.get("species"),
        "variety": plant.get("variety"),
        "readiness_observed_date": readiness_observed_date,
        "readiness_definition_version": readiness_definition_version,
        "assessment_source": assessment_source,
    }


def list_readiness_events(
    repo: BaseRepository,
    *,
    plant_id: Optional[int] = None,
    data_source: Optional[str] = None,
) -> list[dict]:
    """Retrieve recorded readiness events."""
    return repo.list_readiness_events(plant_id=plant_id, data_source=data_source)


def import_camera_measurements(
    repo: BaseRepository,
    source,
    *,
    default_species: str = "Super Orange",
    default_variety: Optional[str] = None,
    auto_register: bool = True,
    data_source: str = "real",
    initial_age_days: Optional[int] = None,
    initial_age_measured_on: Optional[str] = None,
) -> dict:
    """Import ESP32-CAM / OpenCV daily measurements into the repository."""
    from . import camera_import
    return camera_import.import_camera_measurements(
        repo,
        source,
        default_species=default_species,
        default_variety=default_variety,
        auto_register=auto_register,
        data_source=data_source,
        initial_age_days=initial_age_days,
        initial_age_measured_on=initial_age_measured_on,
    )


# --------------------------------------------------------------------------- #
# Transplant + 14-day feedback workflows
# --------------------------------------------------------------------------- #


def record_transplant(repo: BaseRepository, *, plant_id: int,
                      transplant_date: str,
                      height_at_transplant_cm: Optional[float] = None,
                      data_source: Optional[str] = None,
                      transplant_occurred: bool = True,
                      condition_at_transplant: Optional[str] = None,
                      transplant_notes: Optional[str] = None) -> dict:
    """Create the transplant event and its pending 14-day feedback record.

    A readiness assessment never implies a transplant: pass
    ``transplant_occurred=False`` to record a decision/deferral; no feedback
    is then due.
    """
    return repo.record_transplant(
        plant_id=plant_id, transplant_date=transplant_date,
        height_at_transplant_cm=height_at_transplant_cm,
        data_source=data_source,
        transplant_occurred=transplant_occurred,
        condition_at_transplant=condition_at_transplant,
        transplant_notes=transplant_notes)


def feedback_state(row: dict, as_of: Optional[date] = None) -> dict:
    """Classify a feedback row: pending / due / overdue / completed."""
    as_of = as_of or date.today()
    completed = row.get("feedback_submitted_date") is not None
    due = parse_iso_date(row["feedback_due_date"])
    delta_days = (due - as_of).days  # >0 future, 0 due today, <0 overdue
    if completed:
        status = "completed"
    elif delta_days < 0:
        status = "overdue"
    elif delta_days == 0:
        status = "due"
    else:
        status = "pending"
    return {"status": status, "completed": completed,
            "due_date": due.isoformat(), "days_until_due": delta_days,
            "is_overdue": (not completed and delta_days < 0),
            "days_overdue": max(-delta_days, 0)}


def _growth_for(row: dict,
                height_after_14d_cm: Optional[float]) -> Optional[float]:
    """Growth = after 14d - at transplant, only when both heights exist."""
    height_at = row.get("height_at_transplant_cm")
    if height_at is None:
        # Accept either key for the transplant height.
        height_at = row.get("transplant_height_cm")
    return compute_growth(height_at, height_after_14d_cm)


def submit_feedback(repo: BaseRepository, *, feedback_id: int,
                    feedback_submitted_date: str,
                    survived_after_14d: bool,
                    height_after_14d_cm: Optional[float] = None,
                    visible_condition: Optional[str] = None,
                    user_notes: Optional[str] = None) -> dict:
    """Submit the 14-day outcome once (duplicates rejected by the repo)."""
    return repo.submit_feedback(
        feedback_id=feedback_id,
        feedback_submitted_date=feedback_submitted_date,
        survived_after_14d=survived_after_14d,
        height_after_14d_cm=height_after_14d_cm,
        visible_condition=visible_condition, user_notes=user_notes)


def correct_feedback(repo: BaseRepository, *, feedback_id: int,
                     feedback_submitted_date: Optional[str] = None,
                     survived_after_14d: Optional[bool] = None,
                     height_after_14d_cm: Optional[float] = None,
                     visible_condition: Optional[str] = None,
                     user_notes: Optional[str] = None,
                     reason: str) -> dict:
    """Amend an already-submitted outcome, preserving an audit record.

    The original values are written to ``feedback_corrections`` *before* the
    update, so history is never silently lost. Requires an explicit reason.
    """
    if not (reason or "").strip():
        raise ValidationError(
            "A correction must state a reason - audit history is mandatory.")
    existing = repo.get_feedback(feedback_id)
    if existing is None:
        raise NotFoundError(f"No feedback record with feedback_id={feedback_id}.")
    if existing.get("feedback_submitted_date") is None:
        raise ValidationError(
            "Only submitted feedback can be corrected - submit it first.")

    previous = {k: existing.get(k) for k in (
        "feedback_submitted_date", "survived_after_14d",
        "height_after_14d_cm", "growth_after_14d_cm", "visible_condition",
        "user_notes")}
    repo.record_feedback_correction(feedback_id, previous, reason=reason)

    # Reuse the standard submit path for validation, then relax the
    # duplicate guard by writing through a targeted update helper.
    new_values = {
        "feedback_submitted_date":
            feedback_submitted_date or existing["feedback_submitted_date"],
        "survived_after_14d": (existing["survived_after_14d"]
                               if survived_after_14d is None
                               else survived_after_14d),
        "height_after_14d_cm": (existing.get("height_after_14d_cm")
                                if height_after_14d_cm is None
                                else height_after_14d_cm),
        "visible_condition": (visible_condition
                              if visible_condition is not None
                              else existing.get("visible_condition")),
        "user_notes": (user_notes if user_notes is not None
                       else existing.get("user_notes")),
    }
    return repo.update_feedback(feedback_id, new_values,
                                expected_submitted=True)


# --------------------------------------------------------------------------- #
# Prediction (shared with prediction.py CLI)
# --------------------------------------------------------------------------- #


def predict_for_plant(repo: BaseRepository, plant_id: int, models_dir: Path,
                      allow_provisional_baseline: bool = False) -> dict:
    """Species-aware prediction for a registered plant's latest observation.

    When no validated model is available AND
    ``allow_provisional_baseline`` is set, the successful-cohort reference
    baseline may supply a clearly-labelled *provisional* estimate. The flag
    defaults to off so an ordinary real-plant prediction still refuses
    honestly (see the honest-refusal contract).
    """
    plant = repo.get_plant(plant_id)
    if plant is None:
        raise NotFoundError(f"No plant with plant_id={plant_id}.")
    species = plant["species"]
    variety = plant["variety"]
    plant_source = plant["data_source"]

    obs = repo.latest_observation(plant_id)
    if obs is None:
        return {
            "available": False,
            "message": "No observations recorded for this plant yet - "
                       "the system is still collecting data.",
            "plant_id": plant_id,
            "species": species,
            "variety": variety,
            "plant_data_source": plant_source,
            "readiness_event_counts": repo.readiness_event_counts(
                species, variety, data_source="real"),
        }

    selection = mdl.select_model(species, variety, models_dir)
    # Real plants use only validated real models.
    mdl.load_selection(selection, models_dir)

    history_by_plant = _accepted_plant_history(
        repo, [plant_id], obs["observation_date"]).get(plant_id, {})
    accepted_history = history_by_plant.get("accepted", [])
    latest_flagged = any(
        o.get("observation_id") == obs["observation_id"]
        for o in history_by_plant.get("flagged", []))
    age_source = age_source_for_plant(plant)

    observation = {
        "age_days": obs["age_days"],
        "height_cm": obs["height_cm"],
        "growth_rate_cm_per_day": obs["growth_rate_cm_per_day"],
        "stem_width_cm": obs.get("stem_width_cm"),
        "visible_true_leaf_count": obs["visible_true_leaf_count"],
        "species": species,
        "variety": variety,
        "observation_date": obs["observation_date"],
    }
    result = mdl.predict_days_remaining(selection, observation)
    result.update({
        "plant_id": plant_id,
        "species": species,
        "variety": variety,
        "plant_data_source": plant_source,
        "observation_date": obs["observation_date"],
        "observation": dict(obs),
        "age_source": age_source,
        "latest_reading_flagged_suspect": latest_flagged,
        "readiness_event_counts": repo.readiness_event_counts(
            species, variety, data_source="real"),
    })
    if not result.get("available") and allow_provisional_baseline:
        result = _apply_provisional_baseline(
            result, models_dir, species, variety, observation,
            history=accepted_history, age_source=age_source)
    result.setdefault("provisional", False)
    result.setdefault("method", _method_for(result))
    result.setdefault("age_source", age_source)
    return result


def predict_ad_hoc(species: str, variety: Optional[str], observation: dict,
                   models_dir: Path) -> dict:
    """Prediction from caller-supplied values (no stored plant required)."""
    selection = mdl.select_model(species, variety, models_dir)
    mdl.load_selection(selection, models_dir)
    observation = dict(observation)
    observation["species"] = species
    observation["variety"] = variety
    return mdl.predict_days_remaining(selection, observation)


# --------------------------------------------------------------------------- #
# Species-level prediction (latest cloud cohort)
# --------------------------------------------------------------------------- #


def _median(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _median_int(values: Iterable[Optional[float]]) -> Optional[int]:
    med = _median(values)
    return None if med is None else int(round(med))


def _rounded(values: Iterable[Optional[float]],
             digits: int = 4) -> Optional[float]:
    med = _median(values)
    return None if med is None else round(float(med), digits)


def _cohort_age_source(repo: BaseRepository, rows: list[dict]) -> str:
    """Honest provenance label for the cohort's age information."""
    sources = set()
    for plant_id in {r.get("plant_id") for r in rows}:
        if plant_id is None:
            continue
        plant = repo.get_plant(plant_id)
        if not plant:
            continue
        source = age_source_for_plant(plant)
        if source != "unknown":
            sources.add(source)
    if not sources:
        return "unknown"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def _accepted_plant_history(repo: BaseRepository, plant_ids: list[int],
                            up_to: str) -> dict[int, dict]:
    """Accepted and suspect observations per plant, up to ``up_to``.

    A height more than ``HEIGHT_JUMP_SUSPECT_RATIO`` away from the trailing
    median is flagged and excluded from the accepted series. It is never
    deleted and remains available for human review.
    """
    result: dict[int, dict] = {}
    for plant_id in plant_ids:
        try:
            observations = [dict(o) for o in repo.get_observations(plant_id)]
        except Exception:
            observations = []
        observations = [
            o for o in observations
            if o.get("observation_date") and o["observation_date"] <= up_to
        ]
        observations.sort(key=lambda o: (o["observation_date"],
                                         o.get("observation_id") or 0))
        accepted: list[dict] = []
        flagged: list[dict] = []
        for observation in observations:
            height = observation.get("height_cm")
            is_camera = (
                observation.get("calibration_source")
                == config.CALIBRATION_SOURCE_CAMERA)
            already_flagged = bool(observation.get("quality_flag"))
            if already_flagged or (
                is_camera and height is not None
                and is_height_outlier(
                    height, [a.get("height_cm") for a in accepted])):
                if not observation.get("quality_note"):
                    observation["quality_note"] = (
                        "Height differs by more than "
                        f"{int(config.HEIGHT_JUMP_SUSPECT_RATIO * 100)}% from "
                        "the previous accepted height. Kept for review; "
                        "excluded from growth rates and medians.")
                observation["quality_flag"] = (
                    observation.get("quality_flag")
                    or "height_jump_suspect")
                flagged.append(observation)
            else:
                accepted.append(observation)
        result[plant_id] = {"accepted": accepted, "flagged": flagged}
    return result


def _cohort_context(repo: BaseRepository, species: str,
                    variety: Optional[str],
                    observation_date: Optional[str] = None,
                    plant_ids: Optional[list[int]] = None) -> Optional[dict]:
    """Aggregate one date's usable observations for a species cohort."""
    snapshot = repo.species_cohort_snapshot(species, variety, observation_date)
    rows = [dict(r) for r in snapshot.get("rows", [])]
    target_date = snapshot.get("observation_date")
    if plant_ids is not None:
        allowed = set(plant_ids)
        rows = [r for r in rows if r.get("plant_id") in allowed]
    if not rows or not target_date:
        return None

    cohort_plant_ids = sorted({r["plant_id"] for r in rows
                               if r.get("plant_id") is not None})
    history_by_plant = _accepted_plant_history(
        repo, cohort_plant_ids, target_date)
    accepted_ids = {
        o.get("observation_id")
        for group in history_by_plant.values()
        for o in group["accepted"]
    }
    flagged_ids = {
        o.get("observation_id")
        for group in history_by_plant.values()
        for o in group["flagged"]
    }
    usable_rows = [r for r in rows if r.get("observation_id") in accepted_ids]
    suspect_rows_excluded = sum(
        1 for r in rows if r.get("observation_id") in flagged_ids)

    history = [o for group in history_by_plant.values()
               for o in group["accepted"]]
    history_dates = sorted({o["observation_date"] for o in history})

    heights = [r.get("height_cm") for r in usable_rows]
    ages = [r.get("age_days") for r in usable_rows]
    rates = [r.get("growth_rate_cm_per_day") for r in usable_rows]
    canopy = [r.get("canopy_width_cm") for r in usable_rows]
    stem = [
        r.get("stem_width_cm") for r in usable_rows
        if r.get("stem_width_cm") is not None
        and float(r["stem_width_cm"]) > 0
    ]
    leaves = [r.get("visible_true_leaf_count") for r in usable_rows]
    age_source = _cohort_age_source(repo, rows)

    cohort_snapshot = {
        "aggregation": "population_median",
        "plants_represented": len({r["plant_id"] for r in usable_rows}),
        "row_count": len(usable_rows),
        "rows_total": len(rows),
        "suspect_rows_excluded": suspect_rows_excluded,
        "observation_date": target_date,
        "median_height_cm": _rounded(heights),
        "min_height_cm": round(
            min(float(h) for h in heights if h is not None), 4
        ) if any(h is not None for h in heights) else None,
        "max_height_cm": round(
            max(float(h) for h in heights if h is not None), 4
        ) if any(h is not None for h in heights) else None,
        "median_age_days": _median_int(ages),
        "median_growth_rate_cm_per_day": _rounded(rates),
        "median_canopy_width_cm": _rounded(canopy),
        "median_stem_width_cm": _rounded(stem),
        "median_visible_true_leaf_count": _median_int(leaves),
        "missing_height_count": sum(1 for h in heights if h is None),
        "missing_age_count": sum(1 for a in ages if a is None),
        "age_source": age_source,
    }

    representative_observation = {
        "age_days": cohort_snapshot["median_age_days"],
        "height_cm": cohort_snapshot["median_height_cm"],
        "growth_rate_cm_per_day":
            cohort_snapshot["median_growth_rate_cm_per_day"],
        "canopy_width_cm": cohort_snapshot["median_canopy_width_cm"],
        "stem_width_cm": cohort_snapshot["median_stem_width_cm"],
        "visible_true_leaf_count":
            cohort_snapshot["median_visible_true_leaf_count"],
        "observation_date": target_date,
    }

    return {
        "species": species,
        "variety": variety,
        "target_date": target_date,
        "rows": rows,
        "usable_rows": usable_rows,
        "plant_ids": cohort_plant_ids,
        "cohort_snapshot": cohort_snapshot,
        "representative_observation": representative_observation,
        "history": history,
        "history_dates": history_dates,
        "age_source": age_source,
        "suspect_rows_excluded": suspect_rows_excluded,
    }


def _estimate_context(context: dict, models_dir: Path,
                      allow_provisional_baseline: bool) -> dict:
    """Model prediction (or provisional fallback) for one cohort context."""
    result = predict_ad_hoc(
        context["species"], context["variety"],
        context["representative_observation"], models_dir)
    result.update({
        "species": context["species"],
        "variety": context["variety"],
        "observation_date": context["target_date"],
        "prediction_scope": "species_cohort",
        "cohort_snapshot": context["cohort_snapshot"],
    })
    if not result.get("available") and allow_provisional_baseline:
        result = _apply_provisional_baseline(
            result, models_dir, context["species"], context["variety"],
            context["representative_observation"],
            history=context["history"],
            age_source=context["age_source"])
        result["prediction_scope"] = "species_cohort"
    result.setdefault("provisional", False)
    result.setdefault("method", _method_for(result))
    result.setdefault("age_source", context["age_source"])
    return result


def _apply_date_stability(repo: BaseRepository, context: dict, result: dict,
                          models_dir: Path,
                          allow_provisional_baseline: bool) -> dict:
    """Never move readiness later while the plant is still growing.

    Compares with the previous usable observation date. A later date is only
    allowed when the median height did not increase (a genuine stall or
    regression). A 1% dead-band keeps measurement noise from counting as
    growth.
    """
    result.setdefault("date_capped_to_previous", False)
    result.setdefault("date_stability", None)
    result.setdefault("previous_predicted_readiness_date", None)
    result.setdefault("stability_note", None)

    # The no-later rule applies to provisional estimates. A validated model
    # has its own uncertainty handling and is never silently overridden.
    if not result.get("provisional"):
        return result

    current_prediction = result.get("predicted_readiness_date")
    previous_dates = [d for d in context["history_dates"]
                      if d < context["target_date"]]
    if not current_prediction or not previous_dates:
        return result
    previous_date = previous_dates[-1]
    previous_context = _cohort_context(
        repo, context["species"], context["variety"],
        observation_date=previous_date, plant_ids=context["plant_ids"])
    if not previous_context or not previous_context["usable_rows"]:
        return result

    previous_result = _estimate_context(
        previous_context, models_dir, allow_provisional_baseline)
    previous_prediction = previous_result.get("predicted_readiness_date")
    if not previous_prediction:
        return result

    current_height = context["cohort_snapshot"].get("median_height_cm")
    previous_height = previous_context["cohort_snapshot"].get(
        "median_height_cm")
    growing = (current_height is not None and previous_height is not None
               and float(current_height) > float(previous_height) * 1.01)
    growth = None
    if current_height is not None and previous_height is not None:
        growth = round(float(current_height) - float(previous_height), 4)

    result["previous_observation_date"] = previous_date
    result["previous_predicted_readiness_date"] = previous_prediction
    result["previous_median_height_cm"] = previous_height
    result["growth_since_previous_cm"] = growth
    result["plant_grew_since_previous"] = growing
    result["previous_method"] = previous_result.get("method")
    result["previous_estimate_basis"] = previous_result.get("estimate_basis")

    current_basis = result.get("estimate_basis")
    previous_basis = previous_result.get("estimate_basis")
    if (current_basis and previous_basis
            and current_basis != previous_basis):
        result["date_stability"] = "method_changed"
        result["stability_note"] = (
            "The estimate basis changed from "
            f"'{previous_basis}' to '{current_basis}' as more evidence became "
            "available. The date is re-anchored to the new evidence; later "
            "movement will be held while the plant keeps growing.")
        return result

    if growing and current_prediction > previous_prediction:
        held_days = max(0, days_between(context["target_date"],
                                        previous_prediction))
        result["predicted_readiness_date"] = previous_prediction
        result["days_remaining"] = held_days
        result["date_capped_to_previous"] = True
        result["date_stability"] = "held_while_growing"
        result["stability_note"] = (
            "The plant grew since the previous reading, so the readiness "
            "date was held at the previous estimate instead of moving later.")
    elif not growing and current_prediction > previous_prediction:
        result["date_stability"] = "later_after_stall"
        result["stability_note"] = (
            "Height did not increase since the previous reading; a later "
            "date is allowed because the evidence indicates a stall.")
    else:
        result["date_stability"] = "stable"
    return result


def predict_species_cohort(repo: BaseRepository, species: str,
                           variety: Optional[str] = None,
                           models_dir: Path = Path("models"),
                           observation_date: Optional[str] = None,
                           allow_provisional_baseline: bool = False) -> dict:
    """Predict readiness for a species using its latest observed cohort.

    The UI calls this instead of asking the user to choose an individual
    plant. The backend selects the newest cloud/data date for the species,
    aggregates the observations recorded on that date, and passes the
    representative observation to the existing prediction workflow.
    """
    models_dir = Path(models_dir)
    context = _cohort_context(repo, species, variety, observation_date)

    event_counts = repo.readiness_event_counts(species, variety,
                                                data_source="real")

    species_label = f"{species}"
    if variety:
        species_label += f" / {variety}"

    if context is None:
        return {
            "available": False,
            "message": (
                f"No real observations found for {species_label} on the "
                "latest available date - the system is still collecting data."
            ),
            "species": species,
            "variety": variety,
            "observation_date": None,
            "cohort_snapshot": {
                "aggregation": "population_median",
                "plants_represented": 0,
                "row_count": 0,
                "observation_date": None,
            },
            "readiness_event_counts": event_counts,
            "provisional": False,
            "method": config.METHOD_UNAVAILABLE,
        }

    if not context["usable_rows"]:
        return {
            "available": False,
            "message": (
                f"The latest {species_label} reading "
                f"({context['target_date']}) was flagged as a suspect height "
                "jump (>50% from the previous accepted height). It is kept for review "
                "but excluded from prediction; record a verified reading or "
                "wait for the next measurement."
            ),
            "species": species,
            "variety": variety,
            "observation_date": context["target_date"],
            "cohort_snapshot": context["cohort_snapshot"],
            "readiness_event_counts": event_counts,
            "suspect_rows_excluded": context["suspect_rows_excluded"],
            "age_source": context["age_source"],
            "provisional": False,
            "method": config.METHOD_UNAVAILABLE,
        }

    result = _estimate_context(
        context, models_dir, allow_provisional_baseline)
    result.update({
        "species": species,
        "variety": variety,
        "observation_date": context["target_date"],
        "prediction_scope": "species_cohort",
        "cohort_snapshot": context["cohort_snapshot"],
        "readiness_event_counts": event_counts,
        "suspect_rows_excluded": context["suspect_rows_excluded"],
        "age_source": context["age_source"],
    })
    result = _apply_date_stability(
        repo, context, result, models_dir, allow_provisional_baseline)
    # How many staged OpenCV readings still await human review for this
    # species. They were deliberately NOT folded into the prediction above.
    try:
        groups = repo.pending_capture_groups(species=species, variety=variety)
        result["pending_review_count"] = sum(
            len(g.get("captures", [])) for g in groups)
    except Exception:
        result["pending_review_count"] = 0
    return result


# --------------------------------------------------------------------------- #
# Model / species status (dashboard + species page)
# --------------------------------------------------------------------------- #


def list_pending_captures(repo: BaseRepository, *, species=None, variety=None,
                         data_source: str = "real") -> list[dict]:
    """Staged OpenCV readings awaiting human review, grouped by plant/date."""
    groups = repo.pending_capture_groups(species=species, variety=variety,
                                        data_source=data_source)
    # Attach a proposed daily median for each group (accepted-valid captures).
    for g in groups:
        valid = [c for c in g["captures"]
                 if c.get("validity") == "valid"
                 and c.get("review_status") == "pending"]
        heights = [c.get("height_cm") for c in valid]
        stems = [
            c.get("stem_width_cm") for c in valid
            if c.get("stem_width_cm") is not None
            and float(c["stem_width_cm"]) > 0
        ]
        leaves = [c.get("visible_true_leaf_count") for c in valid]
        proposed_height = _median_num(heights)
        trailing: list[float] = []
        try:
            prior = [
                o for o in repo.get_observations(g["plant_id"])
                if o.get("observation_date")
                and o["observation_date"] < g["observation_date"]
                and o.get("height_cm") is not None
            ]
            prior.sort(key=lambda o: o["observation_date"])
            trailing = [float(o["height_cm"]) for o in prior[-3:]]
        except Exception:
            trailing = []
        suspect = is_height_outlier(proposed_height, trailing)
        g["proposed"] = {
            "median_height_cm": proposed_height,
            "median_stem_width_cm": _median_num(stems),
            "median_visible_true_leaf_count": _median_int(leaves),
            "n_valid": len(valid),
            "n_total": len(g["captures"]),
            "suspect": suspect,
            "suspect_note": (
                "Proposed height differs by more than "
                f"{int(config.HEIGHT_JUMP_SUSPECT_RATIO * 100)}% from the "
                "previous accepted height. Accepting stores it flagged; it will be "
                "excluded from growth rates and medians." if suspect else None),
        }
        for c in g["captures"]:
            c["image_url"] = c.get("image_url") or _capture_image_url(c.get("public_id"))
            c["quality_flag"] = (
                "height_jump_suspect"
                if is_height_outlier(c.get("height_cm"), trailing) else None)
    return groups


def _median_num(values):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return round(vals[mid], 4)
    return round((vals[mid - 1] + vals[mid]) / 2, 4)


def review_captures(repo: BaseRepository, *, capture_ids: list[int],
                    action: str, reason: Optional[str] = None) -> dict:
    """Accept or discard staged captures; accepted ones rebuild daily obs."""
    if action not in ("accept", "discard"):
        raise ValidationError(f"review action must be 'accept' or 'discard', got {action!r}.")
    db_action = "accepted" if action == "accept" else "discarded"
    reviewed_at = datetime.now(timezone.utc).isoformat()
    rows = repo.review_captures(capture_ids, db_action, reviewed_at=reviewed_at)
    rebuilt = []
    if db_action == "accepted":
        groups = {}
        for r in rows:
            groups.setdefault((r["plant_id"], r["observation_date"]), True)
        for (plant_id, obs_date) in groups:
            try:
                rebuilt.append(camera_import.rebuild_daily_observation_from_captures(
                    repo, plant_id, obs_date, reason=reason or "reviewed accept"))
            except Exception as exc:
                rebuilt.append({"plant_id": plant_id, "observation_date": obs_date,
                                "error": str(exc)})
    return {"reviewed": len(rows), "action": db_action,
            "rebuilt": rebuilt, "capture_ids": [r["capture_id"] for r in rows]}


def _capture_image_url(public_id):
    if not public_id:
        return None
    cloud = getattr(config, "CLOUDINARY_CLOUD_NAME", None) or __import__("os").getenv("CLOUDINARY_CLOUD_NAME")
    if not cloud:
        return None
    return f"https://res.cloudinary.com/{cloud}/image/upload/w_320,c_fill/{public_id}.jpg"


def model_status(models_dir: Path) -> dict:
    """Real vs synthetic artefact summary for the dashboard."""
    out = {}
    for source in config.VALID_SOURCES:
        try:
            entries = mdl.list_artifacts(models_dir, source)
        except FileNotFoundError:
            entries = []
        active = [e for e in entries if e.get("active", True)]
        out[source] = {
            "total": len(entries),
            "active": len(active),
            "validated": sum(
                1 for e in active
                if e["metadata"].get("validated")),
            "artifacts": [
                {"model_name": e["metadata"].get("model_name"),
                 "scope_species": e["metadata"].get("scope_species"),
                 "scope_variety": e["metadata"].get("scope_variety"),
                 "validated": e["metadata"].get("validated"),
                 "n_independent_plants":
                     e["metadata"].get("n_independent_plants"),
                 "n_labelled_readiness_events":
                     e["metadata"].get("n_labelled_readiness_events"),
                 "mae_days": (e["metadata"].get("validation_metrics") or {}
                              ).get("mae_days"),
                 "trained_at": e["metadata"].get("trained_at")}
                for e in active],
        }
    return out


def species_model_plan(repo: BaseRepository, species: str,
                       variety: Optional[str], models_dir: Path) -> dict:
    """Which fallback level *would* be used for this species/variety."""
    selection = mdl.select_model(species, variety, models_dir)
    real_counts = repo.readiness_event_counts(species, variety,
                                              data_source="real")
    return {
        "level": selection["level"],
        "message": selection["message"],
        "data_source": selection["data_source"],
        "model_name": (selection.get("entry") or {}).get(
            "metadata", {}).get("model_name"),
        "real_readiness_event_counts": real_counts,
        "meets_min_requirements": (
            real_counts["independent_plants"] >= config.MIN_INDEPENDENT_PLANTS
            and real_counts["readiness_events"] >= config.MIN_READINESS_EVENTS),
        "min_requirements": {
            "independent_plants": config.MIN_INDEPENDENT_PLANTS,
            "readiness_events": config.MIN_READINESS_EVENTS},
    }


# --------------------------------------------------------------------------- #
# Reference baseline + prediction history + readiness assessments
# --------------------------------------------------------------------------- #


def _method_for(result: dict) -> str:
    """Label which method produced a prediction result (for snapshots)."""
    explicit = result.get("method")
    if explicit:
        return explicit
    if result.get("available"):
        return config.METHOD_VALIDATED_MODEL
    return config.METHOD_UNAVAILABLE


def _coerce_level(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _choose_provisional_estimate(baseline: dict, observation: dict,
                                 history: Optional[list[dict]]) -> dict:
    """Pick trajectory (no age) or historical timing, with honest fallbacks."""
    history = history or []
    growth_history_n = len({p.get("observation_date") for p in history
                            if p.get("observation_date")})
    trajectory = None
    if observation.get("age_days") is None:
        trajectory = ref.trajectory_estimate(baseline, observation, history)
        if trajectory.get("available"):
            return trajectory
    timing = ref.provisional_estimate(
        baseline, observation, growth_history_n=growth_history_n)
    if timing.get("available"):
        if trajectory is not None and not trajectory.get("available"):
            timing["trajectory_note"] = trajectory.get("message")
        return timing
    if trajectory is None:
        trajectory = ref.trajectory_estimate(baseline, observation, history)
    if trajectory.get("available"):
        return trajectory
    # Neither worked: prefer the timing message when it is specific, otherwise
    # surface the trajectory explanation.
    if timing.get("message"):
        timing["trajectory_note"] = trajectory.get("message")
        return timing
    return trajectory


def _apply_provisional_baseline(result: dict, models_dir: Path, species: str,
                                variety: Optional[str],
                                observation: Optional[dict],
                                history: Optional[list[dict]] = None,
                                age_source: Optional[str] = None) -> dict:
    """Replace an unavailable result with a provisional baseline estimate.

    Only called when the model-based prediction was unavailable, so a working
    model is never silently replaced. The estimate stays explicitly labelled
    provisional and the reference transplant date is flagged as an assumption.
    """
    baseline = ref.load_baseline(Path(models_dir), species, variety)
    estimate = _choose_provisional_estimate(
        baseline, observation or {}, history)
    merged = dict(result)
    if not estimate.get("available"):
        merged["provisional"] = False
        merged["baseline_available"] = bool(baseline)
        merged["baseline_note"] = estimate.get("message")
        merged["trajectory_note"] = estimate.get("trajectory_note")
        return merged
    passthrough = (
        "days_remaining", "days_remaining_raw",
        "days_remaining_uncertainty_days", "predicted_readiness_date",
        "reference_basis", "reference_group", "n_independent_plants",
        "reference_transplant_is_assumption", "insufficient_growth_history",
        "history_note", "baseline_version", "estimate_basis",
        "target_height_cm", "current_height_cm", "remaining_growth_cm",
        "robust_growth_rate_cm_per_day", "n_history_readings",
        "history_span_days", "history_first_date", "history_last_date",
        "horizon_capped", "cohort_growth_rate_cm_per_day",
        "plant_growth_rate_cm_per_day", "growth_rate_adjustment_factor",
        "growth_rate_adjustment_applied", "growth_rate_adjustment_reason",
        "trajectory_note",
    )
    merged.update({key: estimate.get(key) for key in passthrough})
    merged.update({
        "available": True,
        "method": config.METHOD_REFERENCE_BASELINE,
        "provisional": True,
        "data_source": "real",
        "message": (estimate.get("message")
                    or config.PROVISIONAL_BASELINE_MESSAGE),
        "limitations": estimate.get("limitations", []),
        "age_source": age_source,
    })
    return merged


def build_reference_baseline(repo: BaseRepository, species: str, *,
                             models_dir: Path, variety: Optional[str] = None,
                             data_source: str = "real",
                             save: bool = True) -> dict:
    """Build (and by default persist) the successful-cohort reference baseline.

    Safe to repeat: the JSON is regenerated from stored measurements, and
    re-importing the same source produces the same medians.
    """
    baseline = ref.build_reference_from_repo(
        repo, species, variety=variety, data_source=data_source)
    path = None
    if save:
        path = ref.save_baseline(Path(models_dir), baseline)
    summary = ref.baseline_summary(baseline)
    summary["path"] = str(path) if path else None
    return summary


def load_reference_baseline(models_dir: Path, species: str,
                            variety: Optional[str] = None) -> Optional[dict]:
    """Load a persisted reference baseline summary (or ``None``)."""
    baseline = ref.load_baseline(Path(models_dir), species, variety)
    if baseline is None:
        return None
    return ref.baseline_summary(baseline)


def save_prediction_snapshot(repo: BaseRepository, result: dict, *,
                             plant_id: Optional[int] = None,
                             prediction_scope: Optional[str] = None,
                             observation_snapshot: Optional[dict] = None,
                             cohort_snapshot: Optional[dict] = None) -> dict:
    """Persist an immutable snapshot of a prediction result (append-only)."""
    species = (result.get("species") or "").strip()
    if not species:
        raise ValidationError(
            "prediction result has no species; cannot save a snapshot.")
    data_source = result.get("data_source")
    if data_source not in config.VALID_SOURCES:
        data_source = result.get("plant_data_source")
    if data_source not in config.VALID_SOURCES:
        data_source = None
    days = result.get("days_remaining")
    if days is None:
        days = result.get("days_remaining_raw")
    return repo.save_prediction_snapshot(
        species=species,
        variety=result.get("variety"),
        method=_method_for(result),
        available=bool(result.get("available")),
        plant_id=plant_id if plant_id is not None else result.get("plant_id"),
        prediction_scope=(prediction_scope
                          or result.get("prediction_scope") or "plant"),
        observation_date=result.get("observation_date"),
        level=_coerce_level(result.get("level")),
        provisional=bool(result.get("provisional")),
        days_remaining=days,
        predicted_readiness_date=result.get("predicted_readiness_date"),
        model_name=result.get("model_name"),
        data_source=data_source,
        baseline_version=result.get("baseline_version"),
        n_independent_plants=result.get("n_independent_plants"),
        message=result.get("message"),
        limitations=result.get("limitations"),
        observation_snapshot=(observation_snapshot
                              if observation_snapshot is not None
                              else result.get("observation")),
        cohort_snapshot=(cohort_snapshot if cohort_snapshot is not None
                         else result.get("cohort_snapshot")),
    )


def list_prediction_history(repo: BaseRepository, *,
                            plant_id: Optional[int] = None,
                            species: Optional[str] = None,
                            data_source: Optional[str] = None,
                            scope: Optional[str] = None,
                            limit: Optional[int] = None) -> list[dict]:
    return repo.list_prediction_history(
        plant_id=plant_id, species=species, data_source=data_source,
        scope=scope, limit=limit)


def record_readiness_assessment(
        repo: BaseRepository, *,
        assessment_date: str,
        readiness_status: str,
        plant_id: Optional[int] = None,
        species: Optional[str] = None,
        variety: Optional[str] = None,
        notes: Optional[str] = None,
        prediction_id: Optional[int] = None,
        prediction_snapshot: Optional[dict] = None,
        assessment_source: str = "user",
        data_source: Optional[str] = None) -> dict:
    """Record the user's readiness judgement.

    The assessment is kept distinct from the system prediction (the prediction
    shown at the time is preserved separately) and never creates a training
    label.
    """
    if prediction_id is not None and prediction_snapshot is None:
        pred = repo.get_prediction(prediction_id)
        if pred is None:
            raise NotFoundError(
                f"No prediction with prediction_id={prediction_id}.")
        prediction_snapshot = {
            "prediction_id": pred.get("prediction_id"),
            "method": pred.get("method"),
            "available": pred.get("available"),
            "provisional": pred.get("provisional"),
            "days_remaining": pred.get("days_remaining"),
            "predicted_readiness_date": pred.get("predicted_readiness_date"),
            "message": pred.get("message"),
            "created_at": pred.get("created_at"),
        }
    return repo.record_readiness_assessment(
        plant_id=plant_id, species=species, variety=variety,
        assessment_date=assessment_date, readiness_status=readiness_status,
        notes=notes, prediction_id=prediction_id,
        prediction_snapshot=prediction_snapshot,
        assessment_source=assessment_source, data_source=data_source)


def list_readiness_assessments(repo: BaseRepository, *,
                               plant_id: Optional[int] = None,
                               species: Optional[str] = None,
                               data_source: Optional[str] = None) -> list[dict]:
    return repo.list_readiness_assessments(
        plant_id=plant_id, species=species, data_source=data_source)


def _parse_csv_text(csv_text: str) -> tuple[list[str], list[dict], list[dict]]:
    """Validate a measurement CSV without touching the database.

    Returns ``(fieldnames, rows, issues)``. Row-level problems are reported
    per line, exactly like the CLI importer; nothing is invented.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValidationError("CSV has no header row.")
    # Normalise header names (strip whitespace) and row keys/values, mirroring
    # plantreadiness.csv_import so the shipped CSV (headers like " Age") works.
    reader.fieldnames = [str(n).strip() for n in reader.fieldnames]
    fieldnames = list(reader.fieldnames)

    def _col(*names):
        for n in names:
            if n in fieldnames:
                return n
        return None

    date_col = _col("Date")
    plant_col = _col("Plant_ID")
    height_col = _col("Height_cm")
    age_col = _col("Age_days", "Age")
    growth_col = _col("Growth_rate_cm_per_day")
    # Optional width / true-leaf columns (camera or hand-measured). Absence is OK.
    width_col = _col("stem_width_cm", "average_width_cm", "width_cm",
                     "median_width_cm", "Width_cm")
    leaves_col = _col("visible_true_leaf_count", "average_visible_leaves",
                      "true_leaves", "leaves", "median_visible_leaves",
                      "True_leaves")
    # Optional readiness/transplant label column. When present, a readiness
    # event is recorded per plant (ground truth for supervised training).
    readiness_col = _col("readiness_observed_date", "transplant_date",
                         "ready_date", "readiness_date", "readiness_date_observed")

    missing = []
    if date_col is None:
        missing.append("Date")
    if plant_col is None:
        missing.append("Plant_ID")
    if height_col is None:
        missing.append("Height_cm")
    if age_col is None:
        missing.append("Age_days")
    if missing:
        raise ValidationError(
            f"CSV is missing required columns: {missing}. "
            f"Found: {fieldnames}")

    def _parse_date_val(value):
        text = (value or "").strip()
        try:
            return parse_iso_date(text, "Date")
        except ValidationError:
            from datetime import datetime as _dt
            for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
                try:
                    return _dt.strptime(text, fmt).date()
                except ValueError:
                    continue
            raise

    def _to_int_age(text):
        text = (text or "").strip().lower()
        if not text:
            return None
        for suffix in (" days", " day", "days", "day"):
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                break
        if text.endswith("d"):
            text = text[:-1].strip()
        return int(float(text)) if text else None

    rows: list[dict] = []
    issues: list[dict] = []
    for line_no, raw in enumerate(reader, start=2):
        row = {str(k).strip(): (v.strip() if isinstance(v, str) else v)
               for k, v in raw.items()}
        # Skip completely blank lines (common trailing newline) silently.
        if all((v is None or str(v).strip() == "") for v in row.values()):
            continue
        ref = (row.get(plant_col) or "").strip()
        if not ref:
            issues.append({"line": line_no, "problem": "missing Plant_ID"})
            continue
        try:
            obs_date = _parse_date_val(row.get(date_col))
        except (ValidationError, ValueError) as exc:
            issues.append({"line": line_no, "problem": str(exc)})
            continue
        try:
            height_txt = (row.get(height_col) or "").strip()
            height = float(height_txt) if height_txt else None
        except (TypeError, ValueError) as exc:
            issues.append({"line": line_no,
                           "problem": f"bad numeric value: {exc}"})
            continue
        if height is None:
            issues.append({"line": line_no,
                           "problem": "missing Height_cm; row skipped "
                                      "(no invented data)"})
            continue
        if height < 0:
            issues.append({"line": line_no,
                           "problem": "Height_cm must be >= 0"})
            continue
        try:
            age = _to_int_age(row.get(age_col))
            growth_txt = (row.get(growth_col) or "").strip() if growth_col else ""
            growth = float(growth_txt) if growth_txt else None
            width_txt = (row.get(width_col) or "").strip() if width_col else ""
            width = float(width_txt) if width_txt else None
            leaves_txt = (row.get(leaves_col) or "").strip() if leaves_col else ""
            leaves = int(float(leaves_txt)) if leaves_txt else None
        except (TypeError, ValueError) as exc:
            issues.append({"line": line_no,
                           "problem": f"bad numeric value: {exc}"})
            continue
        if width is not None and width < 0:
            issues.append({"line": line_no,
                           "problem": "stem width must be >= 0"})
            continue
        if leaves is not None and leaves < 0:
            issues.append({"line": line_no,
                           "problem": "true leaf count must be >= 0"})
            continue
        # Optional readiness date (ground truth label).
        readiness_date = None
        if readiness_col:
            rtxt = (row.get(readiness_col) or "").strip()
            if rtxt:
                try:
                    readiness_date = _parse_date_val(rtxt).isoformat()
                except (ValidationError, ValueError) as exc:
                    issues.append({"line": line_no,
                                   "problem": f"bad readiness date: {exc}"})
                    continue
        rows.append({"line": line_no, "plant_ref": ref,
                     "date": obs_date.isoformat(), "age_days": age,
                     "height_cm": height, "growth_rate_cm_per_day": growth,
                     "stem_width_cm": width,
                     "visible_true_leaf_count": leaves,
                     "readiness_date": readiness_date})
    return fieldnames, rows, issues


def preview_csv_import(csv_text: str, *, existing_plants: Iterable[str] = ()
                       ) -> dict:
    """Validate + preview a CSV before anything is written (dry-run)."""
    fieldnames, rows, issues = _parse_csv_text(csv_text)
    existing = set(existing_plants)
    refs = sorted({r["plant_ref"] for r in rows})
    new_refs = [r for r in refs if r not in existing]
    known_rows = [r for r in rows if r["plant_ref"] in existing]
    new_rows = [r for r in rows if r["plant_ref"] not in existing]
    # All-or-nothing: a duplicate (Plant_ID, Date) inside the file is a hard
    # rejection for the upload path (the importer still skips such rows).
    seen_keys = set()
    for r in rows:
        key = (r["plant_ref"], r["date"])
        if key in seen_keys:
            issues.append({"line": r["line"],
                           "problem": "duplicate (Plant_ID, Date) within file"})
        seen_keys.add(key)
    return {
        "valid": not issues,
        "fieldnames": fieldnames,
        "rows_preview": rows[:50],
        "rows_total": len(rows),
        "issues": issues,
        "plants_in_file": refs,
        "plants_new": new_refs,
        "rows_existing_plants": len(known_rows),
        "rows_new_plants": len(new_rows),
        "importable_rows": len(rows),
    }


def run_csv_import(repo: BaseRepository, csv_text: str, *,
                   species: str, variety: Optional[str] = None,
                   date_started: Optional[str] = None,
                   dry_run: bool = True) -> dict:
    """Import measurement rows through the repository (append-only).

    With ``dry_run=True`` (the default) nothing is written - the returned
    report is a preview. Real imports mark every row ``data_source='real'``
    and skip duplicates rather than overwriting history.
    """
    species = (species or "").strip()
    if not species:
        raise ValidationError("species is required.")

    plants = repo.list_plants(species=species, data_source="real")
    existing_refs = {p.get("external_ref") for p in plants
                     if p.get("external_ref")}
    preview = preview_csv_import(csv_text, existing_plants=existing_refs)

    _, rows, _ = _parse_csv_text(csv_text)

    report = {
        "dry_run": dry_run,
        "species": species,
        "rows_read": preview["rows_total"],
        "plants_new": len(preview["plants_new"]),
        "plants_existing": len(existing_refs),
        "observations_inserted": 0,
        "observations_skipped_duplicate": 0,
        "readiness_events_recorded": 0,
        "readiness_events_skipped": 0,
        "issues": preview["issues"],
        "preview": preview,
        "rejected": False,
    }
    if dry_run:
        return report

    # All-or-nothing: refuse to write if any row is invalid.
    if preview["issues"]:
        report["rejected"] = True
        return report

    ref_to_id: dict[str, int] = {p["external_ref"]: p["plant_id"]
                                 for p in plants if p.get("external_ref")}
    for ref in preview["plants_new"]:
        ref_to_id[ref] = repo.register_plant(
            species=species, variety=variety, date_started=date_started,
            data_source="real", external_ref=ref,
            cohort="manual_upload")

    existing_dates = {(r["plant_id"], r["observation_date"])
                      for r in _existing_observations(repo, ref_to_id.values())}
    for row in rows:
        pid = ref_to_id.get(row["plant_ref"])
        if pid is None:
            continue
        if (pid, row["date"]) in existing_dates:
            report["observations_skipped_duplicate"] += 1
            continue
        try:
            repo.add_observation(
                plant_id=pid, observation_date=row["date"],
                age_days=row["age_days"], height_cm=row["height_cm"],
                growth_rate_cm_per_day=row["growth_rate_cm_per_day"],
                stem_width_cm=row.get("stem_width_cm"),
                visible_true_leaf_count=row.get("visible_true_leaf_count"),
                data_source="real",
                calibration_source=config.CALIBRATION_SOURCE_CSV)
            report["observations_inserted"] += 1
            existing_dates.add((pid, row["date"]))
        except DuplicateRecordError:
            report["observations_skipped_duplicate"] += 1
        except ValidationError as exc:
            # Should not happen after validation, but if it does, record it.
            report["issues"].append({"line": row["line"], "problem": str(exc)})

    # Record readiness events when the CSV provides a readiness_observed_date.
    # Idempotent: a duplicate (plant, definition) is skipped, not an error.
    readiness_by_ref: dict[str, str] = {}
    for row in rows:
        rd = row.get("readiness_date")
        if rd and row["plant_ref"] not in readiness_by_ref:
            readiness_by_ref[row["plant_ref"]] = rd
    readiness_recorded = 0
    readiness_skipped = 0
    for ref, rdate in readiness_by_ref.items():
        pid = ref_to_id.get(ref)
        if pid is None:
            continue
        try:
            repo.record_readiness_event(
                plant_id=pid,
                readiness_observed_date=rdate,
                readiness_definition_version="csv_import_v1",
                assessment_source="csv_import",
                data_source="real",
            )
            readiness_recorded += 1
        except Exception as exc:
            # Duplicate or validation error — skip silently (idempotent).
            readiness_skipped += 1
    report["readiness_events_recorded"] = readiness_recorded
    report["readiness_events_skipped"] = readiness_skipped
    return report


def _existing_observations(repo: BaseRepository, plant_ids) -> list[dict]:
    out = []
    for pid in plant_ids:
        for obs in repo.get_observations(pid):
            out.append({"plant_id": pid,
                        "observation_date": obs["observation_date"]})
    return out


def export_table_csv(repo: BaseRepository, table: str) -> str:
    """Export a table as CSV (UTF-8) for download/backup."""
    if table == "plants":
        rows = repo.list_plants()
    elif table == "observations":
        rows = []
        for plant in repo.list_plants():
            rows.extend(repo.get_observations(plant["plant_id"]))
    elif table == "feedback_14d":
        rows = repo.all_feedback()
    else:
        raise ValidationError(
            f"Unknown export table '{table}' "
            "(expected plants, observations or feedback_14d).")
    if not rows:
        return ""
    cols = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def dashboard_summary(repo: BaseRepository, models_dir: Path) -> dict:
    """Real-only dashboard metrics (never padded with synthetic numbers)."""
    stats = repo.dashboard_stats()
    by_species = repo.readiness_counts_by_species()
    return {
        "backend": repo.backend,
        "n_plants": stats["n_plants"],
        "n_observations": stats["n_observations"],
        "n_feedback_total": stats["n_feedback_total"],
        "n_feedback_completed": stats["n_feedback_completed"],
        "n_feedback_pending": stats["n_feedback_pending"],
        "n_feedback_overdue": stats["n_feedback_overdue"],
        "pending_feedback": stats["pending_feedback"],
        "readiness_by_species": by_species,
        "model_status": model_status(models_dir),
    }
