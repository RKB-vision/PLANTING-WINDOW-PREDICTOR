"""
cloudinary_refresh.py — Pull, measure, and import camera images from Cloudinary.

One bounded, fail-open pass of the ESP32-CAM → Cloudinary → OpenCV → SQLite
path:

  1. list new captures for each configured camera source
  2. download and measure them (vision.measure_image)
  3. store the median of same-day captures as one observation per plant/day
  4. return every per-image measurement so the UI can show a carousel

Nothing is invented: uncalibrated captures are stored but flagged
``camera_rod_unverified`` / ``camera_scale_fallback`` and excluded from the
historical reference cohort. A plant with no ``planted_date`` gets
``age_days = NULL`` rather than a guessed age.

Usage:
    import cloudinary_refresh as cr
    report = cr.refresh(db_path, species="Sunflower (SuperOrange)", force=True)
"""

from __future__ import annotations

import base64
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

import db
import vision

# --- Throttle state (per-process) ---
_last_refresh: dict[str, float] = {}

PROCESSED_IDS_FILE = Path("data/generated/processed_cloudinary_ids.txt")
MEASUREMENTS_CACHE_FILE = Path("data/generated/cloudinary_measurements.json")
ANNOTATED_DIR = Path("data/generated/cloudinary/annotated")

# Sources that are historical/manual and must never be targeted by a camera.
NON_CAMERA_COHORTS = ("reference", "manual_upload")


def _load_env() -> None:
    """Load .env if present (simple key=value parser, real env wins)."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


def _creds() -> tuple[str, str, str] | None:
    _load_env()
    cloud = os.getenv("CLOUDINARY_CLOUD_NAME")
    key = os.getenv("CLOUDINARY_API_KEY")
    secret = os.getenv("CLOUDINARY_API_SECRET")
    if cloud and key and secret:
        return cloud, key, secret
    return None


def _camera_sources() -> List[Tuple[str, str]]:
    _load_env()
    raw = os.getenv("CAMERA_SOURCES", "").strip()
    if not raw:
        return []
    sources = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        prefix, ref = chunk.split("=", 1)
        prefix = prefix.strip()
        ref = ref.strip()
        if prefix and ref:
            sources.append((prefix, ref))
    return sources


def _rod_length() -> float:
    _load_env()
    return float(os.getenv("CAMERA_ROD_LENGTH_CM", "12.0"))


def _verified_rod_length() -> Optional[float]:
    """Physically verified rod length, if one has been recorded."""
    _load_env()
    raw = os.getenv("CAMERA_VERIFIED_ROD_LENGTH_CM", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _min_interval() -> float:
    _load_env()
    return float(os.getenv("CAMERA_MIN_REFRESH_INTERVAL_SECONDS", "60"))


def _captures_per_refresh() -> int:
    _load_env()
    return int(os.getenv("CAMERA_CAPTURES_PER_REFRESH", "4"))


def _get_processed_ids() -> set[str]:
    if not PROCESSED_IDS_FILE.exists():
        return set()
    return {line.strip() for line in PROCESSED_IDS_FILE.read_text().splitlines() if line.strip()}


def _mark_processed(public_id: str, already: set[str]) -> None:
    if not public_id or public_id in already:
        return
    PROCESSED_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PROCESSED_IDS_FILE.open("a") as f:
        f.write(public_id + "\n")
    already.add(public_id)


def _load_measurements_cache() -> Dict[str, Dict[str, Any]]:
    if not MEASUREMENTS_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(MEASUREMENTS_CACHE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_measurements_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    MEASUREMENTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEASUREMENTS_CACHE_FILE.write_text(json.dumps(cache, indent=1))


def _get_cloudinary_images(
    prefix: str,
    cloud: str,
    key: str,
    secret: str,
    max_results: int = 500,
) -> List[Dict[str, Any]]:
    """List uploaded images whose public_id starts with ``prefix``.

    The Cloudinary Admin API requires ``type=upload``; omitting it returns
    HTTP 400. Results are paginated with ``next_cursor``.
    """
    resources: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {
            "prefix": prefix,
            "type": "upload",
            "max_results": min(500, max(1, max_results)),
        }
        if cursor:
            params["next_cursor"] = cursor
        url = (
            f"https://api.cloudinary.com/v1_1/{cloud}/resources/image?"
            f"{urllib.parse.urlencode(params)}"
        )
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"{key}:{secret}".encode()).decode(),
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"Cloudinary API error {exc.code}: {body or exc.reason}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Cloudinary API error: {exc}") from exc

        resources.extend(data.get("resources", []) or [])
        cursor = data.get("next_cursor")
        if not cursor or len(resources) >= max_results:
            break
    return resources[:max_results]


def _download_image(url: str) -> np.ndarray:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Failed to decode image")
    return img


def _parse_date_from_id(public_id: str) -> Optional[str]:
    """Extract YYYY-MM-DD from public_id like 'Plant02_20260915_100000'."""
    parts = public_id.split("_")
    if len(parts) >= 2:
        date_part = parts[1]
        if len(date_part) == 8 and date_part.isdigit():
            try:
                d = datetime.strptime(date_part, "%Y%m%d").date()
                return d.isoformat()
            except ValueError:
                pass
    return None


def _capture_date(resource: Dict[str, Any]) -> Tuple[str, str]:
    """Return (iso_date, date_source) for a Cloudinary resource.

    Prefers the timestamp encoded in the public_id (capture time). Falls back
    to Cloudinary's upload timestamp, then to processing time — the source is
    reported so callers can see the date was not embedded in the filename.
    """
    pid = resource.get("public_id", "")
    from_id = _parse_date_from_id(pid)
    if from_id:
        return from_id, "public_id"

    created = resource.get("created_at")
    if created:
        try:
            return (
                datetime.fromisoformat(str(created).replace("Z", "+00:00")).date().isoformat(),
                "upload_time",
            )
        except ValueError:
            pass
    return datetime.now().date().isoformat(), "processing_time"


def age_days_from_planted(planted_date: Any, observation_date: str) -> Optional[int]:
    """Age in days from a real planted_date; None when the date is unknown."""
    if not planted_date:
        return None
    try:
        planted = date.fromisoformat(str(planted_date)[:10])
        observed = date.fromisoformat(str(observation_date)[:10])
    except ValueError:
        return None
    days = (observed - planted).days
    return days if days >= 0 else None


def calibration_for(measured: vision.MeasurementResult, rod_cm: float) -> Tuple[str, bool]:
    """Map (rod detected, rod length verified) to an honest provenance pair."""
    verified_rod = _verified_rod_length()
    rod_length_verified = verified_rod is not None and abs(verified_rod - rod_cm) < 1e-6
    if measured.calibrated and rod_length_verified:
        return "camera_rod_verified", True
    if measured.calibrated:
        return "camera_rod_unverified", False
    return "camera_scale_fallback", False


def _resolve_plant(
    db_path: Path | str,
    external_ref: str,
    species: Optional[str],
    plant_species: Optional[str],
    planted_date: Optional[str],
    auto_register: bool,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Find the camera's plant, optionally registering it on first use.

    Returns (plant, candidates). ``plant`` is None when the reference is
    unregistered/ambiguous; candidates preserves ambiguity for the caller.
    """
    candidates = [
        p for p in db.find_plants_by_ref(db_path, external_ref)
        if str(p.get("cohort") or "").strip().lower() not in NON_CAMERA_COHORTS
    ]
    if len(candidates) == 1:
        plant = candidates[0]
        # A supplied planted_date fills a missing one; it never rewrites history.
        if planted_date and not plant.get("planted_date"):
            db.register_plant(
                db_path,
                plant_id=str(plant["plant_id"]),
                species=str(plant["species"]),
                variety=plant.get("variety") or "",
                planted_date=planted_date,
                cohort=plant.get("cohort"),
                external_ref=plant.get("external_ref"),
            )
            plant = db.get_plant(db_path, plant["plant_id"]) or plant
        return plant, candidates
    if len(candidates) > 1:
        return None, candidates

    register_as = plant_species or species
    if auto_register and register_as:
        plant_id = db.register_plant(
            db_path,
            plant_id=external_ref,
            species=register_as,
            variety="",
            planted_date=planted_date,
            cohort="live",
            external_ref=external_ref,
        )
        plant = db.get_plant(db_path, plant_id)
        return plant, ([plant] if plant else [])
    return None, []


def refresh(
    db_path: "Path | str" = db.DEFAULT_DB_PATH,
    species: Optional[str] = None,
    captures: Optional[int] = None,
    min_interval: Optional[float] = None,
    *,
    force: bool = False,
    reprocess: bool = False,
    auto_register: bool = False,
    plant_species: Optional[str] = None,
    planted_date: Optional[str] = None,
    external_ref: Optional[str] = None,
    rod_cm: Optional[float] = None,
) -> Dict[str, Any]:
    """Pull, measure, and import camera images in one bounded pass.

    Args:
        species: only import sources registered under this species.
        captures: max new captures per source (default from env).
        min_interval: throttle override in seconds; None uses env.
        force: bypass the throttle entirely (UI button clicks).
        reprocess: re-measure captures already in the processed ledger.
        auto_register: register an unknown camera source instead of failing.
        plant_species: species to register a new camera plant under.
        planted_date: optional planted date for the new/known plant; drives
            ``age_days``. Never guessed when omitted.
        external_ref: restrict to one configured source's external reference.
        rod_cm: reference rod length override.

    Returns a report dict; operational failures are reported, not raised.
    """
    report: Dict[str, Any] = {
        "enabled": False,
        "ran": False,
        "reason": None,
        "sources": [],
        "images": [],
        "warnings": [],
        "rows_written": 0,
        "observations_inserted": 0,
        "observations_skipped_duplicate": 0,
        "errors": [],
    }

    creds = _creds()
    sources = _camera_sources()

    if not creds:
        report["reason"] = "Cloudinary credentials not configured."
        return report
    if not sources:
        report["reason"] = "No camera sources configured (set CAMERA_SOURCES in .env)."
        return report

    if external_ref:
        wanted = db.normalize_plant_ref(external_ref)
        sources = [(p, r) for (p, r) in sources if db.normalize_plant_ref(r) == wanted]
        if not sources:
            report["reason"] = f"No CAMERA_SOURCES entry matches '{external_ref}'."
            return report

    report["enabled"] = True

    interval = 0.0 if force else (_min_interval() if min_interval is None else float(min_interval))
    clock = time.monotonic
    key = species or "*"
    if interval > 0:
        last = _last_refresh.get(key)
        if last is not None and (clock() - last) < interval:
            report["reason"] = (
                f"Throttled: refreshed {clock() - last:.0f}s ago (min {interval:.0f}s)."
            )
            return report
    _last_refresh[key] = clock()
    report["ran"] = True

    per_source = _captures_per_refresh() if captures is None else int(captures)
    if per_source <= 0:
        report["ran"] = False
        report["reason"] = "captures must be at least 1."
        return report

    rod_cm = _rod_length() if rod_cm is None else float(rod_cm)
    processed = _get_processed_ids()
    cache = _load_measurements_cache()

    for prefix, source_ref in sources:
        entry: Dict[str, Any] = {
            "prefix": prefix,
            "external_ref": source_ref,
            "status": "skipped",
            "rows": 0,
            "message": None,
            "images": [],
            "warnings": [],
        }

        plant, candidates = _resolve_plant(
            db_path,
            external_ref=source_ref,
            species=species,
            plant_species=plant_species,
            planted_date=planted_date,
            auto_register=auto_register,
        )
        if plant is None:
            if len(candidates) > 1:
                entry["status"] = "ambiguous"
                entry["message"] = (
                    f"Ambiguous external_ref '{source_ref}': {len(candidates)} plants. Quarantined."
                )
            else:
                entry["status"] = "unregistered"
                entry["message"] = (
                    f"No registered plant with external_ref '{source_ref}'. "
                    "Register it first or enable auto-registration."
                )
            report["sources"].append(entry)
            continue

        plant_id = str(plant["plant_id"])
        entry["plant_id"] = plant_id
        entry["species"] = plant.get("species")

        if species and db.normalize_species(plant.get("species")) != db.normalize_species(species):
            entry["status"] = "other_species"
            entry["message"] = (
                f"Registered under '{plant.get('species')}', not '{species}'."
            )
            report["sources"].append(entry)
            continue
        if not plant.get("planted_date"):
            entry["warnings"].append(
                f"Plant '{plant_id}' has no planted_date; age_days stays NULL (not guessed)."
            )

        cloud, api_key, secret = creds
        try:
            resources = _get_cloudinary_images(prefix, cloud, api_key, secret)
        except Exception as e:
            entry["status"] = "error"
            entry["message"] = str(e)
            report["errors"].append(f"{prefix}: {e}")
            report["sources"].append(entry)
            continue

        new_images = [r for r in resources if reprocess or r.get("public_id") not in processed]
        new_images.sort(key=lambda r: r.get("created_at", ""))
        # Keep the newest captures of this pass.
        new_images = new_images[-per_source:]

        if not new_images:
            entry["status"] = "no_new_images"
            entry["message"] = "No new captures; showing previously measured images."
            cached = [
                v for v in cache.values()
                if v.get("prefix") == prefix
            ]
            cached.sort(key=lambda v: (v.get("date") or "", v.get("public_id") or ""))
            entry["images"] = cached[-per_source:]
            report["sources"].append(entry)
            continue

        rows_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for resource in new_images:
            pid = resource.get("public_id", "")
            url = resource.get("secure_url", "")
            obs_date, date_source = _capture_date(resource)
            try:
                img = _download_image(url)
                measured = vision.measure_image(img, rod_length_cm=rod_cm)
                calib_source, calib_verified = calibration_for(measured, rod_cm)
                if not measured.calibrated:
                    warning = (
                        f"{pid}: reference rod not detected — measurements use a "
                        "fallback scale and are excluded from the reference cohort."
                    )
                    entry["warnings"].append(warning)
                    report["warnings"].append(warning)
                if measured.plant_rect is None or measured.plant_rect.h == 0:
                    entry["warnings"].append(f"{pid}: no plant foliage detected.")
                    report["warnings"].append(f"{pid}: no plant foliage detected.")

                age_days = age_days_from_planted(plant.get("planted_date"), obs_date)
                annotated_path = None
                try:
                    ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)
                    annotated_file = ANNOTATED_DIR / f"{pid}.png"
                    if cv2.imwrite(str(annotated_file), measured.annotated_image):
                        annotated_path = str(annotated_file)
                except Exception:  # noqa: BLE001 - display artefact is best-effort
                    annotated_path = None
                image_row = {
                    "url": url,
                    "public_id": pid,
                    "prefix": prefix,
                    "plant_id": plant_id,
                    "date": obs_date,
                    "date_source": date_source,
                    "height_cm": measured.height_cm,
                    "stem_width_cm": measured.stem_width_cm,
                    "leaf_count": measured.leaf_count,
                    "leaf_count_method": measured.leaf_count_method,
                    "calibrated": measured.calibrated,
                    "calibration_source": calib_source,
                    "pixels_per_cm": round(measured.pixels_per_cm, 2),
                    "rod_length_cm": rod_cm,
                    "age_days": age_days,
                    "annotated_path": annotated_path,
                }
                entry["images"].append(image_row)
                cache[pid] = image_row
                rows_by_date.setdefault(obs_date, []).append(
                    {
                        "height_cm": measured.height_cm,
                        "stem_width_cm": measured.stem_width_cm,
                        "leaf_count": measured.leaf_count,
                        "calibrated": measured.calibrated,
                        "calibration_source": calib_source,
                        "age_days": age_days,
                    }
                )
                _mark_processed(pid, processed)
            except Exception as e:
                entry.setdefault("measure_errors", []).append(f"{pid}: {e}")
                report["errors"].append(f"{prefix}/{pid}: {e}")

        if not rows_by_date:
            entry["status"] = "no_valid_measurements"
            report["sources"].append(entry)
            continue

        inserted = 0
        skipped = 0
        for obs_date, day_rows in rows_by_date.items():
            heights = [r["height_cm"] for r in day_rows if r["height_cm"] is not None]
            widths = [r["stem_width_cm"] for r in day_rows if r["stem_width_cm"] is not None]
            leaves = [r["leaf_count"] for r in day_rows if r["leaf_count"] is not None]
            if not heights:
                continue

            med_height = statistics.median(heights)
            med_width = statistics.median(widths) if widths else None
            med_leaves = int(round(statistics.median(leaves))) if leaves else None
            # Verified only if every capture on the day was verified.
            calib_source = (
                "camera_rod_verified"
                if all(r["calibration_source"] == "camera_rod_verified" for r in day_rows)
                else (
                    "camera_rod_unverified"
                    if all(r["calibration_source"] in ("camera_rod_verified", "camera_rod_unverified") for r in day_rows)
                    else "camera_scale_fallback"
                )
            )
            calib_verified = calib_source == "camera_rod_verified"
            age_days = day_rows[0]["age_days"]

            try:
                db.record_observation(
                    db_path,
                    plant_id=plant_id,
                    observation_date=obs_date,
                    height_cm=med_height,
                    stem_width_cm=med_width,
                    leaf_count=med_leaves,
                    age_days=age_days,
                    source="camera",
                    calibration_source=calib_source,
                    calibration_verified=calib_verified,
                    rod_length_cm=rod_cm,
                )
                inserted += 1
            except Exception as e:
                skipped += 1
                report["errors"].append(f"{prefix}/{obs_date}: {e}")

        entry["status"] = "imported"
        entry["rows"] = len(rows_by_date)
        report["rows_written"] += len(rows_by_date)
        report["observations_inserted"] += inserted
        report["observations_skipped_duplicate"] += skipped
        report["sources"].append(entry)

    _save_measurements_cache(cache)
    report["images"] = [
        image for source in report["sources"] for image in source.get("images", [])
    ]
    plants_seen = {
        source.get("plant_id") for source in report["sources"] if source.get("plant_id")
    }
    report["plant_id"] = plants_seen.pop() if len(plants_seen) == 1 else None
    report["species"] = next(
        (source.get("species") for source in report["sources"] if source.get("species")), None
    )
    return report


def reset_throttle() -> None:
    _last_refresh.clear()


def configured_sources() -> List[Tuple[str, str]]:
    """Public accessor for the configured (public_id prefix, external_ref) pairs."""
    return _camera_sources()


def configured_rod_length() -> float:
    """Public accessor for the configured reference-rod length in cm."""
    return _rod_length()


def cloudinary_configured() -> bool:
    """True when credentials and at least one camera source are present."""
    return _creds() is not None and bool(_camera_sources())
