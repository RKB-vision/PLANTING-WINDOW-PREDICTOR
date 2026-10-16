"""Prediction entry point - replaced workflow.

BEFORE (removed): this script loaded .pkl files trained on Grok/rule labels
and printed "PEAK READY / Too Early / Too Late" verdicts for columns
(root cohesion, substrate, ...) that do not exist in the real dataset.

NOW: predicts **days remaining until the transplant-ready stage** using the
species-aware fallback order:

  1. validated species/variety model (real labelled data),
  2. broader real model for the species (with uncertainty + limitations),
  3. SYNTHETIC DEMO prediction - only with --allow-synthetic, always labelled,
  4. otherwise: "Readiness prediction unavailable: ..."

Synthetic-trained models are NEVER used for a real plant unless the user
explicitly enables them, and any synthetic output carries a persistent
SYNTHETIC DEMO indicator.

Examples
--------
Predict for a registered plant (uses its latest observation):

    python prediction.py --plant 3

Ad-hoc observation:

    python prediction.py --species "Super Orange" --variety Early \
        --age-days 20 --height-cm 4.2 --growth-rate 0.3

Explicitly enable the synthetic demonstration model:

    python prediction.py --plant 3 --allow-synthetic
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from plantreadiness import config
from plantreadiness import db as dbm
from plantreadiness import services as svc
from plantreadiness.repository import SQLiteRepository


# --------------------------------------------------------------------------- #
# Core prediction (thin wrappers over the shared service layer)
# --------------------------------------------------------------------------- #


def predict_for_plant(conn, plant_id: int, models_dir: Path,
                      allow_synthetic: bool = False,
                      allow_provisional_baseline: bool = False) -> dict:
    """Species-aware prediction for a registered plant's latest observation."""
    return svc.predict_for_plant(SQLiteRepository(conn), plant_id, models_dir,
                                 allow_synthetic=allow_synthetic,
                                 allow_provisional_baseline=(
                                     allow_provisional_baseline))


def predict_ad_hoc(species: str, variety: str | None,
                   observation: dict, models_dir: Path,
                   allow_synthetic: bool = False) -> dict:
    """Prediction from values given on the command line."""
    return svc.predict_ad_hoc(species, variety, observation, models_dir,
                              allow_synthetic=allow_synthetic)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def format_result(result: dict) -> str:
    lines = []
    synthetic_used = bool(result.get("synthetic"))
    if synthetic_used:
        lines.append("=" * 68)
        lines.append(f" {config.SYNTHETIC_BANNER}")
        lines.append("=" * 68)

    if "plant_id" in result:
        lines.append(f"Plant #{result['plant_id']}: "
                     f"{result.get('species')}"
                     f"{' / ' + result['variety'] if result.get('variety') else ''}"
                     f" (data_source={result.get('plant_data_source')})")
    if result.get("observation_date"):
        lines.append(f"Using observation from {result['observation_date']} "
                     f"(age={result['observation'].get('age_days')} d, "
                     f"height={result['observation'].get('height_cm')} cm, "
                     f"rate={result['observation'].get('growth_rate_cm_per_day')} "
                     f"cm/day)")

    if not result.get("available"):
        lines.append("")
        lines.append(result.get("message", config.UNAVAILABLE_MESSAGE))
        counts = result.get("readiness_event_counts")
        if counts:
            lines.append(
                f"Usable real readiness events for "
                f"'{counts['species']}'"
                f"{' / ' + counts['variety'] if counts.get('variety') else ''}: "
                f"{counts['readiness_events']} "
                f"(from {counts['independent_plants']} independent plants).")
        lines.append("Status: collecting data - not making a prediction.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"Model level        : {result['level']}")
    lines.append(f"Prediction source  : "
                 f"{'SYNTHETIC DEMO' if synthetic_used else result.get('data_source')}")
    lines.append(result.get("message", ""))
    days = result.get("days_remaining")
    if result.get("past_readiness") or (
            isinstance(days, (int, float)) and days <= 0):
        lines.append("Transplant-ready stage : Plantable now "
                     "(readiness estimated to have arrived)")
    else:
        lines.append(f"Estimated days remaining until transplant-ready stage: "
                     f"{days}")
    if result.get("predicted_readiness_date"):
        lines.append(f"Estimated readiness date : {result['predicted_readiness_date']} "
                     f"(calculated from observation on {result.get('observation_date')})")
    if result.get("past_readiness"):
        lines.append(f"  PAST READINESS WARNING: {result.get('status_note')}")
    if result.get("mae_days") is not None:
        lines.append(f"Validation MAE     : +/- {result['mae_days']} days "
                     f"(std {result.get('mae_days_std')}) on grouped CV")
        if result.get("baseline_mae_days") is not None:
            lines.append(f"Mean-baseline MAE  : {result['baseline_mae_days']} days")
    lines.append(f"Validated          : {result.get('validated')}")
    lines.append(f"Trained on         : {result.get('n_independent_plants')} "
                 f"independent plants, "
                 f"{result.get('n_labelled_readiness_events')} labelled "
                 f"readiness events")
    if result.get("limitations"):
        lines.append(f"Limitations        : {result['limitations']}")
    if result.get("cross_source_warning"):
        lines.append(f"WARNING            : {result['cross_source_warning']}")
    counts = result.get("readiness_event_counts")
    if counts:
        lines.append(f"Real readiness events for this species: "
                     f"{counts['readiness_events']} "
                     f"({counts['independent_plants']} plants)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Predict days remaining until transplant readiness "
                    "(species-aware, with explicit fallback order).")
    parser.add_argument("--plant", type=int,
                        help="plant_id whose latest observation is used")
    parser.add_argument("--species",
                        help="species for an ad-hoc prediction")
    parser.add_argument("--variety",
                        help="variety for an ad-hoc prediction")
    parser.add_argument("--age-days", type=float)
    parser.add_argument("--height-cm", type=float)
    parser.add_argument("--growth-rate", type=float,
                        help="growth_rate_cm_per_day")
    parser.add_argument("--stem-width-cm", type=float)
    parser.add_argument("--true-leaves", type=int)
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--models-dir", default=str(config.MODELS_DIR))
    parser.add_argument("--allow-synthetic", action="store_true",
                        help="explicitly allow the SYNTHETIC DEMO model "
                             "(never silent, always labelled)")
    parser.add_argument("--json", action="store_true",
                        help="print the raw result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    models_dir = Path(args.models_dir)

    conn = dbm.init_db(Path(args.db))
    try:
        if args.plant is not None:
            result = predict_for_plant(conn, args.plant, models_dir,
                                       allow_synthetic=args.allow_synthetic)
        elif args.species:
            observation = {
                "age_days": args.age_days,
                "height_cm": args.height_cm,
                "growth_rate_cm_per_day": args.growth_rate,
                "stem_width_cm": args.stem_width_cm,
                "visible_true_leaf_count": args.true_leaves,
            }
            if observation["age_days"] is None and \
                    observation["height_cm"] is None:
                print("Provide at least --age-days or --height-cm.",
                      file=sys.stderr)
                return 2
            result = predict_ad_hoc(args.species, args.variety, observation,
                                    models_dir,
                                    allow_synthetic=args.allow_synthetic)
            counts = dbm.readiness_event_counts(
                conn, args.species, args.variety, data_source="real")
            result["readiness_event_counts"] = counts
        else:
            build_parser().print_help()
            print("\nEither --plant or --species is required.", file=sys.stderr)
            return 2

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(format_result(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
