"""
cli.py — Command-Line Interface for the Plant Readiness System

Supports:
  python cli.py seed [--csv data/super_orange_growth_data.csv]
  python cli.py measure <image_path> [--rod-cm 12.0]
  python cli.py train [--species "Sunflower(SuperOrange)"]
  python cli.py predict --species "Sunflower(SuperOrange)" [--height 3.5] [--age 15]
  python cli.py evaluate
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import db
import ml
import vision


def main() -> None:
    parser = argparse.ArgumentParser(description="Plant Readiness CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Seed
    p_seed = subparsers.add_parser("seed", help="Seed database from historical CSV")
    p_seed.add_argument("--csv", default="data/super_orange_growth_data.csv", help="Path to CSV")
    p_seed.add_argument("--species", default="Sunflower(SuperOrange)", help="Species name")
    p_seed.add_argument("--variety", default="SuperOrange", help="Variety name")

    # Measure
    p_meas = subparsers.add_parser("measure", help="Measure plant from an image file")
    p_meas.add_argument("image_path", help="Path to plant image")
    p_meas.add_argument("--rod-cm", type=float, default=12.0, help="Reference rod length in cm")
    p_meas.add_argument("--save-obs", action="store_true", help="Save measurement as observation")
    p_meas.add_argument("--plant-id", default="plant_live", help="Plant ID for saved observation")

    # Train
    p_train = subparsers.add_parser("train", help="Train and validate ML countdown model")
    p_train.add_argument("--species", default=None, help="Species filter (optional)")

    # Predict
    p_pred = subparsers.add_parser("predict", help="Predict transplant readiness days remaining")
    p_pred.add_argument("--species", default="Sunflower(SuperOrange)", help="Species name")
    p_pred.add_argument("--height", type=float, default=None, help="Current plant height in cm")
    p_pred.add_argument("--age", type=int, default=None, help="Current age in days")
    p_pred.add_argument("--growth-rate", type=float, default=None, help="Growth rate cm/day")

    # Evaluate
    subparsers.add_parser("evaluate", help="Evaluate historical predictions against actual feedback")

    args = parser.parse_args()

    if args.command == "seed":
        count = db.seed_from_csv(db.DEFAULT_DB_PATH, args.csv, args.species, args.variety)
        print(f"Successfully seeded {count} observations for {args.species}.")

    elif args.command == "measure":
        res = vision.measure_image(args.image_path, rod_length_cm=args.rod_cm)
        print(f"--- Plant Measurement: {args.image_path} ---")
        print(f"Height: {res.height_cm:.2f} cm")
        print(f"Stem width: {res.stem_width_cm or 0:.3f} cm")
        print(f"Leaf count: {res.leaf_count}")
        print(f"Calibrated: {res.calibrated} ({res.pixels_per_cm:.2f} px/cm)")

        if args.save_obs:
            db.record_observation(
                db.DEFAULT_DB_PATH,
                plant_id=args.plant_id,
                observation_date=date.today(),
                height_cm=res.height_cm,
                stem_width_cm=res.stem_width_cm,
                leaf_count=res.leaf_count,
                source="vision_cli",
            )
            print(f"Saved observation for {args.plant_id}.")

    elif args.command == "train":
        print(f"Training models for species: {args.species or 'All'}...")
        res = ml.train_countdown_model(species=args.species)
        if res["validated"]:
            print(f"Model validated successfully! Best algorithm: {res['best_model_name']}")
            print(f"Cross-validation MAE: {res['best_mae']} days (Mean Baseline: {res['baseline_mae']} days)")
            print(f"Trained on {res['n_plants']} independent plants ({res['n_samples']} samples).")
        else:
            print(f"Model not validated: {res.get('reason')}")

    elif args.command == "predict":
        # Check if a validated model can be trained/used
        model_res = ml.train_countdown_model(species=args.species)
        pipe = model_res.get("pipeline") if model_res.get("validated") else None

        res = ml.predict_species_readiness(
            species=args.species,
            current_height_cm=args.height,
            current_age_days=args.age,
            current_growth_rate=args.growth_rate,
            trained_model=pipe,
        )
        print(f"--- Prediction: {args.species} ---")
        if res.predicted_days_remaining is not None:
            print(f"Estimated Days Remaining: {res.predicted_days_remaining} days")
            print(f"Estimated Readiness Date: {res.predicted_readiness_date}")
            print(f"Method: {res.method} (Confidence: {res.confidence})")
        else:
            print(f"Prediction Unavailable: {res.summary_message}")
            print(f"Method: {res.method}")

    elif args.command == "evaluate":
        res = ml.evaluate_predictions(db.DEFAULT_DB_PATH)
        print("--- Historical Prediction Evaluation ---")
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
