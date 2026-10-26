"""
predictor.py — One-shot measurement → prediction cycle.

This is the single code path behind the "pull images and predict" button:

    1. fetch captures   (Cloudinary, or uploaded image bytes)
    2. measure          (vision: height, stem width, leaf count)
    3. persist          (one median observation per plant/day)
    4. train/validate   (ml: GroupKFold model, only if it beats the baseline)
    5. predict          (ml fallback ladder, honest method + sufficiency)
    6. save prediction  (append-only, with supporting evidence)

Streamlit (app.py) and the CLI both call this module, so the behaviour is
identical and testable without a UI. Predictions are only written when a
number was actually produced; an "insufficient evidence" result is reported
to the caller but not persisted as a fake date.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import cloudinary_refresh as cr
import db
import ml
import vision


@dataclass
class CycleResult:
    ok: bool
    stage: str  # 'fetch' | 'measure' | 'predict' | 'complete'
    messages: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    report: Dict[str, Any] = field(default_factory=dict)
    images: List[Dict[str, Any]] = field(default_factory=list)
    plant: Optional[Dict[str, Any]] = None
    observation: Optional[Dict[str, Any]] = None
    prediction: Optional[Dict[str, Any]] = None
    prediction_id: Optional[int] = None
    training: Dict[str, Any] = field(default_factory=dict)
    annotated_images: List[Any] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe view (annotated numpy images are dropped)."""
        data = asdict(self)
        data.pop("annotated_images", None)
        return data


def _median(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    return statistics.median(clean) if clean else None


def _median_int(values: Sequence[Optional[int]]) -> Optional[int]:
    clean = [int(v) for v in values if v is not None]
    return int(round(statistics.median(clean))) if clean else None


def _ensure_plant(
    db_path: Path | str,
    plant_ref: str,
    species: str,
    planted_date: Optional[str] = None,
    auto_register: bool = True,
) -> Optional[Dict[str, Any]]:
    """Resolve a plant by id/external ref, registering it on first use."""
    candidates = db.find_plants_by_ref(db_path, plant_ref)
    if len(candidates) > 1:
        return None
    if len(candidates) == 1:
        plant = candidates[0]
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
        return plant

    if not auto_register:
        return None
    plant_id = db.register_plant(
        db_path,
        plant_id=plant_ref,
        species=species,
        variety="",
        planted_date=planted_date,
        cohort="live",
        external_ref=plant_ref,
    )
    return db.get_plant(db_path, plant_id)


def _predict_and_save(
    db_path: Path | str,
    species: str,
    plant_id: str,
    observation: Optional[Dict[str, Any]],
    images: List[Dict[str, Any]],
    report: Dict[str, Any],
    messages: List[str],
    warnings: List[str],
) -> Dict[str, Any]:
    """Train, predict, and persist the prediction; returns cycle metadata."""
    train_res = ml.train_countdown_model(db_path, species=species)
    pipeline = train_res.get("pipeline") if train_res.get("validated") else None
    if train_res.get("validated"):
        messages.append(
            f"Validated {train_res.get('best_model_name')} model "
            f"(MAE {train_res.get('best_mae')} d vs baseline "
            f"{train_res.get('baseline_mae')} d)."
        )
    else:
        messages.append(
            f"ML model not validated ({train_res.get('reason', 'criteria not met')}); "
            "using the historical baseline."
        )

    pred = ml.predict_species_readiness(
        db_path=db_path,
        species=species,
        plant_id=plant_id,
        current_obs_date=(observation or {}).get("observation_date"),
        current_height_cm=(observation or {}).get("height_cm"),
        current_age_days=(observation or {}).get("age_days"),
        current_growth_rate=(observation or {}).get("growth_rate_cm_per_day"),
        current_stem_width=(observation or {}).get("stem_width_cm"),
        current_leaf_count=(observation or {}).get("visible_true_leaf_count"),
        trained_model=pipeline,
    )

    prediction_id: Optional[int] = None
    if pred.predicted_days_remaining is not None:
        evidence = {
            "method": pred.method,
            "age_source": pred.age_source,
            "data_sufficient": pred.data_sufficient,
            "inputs": pred.details.get("inputs", {
                k: (observation or {}).get(k)
                for k in ("height_cm", "stem_width_cm", "visible_true_leaf_count", "age_days")
            }),
            "cohort_stats": pred.details.get("cohort_stats"),
            "limitations": pred.limitations,
            "images": [
                {
                    "public_id": img.get("public_id"),
                    "date": img.get("date"),
                    "height_cm": img.get("height_cm"),
                    "stem_width_cm": img.get("stem_width_cm"),
                    "leaf_count": img.get("leaf_count"),
                    "leaf_count_method": img.get("leaf_count_method"),
                    "calibrated": img.get("calibrated"),
                    "calibration_source": img.get("calibration_source"),
                }
                for img in images
            ],
            "vision": {
                "leaf_count_method": (images[0].get("leaf_count_method") if images else None),
                "rod_length_cm": (images[0].get("rod_length_cm") if images else None),
            },
            "camera_report": {
                "sources": [
                    {
                        "prefix": s.get("prefix"),
                        "status": s.get("status"),
                        "rows": s.get("rows"),
                    }
                    for s in report.get("sources", [])
                ]
            },
            "training": {
                "validated": bool(train_res.get("validated")),
                "best_model_name": train_res.get("best_model_name"),
                "best_mae": train_res.get("best_mae"),
                "baseline_mae": train_res.get("baseline_mae"),
                "n_plants": train_res.get("n_plants"),
                "n_samples": train_res.get("n_samples"),
            },
        }
        prediction_id = db.save_prediction(
            db_path,
            species=species,
            # "as of" date: keeps feedback evaluation (actual - prediction_date) meaningful.
            prediction_date=(observation or {}).get("observation_date") or date.today(),
            predicted_days=pred.predicted_days_remaining,
            predicted_date=pred.predicted_readiness_date,
            method=pred.method,
            confidence=pred.confidence,
            plant_id=plant_id,
            supporting_evidence=json.dumps(evidence, default=str),
        )
        messages.append(
            f"Prediction #{prediction_id} saved ({pred.method}, {pred.confidence} confidence)."
        )
    else:
        messages.append("No prediction saved: the evidence is insufficient.")
        warnings.extend(pred.limitations)

    return {
        "prediction": pred.to_dict(),
        "prediction_id": prediction_id,
        "training": {
            "validated": bool(train_res.get("validated")),
            "best_model_name": train_res.get("best_model_name"),
            "best_mae": train_res.get("best_mae"),
            "baseline_mae": train_res.get("baseline_mae"),
            "n_plants": train_res.get("n_plants"),
            "n_samples": train_res.get("n_samples"),
            "reason": train_res.get("reason"),
        },
    }


def run_camera_cycle(
    db_path: Path | str = db.DEFAULT_DB_PATH,
    species: Optional[str] = None,
    captures: int = 4,
    rod_length_cm: Optional[float] = None,
    planted_date: Optional[str] = None,
    external_ref: Optional[str] = None,
    force: bool = True,
    reprocess: bool = False,
    auto_register: bool = True,
) -> CycleResult:
    """Cloudinary → measure → save → predict → persist, in one call."""
    report = cr.refresh(
        db_path,
        species=species,
        captures=captures,
        force=force,
        reprocess=reprocess,
        auto_register=auto_register,
        plant_species=species,
        planted_date=planted_date,
        external_ref=external_ref,
        rod_cm=rod_length_cm,
    )

    messages: List[str] = []
    warnings: List[str] = list(report.get("warnings") or [])

    if not report.get("enabled"):
        return CycleResult(
            ok=False,
            stage="fetch",
            messages=[report.get("reason") or "Cloudinary is not configured."],
            report=report,
        )

    for source in report.get("sources", []):
        if source.get("status") != "imported":
            detail = source.get("message") or source.get("status")
            warnings.append(f"{source.get('prefix')}: {detail}")
        warnings.extend(source.get("warnings") or [])

    plant_ids = sorted({s["plant_id"] for s in report.get("sources", []) if s.get("plant_id")})
    if not plant_ids:
        return CycleResult(
            ok=False,
            stage="fetch",
            messages=["No camera plant could be resolved."] if not report.get("errors") else [],
            warnings=warnings,
            report=report,
        )
    if len(plant_ids) > 1:
        return CycleResult(
            ok=False,
            stage="fetch",
            messages=["Multiple plants matched the configured cameras; refusing to guess."],
            warnings=warnings,
            report=report,
        )

    plant_id = plant_ids[0]
    plant = db.get_plant(db_path, plant_id)
    if plant is None:
        return CycleResult(
            ok=False,
            stage="fetch",
            messages=[f"Plant '{plant_id}' disappeared from the database."],
            report=report,
        )

    if report.get("observations_inserted"):
        messages.append(
            f"Saved {report['observations_inserted']} daily observation(s) from "
            f"{len(report.get('images', []))} capture(s)."
        )
    elif report.get("observations_skipped_duplicate"):
        messages.append("Captures matched existing observations; nothing new was written.")

    observation = db.get_latest_observation(db_path, plant_id)
    if observation is None:
        return CycleResult(
            ok=False,
            stage="measure",
            messages=["No observation exists for this plant yet."],
            warnings=warnings,
            report=report,
            plant=plant,
        )

    cycle_species = plant.get("species") or species
    result = _predict_and_save(
        db_path,
        species=cycle_species,
        plant_id=str(plant_id),
        observation=observation,
        images=report.get("images", []),
        report=report,
        messages=messages,
        warnings=warnings,
    )

    return CycleResult(
        ok=True,
        stage="complete",
        messages=messages,
        warnings=warnings,
        report=report,
        images=report.get("images", []),
        plant=plant,
        observation=observation,
        **result,
    )


def run_image_cycle(
    db_path: Path | str = db.DEFAULT_DB_PATH,
    species: str = "Sunflower(SuperOrange)",
    images: Sequence[Union[bytes, Any]] = (),
    rod_length_cm: float = 12.0,
    plant_ref: str = "camera",
    planted_date: Optional[str] = None,
    observation_date: Optional[date | str] = None,
    auto_register: bool = True,
) -> CycleResult:
    """Uploaded/bytes → measure → save → predict → persist, in one call."""
    if not images:
        return CycleResult(ok=False, stage="fetch", messages=["No images supplied."])

    plant = _ensure_plant(db_path, plant_ref, species, planted_date, auto_register)
    if plant is None:
        return CycleResult(
            ok=False,
            stage="fetch",
            messages=[
                f"Could not resolve plant '{plant_ref}' (missing, ambiguous, or "
                "auto-registration disabled)."
            ],
        )

    plant_id = str(plant["plant_id"])
    obs_date = (
        db.parse_date(str(observation_date))
        if observation_date
        else date.today()
    )

    messages: List[str] = []
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []
    image_rows: List[Dict[str, Any]] = []
    annotated: List[Any] = []

    for index, image_input in enumerate(images):
        try:
            measured = vision.measure_image(image_input, rod_length_cm=rod_length_cm)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the cycle
            warnings.append(f"Image {index + 1} could not be measured: {exc}")
            continue

        calib_source, calib_verified = cr.calibration_for(measured, rod_length_cm)
        if not measured.calibrated:
            warnings.append(
                f"Image {index + 1}: reference rod not detected — measurements use "
                "a fallback scale and are excluded from the reference cohort."
            )
        if not calib_verified:
            warnings.append(
                f"Image {index + 1}: rod length {rod_length_cm:g} cm is not marked "
                "verified; observation is stored but quarantined from the cohort."
            )

        image_rows.append(
            {
                "url": None,
                "public_id": f"upload_{index + 1}",
                "plant_id": plant_id,
                "date": obs_date.isoformat(),
                "date_source": "supplied" if observation_date else "processing_time",
                "height_cm": measured.height_cm,
                "stem_width_cm": measured.stem_width_cm,
                "leaf_count": measured.leaf_count,
                "leaf_count_method": measured.leaf_count_method,
                "calibrated": measured.calibrated,
                "calibration_source": calib_source,
                "pixels_per_cm": round(measured.pixels_per_cm, 2),
                "rod_length_cm": rod_length_cm,
            }
        )
        rows.append(
            {
                "height_cm": measured.height_cm,
                "stem_width_cm": measured.stem_width_cm,
                "leaf_count": measured.leaf_count,
                "calibrated": measured.calibrated,
                "calibration_source": calib_source,
            }
        )
        annotated.append(measured.annotated_image)

    if not rows:
        return CycleResult(
            ok=False,
            stage="measure",
            messages=["None of the supplied images could be measured."],
            warnings=warnings,
            plant=plant,
        )

    height = _median([r["height_cm"] for r in rows])
    if height is None:
        return CycleResult(
            ok=False,
            stage="measure",
            messages=["No plant was detected in the supplied images."],
            warnings=warnings,
            plant=plant,
            images=image_rows,
            annotated_images=annotated,
        )

    age_days = cr.age_days_from_planted(plant.get("planted_date"), obs_date.isoformat())
    calib_verified = all(r["calibration_source"] == "camera_rod_verified" for r in rows)
    calib_source = (
        "camera_rod_verified"
        if calib_verified
        else (
            "camera_rod_unverified"
            if all(r["calibration_source"] in ("camera_rod_verified", "camera_rod_unverified") for r in rows)
            else "camera_scale_fallback"
        )
    )

    db.record_observation(
        db_path,
        plant_id=plant_id,
        observation_date=obs_date,
        height_cm=height,
        stem_width_cm=_median([r["stem_width_cm"] for r in rows]),
        leaf_count=_median_int([r["leaf_count"] for r in rows]),
        age_days=age_days,
        source="camera" if all(r["calibrated"] for r in rows) else "manual_upload",
        calibration_source=calib_source,
        calibration_verified=calib_verified,
        rod_length_cm=rod_length_cm,
    )
    messages.append(
        f"Saved 1 observation for {plant_id} on {obs_date.isoformat()} "
        f"({len(rows)} image(s), median measurement)."
    )

    observation = db.get_observation(db_path, plant_id, obs_date) or db.get_latest_observation(db_path, plant_id)
    result = _predict_and_save(
        db_path,
        species=plant.get("species") or species,
        plant_id=plant_id,
        observation=observation,
        images=image_rows,
        report={"sources": []},
        messages=messages,
        warnings=warnings,
    )

    return CycleResult(
        ok=True,
        stage="complete",
        messages=messages,
        warnings=warnings,
        images=image_rows,
        plant=db.get_plant(db_path, plant_id) or plant,
        observation=observation,
        annotated_images=annotated,
        **result,
    )
