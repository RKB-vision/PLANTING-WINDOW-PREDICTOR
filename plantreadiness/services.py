"""Service layer - one implementation of every business workflow.

Both the command-line tools and the Streamlit web app call these functions,
so behaviour (validation, date arithmetic, fallback rules, honest-refusal
messages) can never drift between the two front-ends.

Every function takes a :class:`plantreadiness.repository.BaseRepository`
instead of a SQLite connection, which is what lets the same code run against
the local SQLite file during development and against Supabase (RLS-scoped)
in production.

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
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config
from . import model as mdl
from .db import (DuplicateRecordError, NotFoundError, ValidationError,
                 compute_growth, days_between, feedback_due_date,
                 parse_iso_date)
from .repository import BaseRepository

# Re-exported so front-ends import workflow errors from one place.
__all__ = [
    "ConfigurationError", "auto_age_days", "auto_growth_rate",
    "register_plant", "record_observation", "record_transplant",
    "submit_feedback", "correct_feedback", "feedback_state",
    "record_readiness_event", "list_readiness_events",
    "import_camera_measurements",
    "predict_for_plant", "predict_ad_hoc", "model_status",
    "species_model_plan", "preview_csv_import", "run_csv_import",
    "export_table_csv", "dashboard_summary",
]


class ConfigurationError(Exception):
    """Backend/secrets not configured - front-end must show setup help."""


# --------------------------------------------------------------------------- #
# Derived-value helpers (auto-calculation, never invented)
# --------------------------------------------------------------------------- #


def auto_age_days(date_started: Optional[str],
                  observation_date: str) -> Optional[int]:
    """Age in days derived from the plant's own start date.

    Returns ``None`` when no start date exists or the observation predates
    it - callers must then treat age as "not measurable" rather than
    guessing (a negative age is never invented).
    """
    if not date_started:
        return None
    age = days_between(date_started, observation_date)
    return age if age >= 0 else None


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
        age = auto_age_days(plant.get("date_started"), observation_date)
        if age is not None:
            age_days = age
            derived["age_days"] = "derived from date_started"

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
    )


# --------------------------------------------------------------------------- #
# Transplant + 14-day feedback workflows
# --------------------------------------------------------------------------- #


def record_transplant(repo: BaseRepository, *, plant_id: int,
                      transplant_date: str,
                      height_at_transplant_cm: Optional[float] = None,
                      data_source: Optional[str] = None) -> dict:
    """Create the transplant event and its pending 14-day feedback record."""
    return repo.record_transplant(
        plant_id=plant_id, transplant_date=transplant_date,
        height_at_transplant_cm=height_at_transplant_cm,
        data_source=data_source)


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
        # Supabase schema: height lives on the linked transplant event
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
                      allow_synthetic: bool = False) -> dict:
    """Species-aware prediction for a registered plant's latest observation."""
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
            "synthetic": False,
            "plant_id": plant_id,
            "species": species,
            "variety": variety,
            "plant_data_source": plant_source,
            "readiness_event_counts": repo.readiness_event_counts(
                species, variety, data_source="real"),
        }

    selection = mdl.select_model(species, variety, models_dir,
                                 allow_synthetic=allow_synthetic)
    # Real plants must not silently use synthetic models: select_model only
    # returns a synthetic artefact when allow_synthetic=True - an explicit
    # caller request - and the result below is labelled accordingly.
    mdl.load_selection(selection, models_dir)

    observation = {
        "age_days": obs["age_days"],
        "height_cm": obs["height_cm"],
        "growth_rate_cm_per_day": obs["growth_rate_cm_per_day"],
        "canopy_width_cm": obs["canopy_width_cm"],
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
        "readiness_event_counts": repo.readiness_event_counts(
            species, variety, data_source="real"),
        "synthetic_note": (
            "SYNTHETIC DEMO: provisional prediction from simulated data - "
            "not a botanical finding."
            if result.get("synthetic") else None),
    })
    if result.get("synthetic") and plant_source == "real":
        result["cross_source_warning"] = (
            "This is a REAL plant being shown an explicitly-enabled "
            "SYNTHETIC DEMO prediction. Do not treat it as evidence.")
    return result


def predict_ad_hoc(species: str, variety: Optional[str], observation: dict,
                   models_dir: Path, allow_synthetic: bool = False) -> dict:
    """Prediction from caller-supplied values (no stored plant required)."""
    selection = mdl.select_model(species, variety, models_dir,
                                 allow_synthetic=allow_synthetic)
    mdl.load_selection(selection, models_dir)
    observation = dict(observation)
    observation["species"] = species
    observation["variety"] = variety
    return mdl.predict_days_remaining(selection, observation)


# --------------------------------------------------------------------------- #
# Model / species status (dashboard + species page)
# --------------------------------------------------------------------------- #


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
                       variety: Optional[str], models_dir: Path,
                       allow_synthetic: bool = False) -> dict:
    """Which fallback level *would* be used for this species/variety."""
    selection = mdl.select_model(species, variety, models_dir,
                                 allow_synthetic=allow_synthetic)
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
# CSV import (preview -> validate -> import) and export
# --------------------------------------------------------------------------- #


def _parse_csv_text(csv_text: str) -> tuple[list[str], list[dict], list[dict]]:
    """Validate a measurement CSV without touching the database.

    Returns ``(fieldnames, rows, issues)``. Row-level problems are reported
    per line, exactly like the CLI importer; nothing is invented.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValidationError("CSV has no header row.")
    fieldnames = list(reader.fieldnames)
    missing = [c for c in ("Date", "Age_days", "Plant_ID", "Height_cm")
               if c not in fieldnames]
    if missing:
        raise ValidationError(
            f"CSV is missing required columns: {missing}. "
            f"Found: {fieldnames}")

    rows: list[dict] = []
    issues: list[dict] = []
    for line_no, row in enumerate(reader, start=2):
        ref = (row.get("Plant_ID") or "").strip()
        if not ref:
            issues.append({"line": line_no, "problem": "missing Plant_ID"})
            continue
        try:
            obs_date = parse_iso_date((row.get("Date") or "").strip(), "Date")
        except ValidationError as exc:
            issues.append({"line": line_no, "problem": str(exc)})
            continue
        try:
            height = float(row["Height_cm"]) if (
                row.get("Height_cm") or "").strip() else None
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
            age = int(float(row["Age_days"])) if (
                row.get("Age_days") or "").strip() else None
            growth = (float(row["Growth_rate_cm_per_day"])
                      if (row.get("Growth_rate_cm_per_day") or "").strip()
                      else None)
        except (TypeError, ValueError) as exc:
            issues.append({"line": line_no,
                           "problem": f"bad numeric value: {exc}"})
            continue
        rows.append({"line": line_no, "plant_ref": ref,
                     "date": obs_date.isoformat(), "age_days": age,
                     "height_cm": height, "growth_rate_cm_per_day": growth})
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
    plants = repo.list_plants(species=species, data_source="real")
    existing_refs = {p.get("external_ref") for p in plants
                     if p.get("external_ref")}
    preview = preview_csv_import(csv_text, existing_plants=existing_refs)

    report = {
        "dry_run": dry_run,
        "rows_read": preview["rows_total"],
        "plants_new": len(preview["plants_new"]),
        "plants_existing": len(existing_refs),
        "observations_inserted": 0,
        "observations_skipped_duplicate": 0,
        "issues": preview["issues"],
        "preview": preview,
    }
    if dry_run:
        return report

    ref_to_id: dict[str, int] = {p["external_ref"]: p["plant_id"]
                                 for p in plants if p.get("external_ref")}
    for ref in preview["plants_new"]:
        ref_to_id[ref] = repo.register_plant(
            species=species, variety=variety, date_started=date_started,
            data_source="real", external_ref=ref)

    _, rows, _ = _parse_csv_text(csv_text)
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
                data_source="real")
            report["observations_inserted"] += 1
            existing_dates.add((pid, row["date"]))
        except DuplicateRecordError:
            report["observations_skipped_duplicate"] += 1
        except ValidationError as exc:
            report["issues"].append({"line": row["line"], "problem": str(exc)})
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
