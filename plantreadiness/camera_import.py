"""Import daily measurements produced by the ESP32-CAM / OpenCV pipeline.

The hardware pipeline (plant_measurement.py) records daily summaries to
data/generated/plant_daily_measurements.csv with the following schema:

    day,plant_id,captures_used,average_height_cm,average_width_cm,
    average_visible_leaves,average_cm_per_pixel,average_confidence,
    first_capture_timestamp,last_capture_timestamp

Column semantics verified from plant_measurement.py (lines 1205-1213, 1630-1730):
* plant_id: External plant identifier (e.g. 'Plant-01'), mapped to
  plants.external_ref.
* average_height_cm: Plant height from stem base to apex in cm ->
  observations.height_cm.
* average_width_cm: STEM WIDTH (caliper in cm), computed as
  plant.stem_width_pixels * cm_per_pixel. NOTE: This is NOT canopy width.
  It is mapped to ``observations.stem_width_cm`` to preserve the real
  measurement without misrepresenting it as canopy width.
* average_visible_leaves: Average detected leaf count ->
  observations.visible_true_leaf_count (rounded to integer).
* first_capture_timestamp / last_capture_timestamp: ISO timestamps
  (e.g. '2026-10-08T11:26:08'), from which observation_date (YYYY-MM-DD)
  is extracted.
* captures_used, average_cm_per_pixel, average_confidence: Hardware/vision
  diagnostics preserved in report/audit.

The import is append-only and idempotent: duplicate observations for the same
plant and date are skipped, never overwritten.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
from pathlib import Path
from typing import Any, Optional, Union

from . import calibration
from . import config
from . import db as dbm
from .repository import BaseRepository, DuplicateRecordError, NotFoundError, ValidationError

CAMERA_REQUIRED_COLUMNS = (
    "plant_id",
    "average_height_cm",
    "first_capture_timestamp",
)

# Columns the four-capture aggregation adds. Each is optional so a CSV produced
# by the older pipeline (or a hand-written row) still imports.
CAMERA_OPTIONAL_COLUMNS = (
    "capture_date",
    "rod_length_cm",
    "expected_captures",
    "processed_captures",
    "valid_captures",
    "failed_captures",
    "incomplete",
    "low_confidence",
    "median_height_cm",
    "median_width_cm",
    "median_visible_leaves",
    "capture_ids",
    "captures_json",
    "problems",
)

VALID_CAPTURE = "valid"
FAILED_CAPTURE = "failed"


def _to_float(value: Any) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _to_leaf_count(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    f = float(value)
    return max(0, int(round(f)))


def _first_present(row: dict[str, Any], *names: str) -> Any:
    """First column that is present and non-empty (median* preferred over mean)."""
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _to_int(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    return int(float(value))


def _safe_float(value: Any) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_stem_width(value: Any) -> Optional[float]:
    """Treat zero/non-finite OpenCV widths as not measured, never as 0 cm."""
    width = _safe_float(value)
    if width is None or not math.isfinite(width) or width <= 0:
        return None
    return width


def _safe_int(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None


def _median(values) -> Optional[float]:
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return None
    return float(statistics.median(numbers))


def _parse_captures_json(raw: Any) -> list[dict[str, Any]]:
    """Decode the per-capture audit column (empty/invalid -> no captures)."""
    if raw is None or str(raw).strip() == "":
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _previous_observation(repo: BaseRepository, plant_id: int,
                          obs_date: str) -> Optional[dict]:
    """Latest stored daily observation strictly *before* ``obs_date``.

    Using the previous *daily* record (not the previous image) is what keeps
    four images of one day from being treated as four days of growth. Rows
    flagged as suspect are skipped so one bad camera day does not poison the
    next growth rate.
    """
    rows = sorted(
        (row for row in repo.get_observations(plant_id)
         if row.get("observation_date")
         and str(row["observation_date"]) < obs_date),
        key=lambda row: str(row["observation_date"]))
    accepted: list[dict] = []
    for row in rows:
        height = row.get("height_cm")
        is_suspect = bool(row.get("quality_flag"))
        if (not is_suspect
                and row.get("calibration_source")
                == config.CALIBRATION_SOURCE_CAMERA
                and height is not None):
            is_suspect = dbm.is_height_outlier(
                height, [a.get("height_cm") for a in accepted])
        if is_suspect:
            continue
        accepted.append(row)
    return accepted[-1] if accepted else None


def _trailing_heights(repo: BaseRepository, plant_id: int,
                      obs_date: str, limit: int = 3) -> list[float]:
    """Heights of the most recent prior observations (for the outlier check)."""
    rows = sorted(
        (row for row in repo.get_observations(plant_id)
         if row.get("observation_date")
         and str(row["observation_date"]) < obs_date
         and row.get("height_cm") is not None),
        key=lambda row: str(row["observation_date"]))
    return [float(row["height_cm"]) for row in rows[-limit:]]


def import_camera_measurements(
    repo: BaseRepository,
    source: Union[str, Path, io.StringIO, io.TextIOBase],
    *,
    default_species: Optional[str] = None,
    default_variety: Optional[str] = None,
    auto_register: bool = True,
    data_source: str = "real",
    stage_only: bool = False,
    initial_age_days: Optional[int] = None,
    initial_age_measured_on: Optional[str] = None,
) -> dict:
    """Import measurements from an ESP32-CAM output CSV into the repository.

    Parameters
    ----------
    repo : BaseRepository
        The local database repository (:class:`SQLiteRepository`).
    source : str, Path, or file-like object
        The CSV file path or file-like buffer containing hardware measurements.
    default_species : str
        Species assigned if auto_register=True creates a new plant.
    default_variety : str, optional
        Variety assigned if auto_register=True creates a new plant.
    auto_register : bool
        If True, plants referenced in the CSV that do not exist in the
        repository are registered under external_ref. If False, records for
        unregistered plants are counted as unmatched and skipped.
    data_source : str
        Data source tag ('real' or 'synthetic', defaults to 'real').

    Returns
    -------
    dict
        Import report with counts of rows read, inserted, skipped duplicates,
        unmatched plants, and issues found.
    """
    report: dict[str, Any] = {
        "rows_read": 0,
        "observations_inserted": 0,
        "staged_captures": 0,
        "observations_updated": 0,
        "observations_skipped_duplicate": 0,
        "captures_recorded": 0,
        "captures_unchanged": 0,
        "failed_captures": 0,
        "daily_records_recomputed": 0,
        "incomplete_days": 0,
        "low_confidence_records": 0,
        "plants_registered": 0,
        "unmatched_plants": [],
        "issues": [],
    }

    if isinstance(source, (str, Path)):
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"Camera measurement CSV not found: {p}")
        fh = p.open("r", newline="", encoding="utf-8")
        should_close = True
    else:
        fh = source
        should_close = False

    try:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row.")

        missing_cols = [
            c for c in CAMERA_REQUIRED_COLUMNS if c not in reader.fieldnames
        ]
        if missing_cols:
            raise ValueError(
                f"Camera CSV is missing required columns: {missing_cols}. "
                f"Found: {list(reader.fieldnames)}"
            )

        # Cache existing plants by external_ref (normalised so that harmless
        # spelling variations such as Plant01 / Plant-01 / Plant_01 resolve to
        # the same plant). A normalised key that maps to more than one plant is
        # recorded as ambiguous and never silently collapsed.
        existing_plants = repo.list_plants()
        ref_to_plants: dict[str, list[dict]] = {}
        id_to_plant: dict[int, dict] = {}
        for p in existing_plants:
            # Historical reference plants are never camera targets: a live plant
            # must be registered separately even if it shares an external_ref.
            _cohort = str(p.get("cohort") or "").strip().lower()
            if _cohort in ("reference", "manual_upload"):
                continue
            if p.get("external_ref"):
                key = config.normalize_plant_ref(p["external_ref"])
                ref_to_plants.setdefault(key, []).append(p)
            ref_to_plants.setdefault(str(p["plant_id"]), []).append(p)
            id_to_plant[int(p["plant_id"])] = p

        for line_no, row in enumerate(reader, start=2):
            report["rows_read"] += 1
            raw_id = (row.get("plant_id") or "").strip()
            if not raw_id:
                report["issues"].append(
                    {"line": line_no, "problem": "Missing plant_id."}
                )
                continue

            # Which day this row summarises. Prefer an explicit capture_date;
            # otherwise fall back to the capture timestamp, as before.
            raw_capture_date = (row.get("capture_date") or "").strip()
            if raw_capture_date:
                try:
                    obs_date = dbm.parse_iso_date(raw_capture_date, "capture_date")
                except Exception as exc:
                    report["issues"].append(
                        {"line": line_no,
                         "problem": f"Invalid capture_date '{raw_capture_date}': {exc}"}
                    )
                    continue
            else:
                ts = (
                    row.get("first_capture_timestamp")
                    or row.get("last_capture_timestamp")
                    or ""
                ).strip()
                if not ts:
                    report["issues"].append(
                        {"line": line_no, "problem": "Missing capture timestamp."}
                    )
                    continue
                # ISO '2026-10-08T11:26:08' or a plain '2026-10-08'
                date_str = ts.split("T")[0].split(" ")[0].strip()
                try:
                    obs_date = dbm.parse_iso_date(date_str, "capture timestamp")
                except Exception as exc:
                    report["issues"].append(
                        {"line": line_no,
                         "problem": f"Invalid timestamp '{ts}': {exc}"}
                    )
                    continue

            obs_date_iso = obs_date.isoformat()

            # Prefer the median aggregate when the row carries one; fall back to
            # the legacy mean columns so older CSVs keep importing.
            height_label = (
                "median_height_cm"
                if str(row.get("median_height_cm") or "").strip()
                else "average_height_cm"
            )
            try:
                height = _to_float(
                    _first_present(row, "median_height_cm", "average_height_cm"))
            except (TypeError, ValueError) as exc:
                report["issues"].append(
                    {"line": line_no, "problem": f"Bad {height_label}: {exc}"}
                )
                continue

            if height is None:
                report["issues"].append(
                    {
                        "line": line_no,
                        "problem": f"Missing {height_label}; row skipped "
                                   "(no fabricated data).",
                    }
                )
                continue

            if height < 0:
                report["issues"].append(
                    {
                        "line": line_no,
                        "problem": f"Negative height {height} cm is invalid.",
                    }
                )
                continue

            # Optional measurements. Only used when actually present.
            try:
                leaves = _to_leaf_count(
                    _first_present(row, "median_visible_leaves",
                                   "average_visible_leaves"))
            except (TypeError, ValueError):
                leaves = None

            # STEM width, deliberately stored as stem_width_cm - never canopy.
            try:
                stem_width = _to_float(
                    _first_present(row, "median_width_cm", "average_width_cm"))
            except (TypeError, ValueError) as exc:
                report["issues"].append(
                    {"line": line_no, "problem": f"Bad stem width: {exc}"}
                )
                continue
            if stem_width is not None and (
                not math.isfinite(stem_width) or stem_width <= 0
            ):
                stem_width = None

            # Effective calibration for this row. Recorded, never guessed: a
            # missing value falls back to the configured rod length because the
            # pipeline that produced the CSV used exactly that.
            try:
                row_rod_length = _to_float(row.get("rod_length_cm"))
            except (TypeError, ValueError):
                row_rod_length = None
            if row_rod_length is None:
                row_rod_length = float(config.CAMERA_ROD_LENGTH_CM)

            captures = _parse_captures_json(row.get("captures_json"))
            if _to_int(row.get("incomplete")):
                report["incomplete_days"] += 1
            if _to_int(row.get("low_confidence")):
                report["low_confidence_records"] += 1

            # Resolve plant in database (normalised key; ambiguous mappings are
            # quarantined, never silently collapsed).
            norm_id = config.normalize_plant_ref(raw_id)
            candidates = ref_to_plants.get(norm_id, [])
            if not candidates and raw_id.isdigit():
                candidates = [id_to_plant.get(int(raw_id))] if id_to_plant.get(int(raw_id)) else []

            if len(candidates) > 1:
                if raw_id not in report["unmatched_plants"]:
                    report["unmatched_plants"].append(raw_id)
                report["issues"].append(
                    {
                        "line": line_no,
                        "problem": (
                            f"Ambiguous plant reference '{raw_id}': "
                            f"multiple registered plants normalize to "
                            f"'{norm_id}' (ids "
                            f"{[p.get('plant_id') for p in candidates]}). "
                            "Quarantined; resolve the mapping manually."
                        ),
                    }
                )
                continue

            target_plant = candidates[0] if candidates else None

            if target_plant is None:
                if auto_register:
                    new_id = repo.register_plant(
                        species=default_species or config.DEFAULT_SPECIES,
                        variety=default_variety,
                        external_ref=raw_id,
                        data_source=data_source,
                        initial_age_days=initial_age_days,
                        initial_age_measured_on=(
                            (initial_age_measured_on or obs_date_iso)
                            if initial_age_days is not None else None),
                    )
                    target_plant = repo.get_plant(new_id)
                    if target_plant:
                        ref_to_plants.setdefault(norm_id, []).append(target_plant)
                        id_to_plant[new_id] = target_plant
                        report["plants_registered"] += 1
                else:
                    if raw_id not in report["unmatched_plants"]:
                        report["unmatched_plants"].append(raw_id)
                    report["issues"].append(
                        {
                            "line": line_no,
                            "problem": f"Unmatched plant '{raw_id}' (auto_register is False).",
                        }
                    )
                    continue

            if not target_plant:
                continue

            plant_id = int(target_plant["plant_id"])

            # ------------------------------------------------------------------
            # 1. Persist each individual capture (idempotent by public_id).
            #    Re-processing the same image never counts it twice.
            # ------------------------------------------------------------------
            state = repo.current_calibration_setting()
            rod_verified = calibration.verification_applies(state, row_rod_length)
            captures_recorded = 0
            failed_captures = 0

            for capture in captures:
                public_id = capture.get("public_id")
                if not public_id:
                    continue
                capture_rod = _safe_float(capture.get("rod_length_cm"))
                if capture_rod is None:
                    capture_rod = row_rod_length
                validity = str(capture.get("validity") or VALID_CAPTURE)
                if validity not in dbm.CAPTURE_VALIDITIES:
                    validity = VALID_CAPTURE
                try:
                    outcome = repo.add_capture(
                        plant_id=plant_id,
                        public_id=str(public_id),
                        observation_date=obs_date_iso,
                        validity=validity,
                        rod_length_cm=capture_rod,
                        calibration_source=config.CALIBRATION_SOURCE_CAMERA,
                        calibration_verified=rod_verified,
                        capture_timestamp=capture.get("capture_timestamp"),
                        height_cm=_safe_float(capture.get("height_cm")),
                        stem_width_cm=_positive_stem_width(
                            capture.get("width_cm")),
                        visible_true_leaf_count=_safe_int(capture.get("leaf_count")),
                        cm_per_pixel=_safe_float(capture.get("cm_per_pixel")),
                        confidence=_safe_float(capture.get("confidence")),
                        problem=capture.get("problem"),
                        data_source=data_source,
                        review_status="pending" if stage_only else "accepted",
                    )
                except (ValidationError, DuplicateRecordError) as exc:
                    report["issues"].append(
                        {"line": line_no,
                         "problem": f"Capture {public_id}: {exc}"}
                    )
                    continue
                if outcome.get("created"):
                    report["captures_recorded"] += 1
                    captures_recorded += 1
                    if stage_only:
                        report["staged_captures"] += 1
                else:
                    report["captures_unchanged"] += 1
                if validity == FAILED_CAPTURE:
                    failed_captures += 1

            report["failed_captures"] += failed_captures

            # ------------------------------------------------------------------
            # 2. The daily value. When individual captures are stored for this
            #    date, the median is recomputed from ALL of them (previous
            #    captures included) so a later capture the same day improves the
            #    day's record instead of being discarded. Otherwise the row's
            #    own aggregate is used.
            # ------------------------------------------------------------------
            if stage_only:
                # Staged only: captures recorded above, no daily observation.
                report["observations_skipped_duplicate"] += 0
                continue
            stored_valid = [
                capture for capture in repo.captures_on_date(plant_id, obs_date_iso)
                if capture.get("validity") == VALID_CAPTURE
                and capture.get("review_status") == "accepted"
            ]

            if stored_valid:
                height = _median(c.get("height_cm") for c in stored_valid)
                stem_width = _median(
                    _positive_stem_width(c.get("stem_width_cm"))
                    for c in stored_valid)
                leaves = _median(c.get("visible_true_leaf_count")
                                 for c in stored_valid)
                if height is None:
                    report["issues"].append(
                        {"line": line_no,
                         "problem": "No stored capture on this date has a usable "
                                    "height; the day is recorded without one."}
                    )
                    continue
                valid_captures = len(stored_valid)
                report["daily_records_recomputed"] += 1
            else:
                valid_captures = _to_int(row.get("valid_captures"))

            # ------------------------------------------------------------------
            # 3. Age (only from a real planting date) and growth rate (only from
            #    a previous DAILY record, using real elapsed days).
            # ------------------------------------------------------------------
            age_days = dbm.age_days_for_plant(target_plant, obs_date_iso)

            # A >50% jump from the previous accepted height is stored but flagged:
            # visible for review, excluded from growth rates and medians.
            suspect = dbm.is_height_outlier(
                height, _trailing_heights(repo, plant_id, obs_date_iso))
            quality_flag = "height_jump_suspect" if suspect else None
            quality_note = (
                "Height differs by more than "
                f"{int(config.HEIGHT_JUMP_SUSPECT_RATIO * 100)}% from the "
                "previous accepted height. Kept for review; excluded from "
                "growth rates and medians." if suspect else None)
            if suspect:
                report["suspect_records"] = report.get("suspect_records", 0) + 1

            growth_rate = None
            previous = _previous_observation(repo, plant_id, obs_date_iso)
            if (not suspect and previous and previous.get("height_cm") is not None
                    and previous.get("observation_date")):
                span = dbm.days_between(
                    previous["observation_date"], obs_date_iso)
                if span > 0:
                    growth_rate = round(
                        (float(height) - float(previous["height_cm"])) / span, 4)

            # ------------------------------------------------------------------
            # 4. Insert or recompute the single daily observation.
            # ------------------------------------------------------------------
            try:
                result = repo.upsert_daily_observation(
                    plant_id=plant_id,
                    observation_date=obs_date_iso,
                    age_days=age_days,
                    height_cm=height,
                    growth_rate_cm_per_day=growth_rate,
                    canopy_width_cm=None,  # stem width is NOT canopy width
                    stem_width_cm=stem_width,
                    visible_true_leaf_count=(
                        None if leaves is None else int(round(leaves))),
                    rod_length_cm=row_rod_length,
                    calibration_source=config.CALIBRATION_SOURCE_CAMERA,
                    calibration_verified=rod_verified,
                    quality_flag=quality_flag,
                    quality_note=quality_note,
                    data_source=data_source,
                    reason="camera import",
                )
                action = result.get("action")
                if action == "inserted":
                    report["observations_inserted"] += 1
                elif action == "updated":
                    report["observations_updated"] += 1
                else:
                    report["observations_skipped_duplicate"] += 1
            except DuplicateRecordError:
                report["observations_skipped_duplicate"] += 1
            except ValidationError as exc:
                report["issues"].append({"line": line_no, "problem": str(exc)})
            except Exception as exc:
                report["issues"].append(
                    {"line": line_no, "problem": f"Database error: {exc}"}
                )

    finally:
        if should_close:
            fh.close()

    return report


def rebuild_daily_observation_from_captures(
    repo: BaseRepository,
    plant_id: int,
    observation_date: str,
    *,
    data_source: str = "real",
    reason: str = "reviewed camera capture",
) -> dict:
    """Recompute (or create) the daily observation for a (plant, date) from
    the accepted captures stored for that day. Returns the upsert result."""
    plant = repo.get_plant(plant_id)
    if plant is None:
        raise NotFoundError(f"No plant with plant_id={plant_id}.")
    obs_date_iso = dbm.parse_iso_date(observation_date, "observation_date").isoformat()

    accepted = [
        c for c in repo.captures_on_date(plant_id, obs_date_iso)
        if c.get("review_status") == "accepted"
        and c.get("validity") == VALID_CAPTURE
    ]
    if not accepted:
        # No accepted valid captures: leave any existing daily row untouched
        # rather than deleting data.
        return {"action": "none", "reason": "no accepted valid captures"}

    height = _median(c.get("height_cm") for c in accepted)
    stem_width = _median(
        _positive_stem_width(c.get("stem_width_cm")) for c in accepted)
    leaves = _median(c.get("visible_true_leaf_count") for c in accepted)
    if height is None:
        return {"action": "none", "reason": "no usable height in accepted captures"}

    rod_length_cm = accepted[0].get("rod_length_cm")
    calibration_verified = all(
        bool(c.get("calibration_verified")) for c in accepted)
    calibration_source = accepted[0].get("calibration_source") \
        or config.CALIBRATION_SOURCE_CAMERA

    age_days = dbm.age_days_for_plant(plant, obs_date_iso)

    suspect = dbm.is_height_outlier(
        height, _trailing_heights(repo, plant_id, obs_date_iso))
    quality_flag = "height_jump_suspect" if suspect else None
    quality_note = (
        "Height differs by more than "
        f"{int(config.HEIGHT_JUMP_SUSPECT_RATIO * 100)}% from the trailing "
        "median. Kept for review; excluded from growth rates and medians."
        if suspect else None)

    growth_rate = None
    previous = _previous_observation(repo, plant_id, obs_date_iso)
    if (not suspect and previous and previous.get("height_cm") is not None
            and previous.get("observation_date")):
        span = dbm.days_between(previous["observation_date"], obs_date_iso)
        if span > 0:
            growth_rate = round(
                (float(height) - float(previous["height_cm"])) / span, 4)

    result = repo.upsert_daily_observation(
        plant_id=plant_id,
        observation_date=obs_date_iso,
        age_days=age_days,
        height_cm=height,
        growth_rate_cm_per_day=growth_rate,
        canopy_width_cm=None,
        stem_width_cm=stem_width,
        visible_true_leaf_count=(
            None if leaves is None else int(round(leaves))),
        rod_length_cm=rod_length_cm,
        calibration_source=calibration_source,
        calibration_verified=calibration_verified,
        quality_flag=quality_flag,
        quality_note=quality_note,
        data_source=data_source,
        reason=reason,
    )
    return result
