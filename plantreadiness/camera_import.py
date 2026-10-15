"""Import daily measurements produced by the ESP32-CAM / OpenCV pipeline.

The hardware pipeline (plant_measurement.py) records daily summaries to
plant_daily_measurements.csv with the following schema:

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
  It is intentionally NOT mapped to canopy_width_cm to avoid biological
  misrepresentation.
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
from pathlib import Path
from typing import Any, Optional, Union

from . import config
from . import db as dbm
from .repository import BaseRepository, DuplicateRecordError, ValidationError

CAMERA_REQUIRED_COLUMNS = (
    "plant_id",
    "average_height_cm",
    "first_capture_timestamp",
)


def _to_float(value: Any) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _to_leaf_count(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    f = float(value)
    return max(0, int(round(f)))


def import_camera_measurements(
    repo: BaseRepository,
    source: Union[str, Path, io.StringIO, io.TextIOBase],
    *,
    default_species: str = "Super Orange",
    default_variety: Optional[str] = None,
    auto_register: bool = True,
    data_source: str = "real",
) -> dict:
    """Import measurements from an ESP32-CAM output CSV into the repository.

    Parameters
    ----------
    repo : BaseRepository
        The database repository (SQLiteRepository or SupabaseRepository).
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
        "observations_skipped_duplicate": 0,
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

        # Cache existing plants by external_ref
        existing_plants = repo.list_plants()
        ref_to_plant: dict[str, dict] = {}
        id_to_plant: dict[int, dict] = {}
        for p in existing_plants:
            if p.get("external_ref"):
                ref_to_plant[str(p["external_ref"]).strip().lower()] = p
            ref_to_plant[str(p["plant_id"])] = p
            id_to_plant[int(p["plant_id"])] = p

        for line_no, row in enumerate(reader, start=2):
            report["rows_read"] += 1
            raw_id = (row.get("plant_id") or "").strip()
            if not raw_id:
                report["issues"].append(
                    {"line": line_no, "problem": "Missing plant_id."}
                )
                continue

            # Extract date from first_capture_timestamp or last_capture_timestamp
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

            # Handle ISO string like '2026-10-08T11:26:08' or '2026-10-08'
            date_str = ts.split("T")[0].split(" ")[0].strip()
            try:
                obs_date = dbm.parse_iso_date(date_str, "capture timestamp")
            except Exception as exc:
                report["issues"].append(
                    {"line": line_no, "problem": f"Invalid timestamp '{ts}': {exc}"}
                )
                continue

            # Parse height
            try:
                height = _to_float(row.get("average_height_cm"))
            except (TypeError, ValueError) as exc:
                report["issues"].append(
                    {"line": line_no, "problem": f"Bad average_height_cm: {exc}"}
                )
                continue

            if height is None:
                report["issues"].append(
                    {
                        "line": line_no,
                        "problem": "Missing average_height_cm; row skipped (no fabricated data).",
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

            # Parse leaves if present
            try:
                leaves = _to_leaf_count(row.get("average_visible_leaves"))
            except (TypeError, ValueError):
                leaves = None

            # Resolve plant in database
            target_plant = ref_to_plant.get(raw_id.lower())
            if target_plant is None and raw_id.isdigit():
                target_plant = id_to_plant.get(int(raw_id))

            if target_plant is None:
                if auto_register:
                    new_id = repo.register_plant(
                        species=default_species,
                        variety=default_variety,
                        external_ref=raw_id,
                        data_source=data_source,
                    )
                    target_plant = repo.get_plant(new_id)
                    if target_plant:
                        ref_to_plant[raw_id.lower()] = target_plant
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

            # Derive age_days and growth_rate if possible
            age_days = None
            if target_plant.get("date_started"):
                try:
                    delta = dbm.days_between(
                        target_plant["date_started"], obs_date.isoformat()
                    )
                    if delta >= 0:
                        age_days = delta
                except Exception:
                    pass

            growth_rate = None
            try:
                latest = repo.latest_observation(plant_id)
                if (
                    latest
                    and latest.get("height_cm") is not None
                    and latest.get("observation_date")
                ):
                    span = dbm.days_between(
                        latest["observation_date"], obs_date.isoformat()
                    )
                    if span > 0:
                        delta_h = float(height) - float(latest["height_cm"])
                        growth_rate = round(delta_h / span, 4)
            except Exception:
                pass

            # Insert observation
            try:
                repo.add_observation(
                    plant_id=plant_id,
                    observation_date=obs_date.isoformat(),
                    height_cm=height,
                    age_days=age_days,
                    growth_rate_cm_per_day=growth_rate,
                    canopy_width_cm=None,  # stem width is NOT canopy width; left None
                    visible_true_leaf_count=leaves,
                    data_source=data_source,
                )
                report["observations_inserted"] += 1
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
