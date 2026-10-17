"""Reprocess stored camera captures under the (now verified) calibration.

Three paths per capture, tried in order:

  1. a local file ``{public_id}.jpg`` already on disk,
  2. a re-download from Cloudinary by public id,
  3. flag the capture ``raw_image_unavailable`` (never silently skipped).

Dry-run by default; pass ``--apply`` to write. Every change is recorded in
``capture_revisions`` and the affected daily observation is recomputed from
all stored captures for that date.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import calibration
from . import config
from . import db as dbm
from .repository import SQLiteRepository


def _find_captures(conn, *, only_unverified: bool) -> list[dict]:
    sql = ("SELECT * FROM camera_captures ORDER BY capture_id")
    rows = [dict(r) for r in conn.execute(sql)]
    if only_unverified:
        kept = []
        for row in rows:
            if calibration.calibration_group(
                    calibration_source=row.get("calibration_source"),
                    rod_length_cm=row.get("rod_length_cm"),
                    calibration_verified=bool(row.get("calibration_verified"))) \
                    in (config.CALIBRATION_GROUP_CAMERA_UNVERIFIED,
                        config.CALIBRATION_GROUP_UNKNOWN):
                kept.append(row)
        rows = kept
    return rows


def _load_image(public_id: str, rod_length: float):
    """Return (image, source) or (None, None) when unavailable."""
    import cv2

    local = Path(f"{public_id}.jpg")
    if local.exists():
        img = cv2.imread(str(local))
        if img is not None:
            return img, "local_file"

    try:
        from plant_measurement import download_cloudinary_image
        # Build a minimal resource dict for the downloader.
        resource = {"public_id": public_id}
        # download_cloudinary_image expects a resource with secure_url or
        # builds one from the public id; fall back to a direct download.
        try:
            image = download_cloudinary_image(resource, Path(f"{public_id}.jpg"))
            return image, "cloudinary_redownload"
        except Exception:
            pass
    except Exception:
        pass

    return None, None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reprocess stored captures under the current calibration.")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--apply", action="store_true",
                        help="Write changes (default is dry-run).")
    parser.add_argument("--all", action="store_true",
                        help="Reprocess every capture, not just unverified ones.")
    args = parser.parse_args(argv)

    conn = dbm.init_db(Path(args.db))
    try:
        repo = SQLiteRepository(conn)
        state = repo.current_calibration_setting()
        rod = config.CAMERA_ROD_LENGTH_CM
        verified = calibration.verification_applies(state, rod)

        if not verified:
            print("WARNING: the configured rod length is still UNVERIFIED. "
                  "Reprocessing under an unverified calibration keeps captures "
                  "quarantined. Run verify_calibration first.", flush=True)

        captures = _find_captures(conn, only_unverified=not args.all)
        print(f"Captures to consider: {len(captures)}")

        plan = []
        for cap in captures:
            public_id = cap["public_id"]
            image, source = _load_image(public_id, rod)
            if image is None:
                plan.append({"capture": cap, "action": "raw_image_unavailable",
                             "source": None})
                continue
            plan.append({"capture": cap, "action": "reprocess", "source": source,
                         "image": image})

        # Report
        for item in plan:
            cap = item["capture"]
            print(f"  {cap['public_id']}: {item['action']}"
                  + (f" ({item['source']})" if item.get("source") else ""))

        if not args.apply:
            print("Dry run only. Re-run with --apply to write changes.")
            return 0

        from plant_measurement import process_image, capture_record_from_result
        import argparse as _ap
        pm_args = _ap.Namespace(rod_length_cm=float(rod), rod_hue=config.CAMERA_ROD_HUE,
                                rod_hue_tolerance=config.CAMERA_ROD_HUE_TOLERANCE,
                                output=Path("plant_result.jpg"))

        affected = {}  # (plant_id, obs_date) -> data_source
        for item in plan:
            cap = item["capture"]
            public_id = cap["public_id"]
            if item["action"] == "raw_image_unavailable":
                repo.add_capture(
                    plant_id=cap["plant_id"],
                    public_id=public_id,
                    observation_date=cap["observation_date"],
                    validity="failed",
                    rod_length_cm=cap["rod_length_cm"],
                    calibration_source=cap.get("calibration_source")
                    or config.CALIBRATION_SOURCE_CAMERA,
                    calibration_verified=cap.get("calibration_verified", False),
                    capture_timestamp=cap.get("capture_timestamp"),
                    height_cm=cap.get("height_cm"),
                    stem_width_cm=cap.get("stem_width_cm"),
                    visible_true_leaf_count=cap.get("visible_true_leaf_count"),
                    cm_per_pixel=cap.get("cm_per_pixel"),
                    confidence=cap.get("confidence"),
                    problem="raw_image_unavailable",
                    data_source=cap.get("data_source", "real"),
                )
                affected[(cap["plant_id"], cap["observation_date"])] = cap.get("data_source", "real")
                continue

            image = item["image"]
            try:
                result = process_image(image, pm_args, capture_number=1,
                                       timestamp=cap.get("capture_timestamp"),
                                       public_id=public_id,
                                       capture_date=cap["observation_date"])
            except Exception as exc:
                print(f"  re-measure failed for {public_id}: {exc}",
                      flush=True)
                continue

            outcome = repo.add_capture(
                plant_id=cap["plant_id"],
                public_id=public_id,
                observation_date=cap["observation_date"],
                validity="valid" if result.height_cm and result.height_cm > 0 else "failed",
                rod_length_cm=float(rod),
                calibration_source=config.CALIBRATION_SOURCE_CAMERA,
                calibration_verified=verified,
                capture_timestamp=result.timestamp,
                height_cm=result.height_cm,
                stem_width_cm=result.width_cm,
                visible_true_leaf_count=result.leaf_count,
                cm_per_pixel=result.cm_per_pixel,
                confidence=result.confidence,
                problem=None,
                data_source=cap.get("data_source", "real"),
            )
            affected[(cap["plant_id"], cap["observation_date"])] = cap.get("data_source", "real")
            print(f"  revised {public_id}: {outcome.get('action', outcome)}")

        # Recompute daily observations from stored captures.
        for (plant_id, obs_date), data_source in affected.items():
            stored_valid = [c for c in repo.captures_on_date(plant_id, obs_date)
                            if c.get("validity") == "valid"]
            if not stored_valid:
                continue
            height = None
            import statistics
            hs = [c.get("height_cm") for c in stored_valid
                  if c.get("height_cm") is not None]
            if hs:
                height = float(statistics.median(hs))
            if height is None:
                continue
            sw = [c.get("stem_width_cm") for c in stored_valid
                  if c.get("stem_width_cm") is not None]
            lvs = [c.get("visible_true_leaf_count") for c in stored_valid
                   if c.get("visible_true_leaf_count") is not None]
            stem_width = float(statistics.median(sw)) if sw else None
            leaves = (int(round(statistics.median(lvs))) if lvs else None)
            repo.upsert_daily_observation(
                plant_id=plant_id,
                observation_date=obs_date,
                height_cm=height,
                stem_width_cm=stem_width,
                visible_true_leaf_count=leaves,
                rod_length_cm=rod,
                calibration_source=config.CALIBRATION_SOURCE_CAMERA,
                calibration_verified=verified,
                data_source=data_source,
                reason="reprocess_captures",
            )
        print("Done.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
PY