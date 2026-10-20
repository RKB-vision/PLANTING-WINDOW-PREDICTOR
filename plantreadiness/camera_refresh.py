"""Auto-refresh camera observations from the cloud before prediction.

This module ties the ESP32-CAM -> Cloudinary -> OpenCV -> CSV -> database path
into one bounded, fail-open pass that the backend can run before a species
prediction:

    for each configured source (Cloudinary prefix -> registered plant):
        1. pull new images the vision pipeline has not processed yet,
        2. measure them with the OpenCV pipeline,
        3. format daily rows and import them (append-only, idempotent).

Design constraints (see ``docs/ARCHITECTURE.md``):

* **Single pass, no polling**, and bounded by ``CAMERA_CAPTURES_PER_REFRESH``
  so it is safe to run inside a request.
* **Fail-open**: every failure is reported in the result and never raised into
  the request; prediction then proceeds on whatever data already exists.
* **Explicit registration**: an image whose plant is not registered is skipped,
  never auto-created under a guessed species.
* **No invented data**: the camera importer leaves growth rate unset until a
  second, strictly later measurement exists.

The vision layer (which imports OpenCV) is imported lazily and can be replaced
with a ``puller`` for tests, so this module stays cheap to import.
"""

from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

from . import camera_import
from . import config
from .repository import BaseRepository

__all__ = [
    "CameraSource",
    "refresh_camera_observations",
    "reset_refresh_throttle",
]


@dataclass(frozen=True)
class CameraSource:
    """One Cloudinary prefix mapped to a registered plant."""

    prefix: str
    external_ref: str
    plant_id: Optional[int] = None


# A puller returns zero or more daily CSV rows (camera schema) for a source.
Puller = Callable[[CameraSource, int], list[dict[str, Any]]]

# In-process throttle state, keyed by species (or "*" for all species).
_last_refresh: dict[str, float] = {}


def reset_refresh_throttle() -> None:
    """Clear the in-process throttle (used by tests)."""
    _last_refresh.clear()


def refresh_camera_observations(
    repo: BaseRepository,
    *,
    species: Optional[str] = None,
    sources: Optional[list[tuple[str, str]]] = None,
    captures: Optional[int] = None,
    puller: Optional[Puller] = None,
    csv_path: Optional[Union[str, Path]] = None,
    min_interval: Optional[float] = None,
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """Pull, measure and import new camera images in one bounded pass.

    Parameters
    ----------
    repo:
        Repository used for plant lookup and observation inserts.
    species:
        If given, only sources whose registered plant has this species are
        refreshed (so predicting species X refreshes X's camera).
    sources:
        Explicit ``(prefix, external_ref)`` pairs. When omitted, they are read
        from ``CAMERA_SOURCES`` (and refresh must be enabled).
    captures:
        Max images to pull per source (default ``CAMERA_CAPTURES_PER_REFRESH``).
    puller:
        Injectable vision layer for tests; defaults to the OpenCV pipeline.
    csv_path:
        Audit CSV the new rows are appended to. ``None`` disables the audit
        file (rows are still imported).
    min_interval:
        Throttle window in seconds (default
        ``CAMERA_MIN_REFRESH_INTERVAL_SECONDS``); 0 disables throttling.
    now:
        Injectable monotonic clock for tests.

    Returns
    -------
    dict
        A report: ``enabled``, ``ran``, ``reason``, per-source ``sources``,
        ``rows_written``, aggregate import counts and ``errors``. It never
        raises for expected operational failures.
    """
    report: dict[str, Any] = {
        "enabled": False,
        "ran": False,
        "reason": None,
        "sources": [],
        "rows_written": 0,
        "observations_inserted": 0,
        "observations_skipped_duplicate": 0,
        "errors": [],
    }

    explicit = sources is not None
    if not explicit:
        if not config.camera_refresh_enabled():
            report["reason"] = (
                "Camera refresh is not enabled (configure Cloudinary "
                "credentials, CAMERA_SOURCES and/or CAMERA_REFRESH_ENABLED)."
            )
            return report
        sources = config.camera_sources()

    report["enabled"] = True

    if not sources:
        report["reason"] = "No camera sources are configured."
        return report

    interval = (
        config.CAMERA_MIN_REFRESH_INTERVAL_SECONDS
        if min_interval is None
        else float(min_interval)
    )
    clock = now or time.monotonic
    key = species or "*"
    if interval > 0:
        last = _last_refresh.get(key)
        if last is not None and (clock() - last) < interval:
            report["reason"] = (
                f"Throttled: refreshed {clock() - last:.0f}s ago "
                f"(minimum interval {interval:.0f}s)."
            )
            return report
    _last_refresh[key] = clock()
    report["ran"] = True

    per_source = (
        config.CAMERA_CAPTURES_PER_REFRESH
        if captures is None
        else int(captures)
    )
    if per_source <= 0:
        report["ran"] = False
        report["reason"] = "captures must be at least 1."
        return report

    pull = puller or _default_puller
    plants = _index_plants(repo)

    for prefix, external_ref in sources:
        entry: dict[str, Any] = {
            "prefix": prefix,
            "external_ref": external_ref,
            "status": "skipped",
            "rows": 0,
            "message": None,
        }

        candidates = plants.get(config.normalize_plant_ref(external_ref), [])
        if not candidates:
            entry["status"] = "unregistered"
            entry["message"] = (
                f"No registered plant with external_ref '{external_ref}'. "
                "Register it first; camera images are never auto-assigned."
            )
            report["sources"].append(entry)
            continue

        if len(candidates) > 1:
            entry["status"] = "ambiguous"
            entry["message"] = (
                f"Ambiguous external_ref '{external_ref}': multiple registered "
                f"plants normalize to the same key "
                f"({[p.get('plant_id') for p in candidates]}). Quarantined."
            )
            report["sources"].append(entry)
            continue

        plant = candidates[0]

        if species and (plant.get("species") or "") != species:
            entry["status"] = "other_species"
            entry["plant_id"] = plant.get("plant_id")
            entry["message"] = (
                f"Registered under species '{plant.get('species')}', "
                f"not '{species}'."
            )
            report["sources"].append(entry)
            continue

        entry["plant_id"] = plant.get("plant_id")
        entry["species"] = plant.get("species")

        try:
            rows = pull(
                CameraSource(prefix, external_ref, plant.get("plant_id")),
                per_source,
            ) or []
        except Exception as exc:  # fail-open: report, do not raise
            entry["status"] = "error"
            entry["message"] = str(exc)
            report["errors"].append(f"{prefix}: {exc}")
            report["sources"].append(entry)
            continue

        if not rows:
            entry["status"] = "no_new_images"
            report["sources"].append(entry)
            continue

        try:
            result = _write_and_import(repo, rows, csv_path)
        except Exception as exc:  # fail-open: report, do not raise
            entry["status"] = "import_error"
            entry["message"] = str(exc)
            report["errors"].append(f"{prefix}: {exc}")
            report["sources"].append(entry)
            continue

        entry["status"] = "imported"
        entry["rows"] = len(rows)
        report["rows_written"] += len(rows)
        report["observations_inserted"] += int(
            result.get("observations_inserted", 0)
        )
        report["observations_skipped_duplicate"] += int(
            result.get("observations_skipped_duplicate", 0)
        )
        report["sources"].append(entry)

    return report


def _index_plants(repo: BaseRepository) -> dict[str, list]:
    """Look up registered plants by their normalised external reference.

    A normalised key may map to more than one plant when two registered
    external refs collide (e.g. ``Plant01`` and ``Plant-01``). Callers must
    treat a multi-element value as ambiguous and quarantine the capture.
    """
    index: dict[str, list] = {}
    for plant in repo.list_plants():
        # Historical reference plants are never camera targets.
        _cohort = str(plant.get("cohort") or "").strip().lower()
        if _cohort in ("reference", "manual_upload"):
            continue
        ref = plant.get("external_ref")
        if ref:
            key = config.normalize_plant_ref(ref)
            index.setdefault(key, []).append(plant)
    return index


def _write_and_import(
    repo: BaseRepository,
    rows: list[dict[str, Any]],
    csv_path: Optional[Union[str, Path]],
) -> dict:
    """Append new rows to the audit CSV (optional) and import them only."""
    fieldnames = list(rows[0].keys())

    if csv_path is not None:
        _append_rows(Path(csv_path), rows, fieldnames)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    buffer.seek(0)

    return camera_import.import_camera_measurements(
        repo,
        buffer,
        auto_register=False,
        data_source="real",
    )


def _append_rows(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _default_puller(
    source: CameraSource,
    captures: int,
) -> list[dict[str, Any]]:
    """Measure new Cloudinary images with the OpenCV pipeline (lazy import)."""
    from plant_measurement import (  # noqa: WPS433 (lazy, heavy cv2 import)
        build_measurement_args,
        collect_new_capture_records_once,
    )

    args = build_measurement_args(
        rod_length_cm=config.CAMERA_ROD_LENGTH_CM,
        rod_hue=config.CAMERA_ROD_HUE,
        rod_hue_tolerance=config.CAMERA_ROD_HUE_TOLERANCE,
    )

    records = collect_new_capture_records_once(
        captures,
        args,
        prefix=source.prefix,
    )

    return _rows_from_records(records, source.external_ref)


def _rows_from_records(
    records: list[Any],
    plant_ref: str,
) -> list[dict[str, Any]]:
    """One median-based daily row per capture date.

    Grouping is by the date parsed from the image name, so four images of the
    same plant on the same day produce exactly one row. Failed captures are
    carried through in the row's audit columns and counted, never dropped.
    """
    if not records:
        return []

    from plant_measurement import (  # noqa: WPS433 (lazy, heavy cv2 import)
        group_records_by_date,
        median_daily_record,
    )

    rows: list[dict[str, Any]] = []
    for capture_date, group in sorted(
        group_records_by_date(records).items(),
        key=lambda item: item[0] or "",
    ):
        rows.append(
            median_daily_record(
                capture_date or "unknown",
                plant_ref,
                capture_date,
                group,
                rod_length_cm=config.CAMERA_ROD_LENGTH_CM,
            )
        )
    return rows
