"""Successful-cohort reference baseline (descriptive, provisional).

The historical Super Orange CSV contains **successful** plants only. This
module turns those measurements into a *reference growth trajectory*:

* measurements of each plant are sorted by the dataset's valid time field
  (``observation_date``);
* growth rate between consecutive measurements is
  ``(height_t - height_{t-1}) / elapsed_days`` using **calendar** elapsed
  days; missing heights, duplicate dates and non-positive intervals are
  handled explicitly and a rate is never invented;
* the median height and median growth rate are taken across plants at
  comparable ages / observation-day positions, which reduces the influence
  of unusually tall, short or fast-growing individuals;
* the transplant reference is the recorded date when one exists, otherwise
  the **assumed** day after the plant's final monitoring day;
* every median records how many independent plants contributed to it.

What this module deliberately does NOT do:

* it does not claim the historical transplant date was optimal — the cohort
  is all-successful and cannot show what would have failed;
* it does not claim that a high growth rate means readiness;
* it does not train a model or create readiness labels.

A baseline is persisted as JSON under ``models/reference/`` so it is
reproducible and separable from the synthetic demonstration data.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import config
from . import db as dbm


# --------------------------------------------------------------------------- #
# Small statistics helpers
# --------------------------------------------------------------------------- #


def median(values: Iterable[Optional[float]]) -> Optional[float]:
    """Median of the non-missing values (``None`` when nothing is measured)."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return statistics.median(vals)


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _iqr(values: Iterable[Optional[float]]) -> Optional[float]:
    """Inter-quartile range of the non-missing values (needs >= 4 points)."""
    vals = sorted(float(v) for v in values if v is not None)
    if len(vals) < 4:
        return None
    q1, _, q3 = statistics.quantiles(vals, n=4, method="inclusive")
    return q3 - q1


# --------------------------------------------------------------------------- #
# Per-plant trajectory + growth rate
# --------------------------------------------------------------------------- #


def compute_plant_trajectory(observations: list[dict]) -> dict:
    """Chronological trajectory for one plant with explicit growth handling.

    ``observations`` are dicts with ``observation_date``, ``age_days``,
    ``height_cm`` and ``growth_rate_cm_per_day``. The returned ``points`` are
    sorted by date and each carries the growth rate actually used
    (measured if present, else derived from the previous real measurement)
    plus a ``growth_rate_derived`` flag and the ``elapsed_days`` since the
    previous observation.

    Duplicate observation dates, missing dates and unmeasurable heights are
    reported in ``issues`` and never silently replaced by invented values.
    """
    issues: list[dict] = []
    by_date: dict[str, dict] = {}
    for obs in observations:
        raw_date = obs.get("observation_date")
        if not raw_date:
            issues.append({"problem": "observation without a date skipped"})
            continue
        try:
            key = dbm.parse_iso_date(raw_date, "observation_date").isoformat()
        except dbm.ValidationError as exc:
            issues.append({"problem": str(exc)})
            continue
        if key in by_date:
            issues.append({"problem": f"duplicate observation date {key} "
                                      "(kept first)"})
            continue
        by_date[key] = obs

    ordered = [by_date[k] for k in sorted(by_date)]
    points: list[dict] = []
    prev: Optional[dict] = None
    for obs in ordered:
        height = obs.get("height_cm")
        rate = obs.get("growth_rate_cm_per_day")
        derived = False
        elapsed: Optional[int] = None
        reason: Optional[str] = None

        if prev is not None:
            try:
                span = dbm.days_between(prev["observation_date"],
                                        obs["observation_date"])
            except dbm.ValidationError:
                span = 0
            if span > 0:
                elapsed = span
                if rate is None:
                    prev_h = prev.get("height_cm")
                    if prev_h is not None and height is not None:
                        rate = round((float(height) - float(prev_h)) / span, 4)
                        derived = True
                    else:
                        reason = ("no previous/current height - rate cannot "
                                  "be derived")
            else:
                reason = "non-positive interval - rate cannot be derived"
        else:
            reason = "first measurement - no growth rate exists"

        points.append({
            "observation_date": obs["observation_date"],
            "age_days": obs.get("age_days"),
            "height_cm": height,
            "growth_rate_cm_per_day": _round(rate),
            "growth_rate_derived": bool(rate is not None and derived),
            "elapsed_days": elapsed,
            "growth_rate_reason": reason,
        })
        prev = obs

    return {"points": points, "issues": issues}


def transplant_reference(points: list[dict],
                         recorded_transplant_date: Optional[str] = None
                         ) -> Optional[dict]:
    """Transplant reference date for one plant.

    A recorded (verified) transplant date always wins. Otherwise the
    reference is *assumed* to be the day after the final monitoring day and
    is flagged as an assumption.
    """
    if recorded_transplant_date:
        try:
            recorded = dbm.parse_iso_date(
                recorded_transplant_date, "transplant_date").isoformat()
        except dbm.ValidationError:
            recorded = None
        if recorded:
            return {"transplant_date": recorded, "source": "recorded",
                    "is_assumption": False,
                    "rule": "verified recorded transplant date"}

    if not points:
        return None
    last_date = max(p["observation_date"] for p in points)
    assumed = (dbm.parse_iso_date(last_date, "observation_date")
               + timedelta(days=1)).isoformat()
    return {"transplant_date": assumed,
            "source": "assumed_day_after_last_observation",
            "is_assumption": True,
            "rule": config.TRANSPLANT_ASSUMPTION_RULE}


# --------------------------------------------------------------------------- #
# Baseline construction
# --------------------------------------------------------------------------- #


def _groups(points_with_targets: list[dict], key: str) -> list[dict]:
    """Aggregate points into median groups keyed by ``key``.

    Each group reports the number of points, the number of *independent*
    plants, median height, median growth rate and median days-until-transplant
    plus spread information.
    """
    buckets: dict[object, list[dict]] = {}
    for point in points_with_targets:
        value = point.get(key)
        if value is None:
            continue
        buckets.setdefault(value, []).append(point)

    groups: list[dict] = []
    for value in sorted(buckets):
        pts = buckets[value]
        days = [p.get("days_until_transplant") for p in pts]
        if not any(d is not None for d in days):
            continue
        groups.append({
            key: value,
            "n_points": len(pts),
            "n_plants": len({p["plant_id"] for p in pts}),
            "median_height_cm": _round(median(p.get("height_cm") for p in pts)),
            "median_growth_rate_cm_per_day": _round(
                median(p.get("growth_rate_cm_per_day") for p in pts)),
            "median_days_until_transplant": _round(median(days)),
            "iqr_days_until_transplant": _round(_iqr(days)),
            "min_days_until_transplant": _round(
                min((float(d) for d in days if d is not None), default=None)),
            "max_days_until_transplant": _round(
                max((float(d) for d in days if d is not None), default=None)),
        })
    return groups


def build_reference_baseline(plants: list[dict],
                             species: str,
                             variety: Optional[str] = None,
                             data_source: str = "real",
                             source_label: Optional[str] = None) -> dict:
    """Build a reproducible successful-cohort baseline.

    ``plants`` is a list of dicts::

        {"plant_id": int,
         "external_ref": str | None,
         "recorded_transplant_date": str | None,
         "observations": [{observation_date, age_days, height_cm,
                           growth_rate_cm_per_day}, ...]}

    Returns a JSON-serialisable baseline. Medians are computed across
    *independent plants*; the count of contributing plants is recorded on
    every group.
    """
    per_plant: list[dict] = []
    all_points: list[dict] = []
    issues_by_plant: dict[str, list] = {}
    n_recorded = 0
    n_assumed = 0

    for plant in plants:
        pid = plant.get("plant_id")
        ref = plant.get("external_ref") or str(pid)
        trajectory = compute_plant_trajectory(plant.get("observations", []))
        points = trajectory["points"]
        if trajectory["issues"]:
            issues_by_plant[str(ref)] = trajectory["issues"]

        transplant = transplant_reference(
            points, plant.get("recorded_transplant_date"))
        if transplant is None:
            continue
        if transplant["is_assumption"]:
            n_assumed += 1
        else:
            n_recorded += 1
        transplant_date = transplant["transplant_date"]

        enriched: list[dict] = []
        for point in points:
            try:
                days_until = dbm.days_between(point["observation_date"],
                                              transplant_date)
            except dbm.ValidationError:
                days_until = None
            row = {**point, "plant_id": pid, "external_ref": ref,
                   "days_until_transplant": days_until,
                   "transplant_date": transplant_date,
                   "transplant_is_assumption": transplant["is_assumption"]}
            enriched.append(row)
            all_points.append(row)

        rates = [p["growth_rate_cm_per_day"] for p in points
                 if p["growth_rate_cm_per_day"] is not None]
        per_plant.append({
            "plant_id": pid,
            "external_ref": ref,
            "n_observations": len(points),
            "first_date": points[0]["observation_date"] if points else None,
            "last_date": points[-1]["observation_date"] if points else None,
            "transplant_date": transplant_date,
            "transplant_source": transplant["source"],
            "transplant_is_assumption": transplant["is_assumption"],
            "n_growth_rates_derived": sum(
                1 for p in points if p["growth_rate_derived"]),
            "median_growth_rate_cm_per_day": _round(median(rates)),
        })

    n_plants = len({p["plant_id"] for p in per_plant})
    by_age = _groups(all_points, "age_days")
    by_days_before = _groups(all_points, "days_until_transplant")
    contributing = max(
        [g["n_plants"] for g in by_age + by_days_before] or [0])

    return {
        "baseline_version": 1,
        "species": species,
        "variety": variety,
        "data_source": data_source,
        "source_label": source_label or f"{species} successful cohort",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_plants": n_plants,
        "n_points": len(all_points),
        "n_plants_contributing_to_medians": contributing,
        "transplant_reference": {
            "assumption": n_assumed > 0,
            "rule": config.TRANSPLANT_ASSUMPTION_RULE,
            "n_recorded": n_recorded,
            "n_assumed": n_assumed,
        },
        "by_age_days": by_age,
        "by_days_before_transplant": by_days_before,
        "per_plant": per_plant,
        "issues_by_plant": issues_by_plant,
        "limitations": [
            "The cohort consists of successfully transplanted plants; it "
            "cannot establish the conditions under which transplantation "
            "would fail.",
            "The historical transplant date is not assumed to be the optimal "
            "or earliest-safe date.",
            "Growth rate is a descriptive signal, not a readiness label.",
            "Medians summarise observed timing; they are not a validated "
            "readiness model.",
        ],
    }


def build_reference_from_repo(repo, species: str,
                              variety: Optional[str] = None,
                              data_source: str = "real") -> dict:
    """Gather stored observations/transplants and build the baseline."""
    plants = repo.list_plants(species=species, data_source=data_source)
    if variety:
        plants = [p for p in plants if p.get("variety") == variety]

    # The baseline summarises the *successful historical cohort*. When plants
    # are explicitly tagged (cohort='reference') prefer those, so a live plant
    # registered later never contaminates the reference medians. Falls back to
    # every plant when nothing is tagged (e.g. a custom import).
    tagged = [p for p in plants
              if str(p.get("cohort") or "").strip().lower() == "reference"]
    if tagged:
        plants = tagged

    transplant_dates = {t["plant_id"]: t["transplant_date"]
                        for t in repo.all_feedback()}
    from . import calibration as _cal

    payload = []
    for plant in plants:
        usable = []
        for obs in repo.get_observations(plant["plant_id"]):
            group = _cal.calibration_group(
                calibration_source=obs.get("calibration_source"),
                rod_length_cm=obs.get("rod_length_cm"),
                calibration_verified=obs.get("calibration_verified"))
            if _cal.is_baseline_usable(group):
                usable.append(obs)
        payload.append({
            "plant_id": plant["plant_id"],
            "external_ref": plant.get("external_ref"),
            "recorded_transplant_date": transplant_dates.get(plant["plant_id"]),
            "observations": usable,
        })
    return build_reference_baseline(
        payload, species, variety=variety, data_source=data_source)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _safe(name: Optional[str]) -> str:
    return (name or "ANY").replace(" ", "").replace("/", "-")


def baseline_path(models_dir: Path, species: str,
                  variety: Optional[str] = None) -> Path:
    return Path(models_dir) / "reference" / (
        f"baseline_{_safe(species)}_{_safe(variety)}.json")


def save_baseline(models_dir: Path, baseline: dict) -> Path:
    path = baseline_path(models_dir, baseline["species"],
                         baseline.get("variety"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2, default=str))
    return path


def load_baseline(models_dir: Path, species: str,
                  variety: Optional[str] = None) -> Optional[dict]:
    path = baseline_path(models_dir, species, variety)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Provisional estimate
# --------------------------------------------------------------------------- #


def _nearest_group(groups: list[dict], key: str, value: float,
                   tolerance: float) -> Optional[dict]:
    """Nearest reference group within ``tolerance`` (ties -> smaller value)."""
    best = None
    best_gap = None
    for group in groups:
        gap = abs(float(group[key]) - float(value))
        if gap > tolerance:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = group, gap
    return best


def provisional_estimate(baseline: dict, observation: dict) -> dict:
    """Estimate days-until-transplant from the successful-cohort baseline.

    Age, height and growth rate are all used as inputs together:

    * ``age_days`` selects the reference group (a reliable planting date does
      NOT disable the other inputs);
    * ``height_cm`` selects the group when no age is available, and stays a
      recorded input even when age is available;
    * ``growth_rate_cm_per_day`` adjusts the estimate via
      ``D_adjusted = D_base * clamp(G_cohort / G_plant, 0.5, 2.0)``.

    The growth-rate adjustment is a provisional heuristic (a hypothesis to
    test against outcomes), never an established fact. Returns
    ``available=False`` when no reference group is close enough or too few
    independent plants contribute. The estimate is always labelled provisional.
    """
    import math

    if not baseline:
        return {"available": False,
                "method": config.METHOD_REFERENCE_BASELINE,
                "provisional": True,
                "message": "No reference baseline is available.",
                "limitations": []}

    age = observation.get("age_days")
    height = observation.get("height_cm")
    obs_date = observation.get("observation_date")
    plant_rate = observation.get("growth_rate_cm_per_day")

    by_age = baseline.get("by_age_days", [])

    group = None
    basis = None
    if age is not None:
        group = _nearest_group(by_age, "age_days", age,
                               config.PROVISIONAL_AGE_TOLERANCE_DAYS)
        if group is not None:
            basis = "age_days=%s" % age
            if (height is not None
                    and group.get("median_height_cm") is not None
                    and abs(float(group["median_height_cm"]) - float(height))
                    > config.PROVISIONAL_HEIGHT_TOLERANCE_CM):
                alt = _nearest_group(by_age, "median_height_cm", height,
                                     config.PROVISIONAL_HEIGHT_TOLERANCE_CM)
                if alt is not None and alt is not group:
                    group = alt
                    basis = "height_cm=%s" % height
    if group is None and height is not None:
        group = _nearest_group(by_age, "median_height_cm", height,
                               config.PROVISIONAL_HEIGHT_TOLERANCE_CM)
        basis = ("height_cm=%s" % height) if group else None

    if group is None or group.get("median_days_until_transplant") is None:
        return {
            "available": False,
            "method": config.METHOD_REFERENCE_BASELINE,
            "provisional": True,
            "message": ("No comparable reference group was found for this "
                        "observation; the baseline cannot estimate readiness."),
            "limitations": baseline.get("limitations", []),
        }
    if group.get("n_plants", 0) < config.MIN_PROVISIONAL_PLANTS:
        return {
            "available": False,
            "method": config.METHOD_REFERENCE_BASELINE,
            "provisional": True,
            "message": ("Only %s independent plants contribute to this "
                        "reference group; too few for a provisional estimate."
                        % group.get("n_plants")),
            "limitations": baseline.get("limitations", []),
        }

    base_days = float(group["median_days_until_transplant"])
    uncertainty = group.get("iqr_days_until_transplant")
    cohort_rate = group.get("median_growth_rate_cm_per_day")

    # Growth-rate adjustment (provisional heuristic, hypothesis to test).
    adjustment_applied = False
    adjustment_reason = None
    factor = 1.0
    try:
        cr = None if cohort_rate is None else float(cohort_rate)
        pr = None if plant_rate is None else float(plant_rate)
    except (TypeError, ValueError):
        cr, pr = None, None

    if cr is None or pr is None:
        adjustment_reason = ("skipped: " +
                             ("cohort growth rate unavailable" if cr is None
                              else "plant growth rate unavailable"))
    elif cr <= 0 or pr <= 0 or not math.isfinite(cr) or not math.isfinite(pr):
        adjustment_reason = "skipped: non-positive or non-finite growth rate"
    else:
        raw_factor = cr / pr
        lo = config.GROWTH_RATE_ADJUSTMENT_MIN_FACTOR
        hi = config.GROWTH_RATE_ADJUSTMENT_MAX_FACTOR
        factor = min(hi, max(lo, raw_factor))
        adjustment_applied = True
        adjustment_reason = "applied"

    adjusted_days = base_days * factor if adjustment_applied else base_days

    predicted_date = None
    if obs_date:
        try:
            predicted_date = (
                dbm.parse_iso_date(obs_date, "observation_date")
                + timedelta(days=int(round(adjusted_days)))
            ).isoformat()
        except dbm.ValidationError:
            predicted_date = None

    insufficient_history = plant_rate is None
    history_note = None
    if insufficient_history:
        history_note = (
            "No recent growth-rate measurement is available for this "
            "observation; the estimate relies on age/historical timing only, "
            "not on this plant's own growth trend.")

    return {
        "available": True,
        "method": config.METHOD_REFERENCE_BASELINE,
        "provisional": True,
        "synthetic": False,
        "days_remaining": round(adjusted_days, 1),
        "days_remaining_raw": round(base_days, 1),
        "days_remaining_uncertainty_days": _round(uncertainty, 1),
        "predicted_readiness_date": predicted_date,
        "reference_basis": basis,
        "reference_group": group,
        "n_independent_plants": group.get("n_plants"),
        "insufficient_growth_history": insufficient_history,
        "history_note": history_note,
        "reference_transplant_is_assumption":
            baseline.get("transplant_reference", {}).get("assumption", True),
        "message": config.PROVISIONAL_BASELINE_MESSAGE,
        "limitations": baseline.get("limitations", []),
        "baseline_version": baseline.get("baseline_version"),
        "hours": None,
        "cohort_growth_rate_cm_per_day": _round(cohort_rate, 4),
        "plant_growth_rate_cm_per_day": _round(plant_rate, 4),
        "growth_rate_adjustment_factor":
            _round(factor, 4) if adjustment_applied else None,
        "growth_rate_adjustment_applied": adjustment_applied,
        "growth_rate_adjustment_reason": adjustment_reason,
    }

def baseline_summary(baseline: dict) -> dict:
    """Trimmed view for the API/UI (drops the per-point dump)."""
    if not baseline:
        return {}
    return {
        "species": baseline.get("species"),
        "variety": baseline.get("variety"),
        "data_source": baseline.get("data_source"),
        "source_label": baseline.get("source_label"),
        "generated_at": baseline.get("generated_at"),
        "n_plants": baseline.get("n_plants"),
        "n_points": baseline.get("n_points"),
        "n_plants_contributing_to_medians":
            baseline.get("n_plants_contributing_to_medians"),
        "transplant_reference": baseline.get("transplant_reference"),
        "by_age_days": baseline.get("by_age_days", []),
        "by_days_before_transplant":
            baseline.get("by_days_before_transplant", []),
        "per_plant": baseline.get("per_plant", []),
        "limitations": baseline.get("limitations", []),
    }
