"""Training entry point - replaced workflow.

BEFORE (removed): this script generated synthetic records inside the
"real" workflow, asked the xAI/Grok API for biological criteria, turned
those criteria into labels with hardcoded rules, trained a Random Forest
*classifier* on those labels, and reported accuracy against labels created
by the same rules. None of that is evidence of readiness learning.

NOW: two explicit modes.

  Mode A - demonstration (synthetic)      `python model_train.py --mode demo`
      Reproducible simulated species/varieties, known simulated readiness
      events, plant-grouped validation, artifacts labelled SYNTHETIC DEMO.

  Mode B - real data                     `python model_train.py --mode real`
      Imports the Super Orange CSV, runs data-quality checks and growth
      analysis, then reports - honestly - that no verified readiness events
      exist, so NO readiness countdown model is trained. No synthetic
      fallback is used for real plants.

The supervised learning target is always:

      days_remaining = readiness_observed_date - observation_date

...taken only from recorded readiness events, never from generated rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Absolute imports: this file is executed as a top-level script
# (`python model_train.py`), so it imports from the package by name.
from plantreadiness import config
from plantreadiness import csv_import
from plantreadiness import db as dbm
from plantreadiness import model as mdl
from plantreadiness import synthetic


# --------------------------------------------------------------------------- #
# Mode A: synthetic demonstration
# --------------------------------------------------------------------------- #


def _train_and_save_scope(rows: list[dict], data_source: str,
                          models_dir: Path,
                          scope_species: str | None,
                          scope_variety: str | None,
                          source_label: str) -> dict | None:
    """Train one scoped model (or the global one when both scopes are None)."""
    df = mdl.build_training_frame(rows)
    if df.empty:
        print(f"  scope {scope_species}/{scope_variety}: no labelled rows - "
              f"skipped")
        return None
    try:
        artifact = mdl.train_countdown_model(
            df, data_source=data_source,
            species=scope_species, variety=scope_variety,
            source_label=source_label)
    except mdl.InsufficientDataError as exc:
        print(f"  scope {scope_species}/{scope_variety}: {exc}")
        return None
    mdl.retire_artifacts(models_dir, data_source, scope_species, scope_variety)
    saved = mdl.save_artifact(artifact, models_dir)
    meta = artifact["metadata"]
    print(f"  scope {scope_species}/{scope_variety}: trained "
          f"{meta['model_name']} on {meta['n_independent_plants']} plants / "
          f"{meta['n_labelled_readiness_events']} readiness events, "
          f"MAE={meta['validation_metrics']['mae_days']} d "
          f"(baseline {meta['validation_metrics']['baseline_mean_mae_days']} d)"
          f" -> {Path(saved['file']).name}")
    return {"artifact": artifact, "saved": saved}


def _scope_pairs(rows: list[dict]) -> list[tuple[str | None, str | None]]:
    """Species/variety scopes seen in the rows, plus the global scope."""
    pairs = sorted({(r["species"], r["variety"]) for r in rows})
    return [(sp, var) for sp, var in pairs] + [(None, None)]


def run_demo_training(db_path: Path, models_dir: Path,
                      seed: int = 42,
                      plants_per_variety: int = 8,
                      holdout_per_variety: int = 4) -> dict:
    """Generate synthetic cohorts, train the demo models, keep a holdout set.

    Cohorts:
      * ``initial`` - training + grouped-CV validation
      * ``holdout`` - NEVER trained on; reserved for old-vs-new comparison
    """
    print("=" * 68)
    print(" SYNTHETIC DEMO MODE - SIMULATED DATA ONLY")
    print(f" {config.SYNTHETIC_BANNER}")
    print("=" * 68)

    cfg = synthetic.DemoConfig(seed=seed,
                               plants_per_species_variety=plants_per_variety)
    conn = dbm.init_db(db_path)
    try:
        initial = synthetic.generate_demo_cohort(
            cfg, label="initial", ref_prefix="SYN")
        holdout_cfg = synthetic.DemoConfig(
            seed=seed + 1000,
            plants_per_species_variety=holdout_per_variety,
            extra_retrain_plants_per_species_variety=holdout_per_variety,
        )
        holdout = synthetic.generate_demo_cohort(
            holdout_cfg, label="holdout", ref_prefix="SYNH")

        # load_cohort_into_db stamps each plant's cohort label
        stats_init = synthetic.load_cohort_into_db(conn, initial)
        stats_hold = synthetic.load_cohort_into_db(conn, holdout)

        print(f"\nGenerated SYNTHETIC initial cohort: {stats_init}")
        print(f"Generated SYNTHETIC holdout cohort:  {stats_hold}")

        # --- train scoped + global models ---------------------------------
        rows = dbm.readiness_training_rows(
            conn, data_source="synthetic", cohorts=["initial"])
        print(f"\nLabelled readiness rows (initial cohort): {len(rows)} "
              f"from {len({r['plant_id'] for r in rows})} plants")

        results = []
        print("Training SYNTHETIC DEMO models:")
        for sp, var in _scope_pairs(rows):
            if sp is None:
                scope_rows = rows
            else:
                scope_rows = [r for r in rows
                              if r["species"] == sp and r["variety"] == var]
            res = _train_and_save_scope(
                scope_rows, "synthetic", models_dir, sp, var,
                "synthetic demo data")
            if res:
                results.append(res)

        if not results:
            print("\nNo demo model could be trained.")
            return {"trained": False, "reason": "no scope met requirements"}

        global_res = next(
            (r for r in reversed(results)
             if r["artifact"]["metadata"]["scope_species"] is None),
            results[-1])
        meta = global_res["artifact"]["metadata"]
        print("\n--- SYNTHETIC DEMO model metadata (global model) ---")
        print(json.dumps({k: v for k, v in meta.items()
                          if k != "training_plant_ids"}, indent=2))
        print("\nNOTE: these error figures describe SIMULATED data only and "
              "must never be quoted as real-world accuracy.")
        return {"trained": True,
                "n_models": len(results),
                "artifact": global_res["artifact"],
                "saved": global_res["saved"]}
    finally:
        conn.close()


def run_demo_retrain(db_path: Path, models_dir: Path,
                     seed: int = 42) -> dict:
    """Add a fresh synthetic cohort, retrain, compare old vs new models.

    Both models are evaluated on the SAME held-out synthetic plants
    (cohort='holdout'), which neither model trained on.
    """
    print("=" * 68)
    print(" SYNTHETIC DEMO RETRAIN - SIMULATED DATA ONLY")
    print(f" {config.SYNTHETIC_BANNER}")
    print("=" * 68)

    conn = dbm.init_db(db_path)
    try:
        cfg = synthetic.DemoConfig(seed=seed + 7)
        retrain = synthetic.generate_demo_cohort(cfg, label="retrain",
                                                 ref_prefix="SYNR")
        stats = synthetic.load_cohort_into_db(conn, retrain)
        print(f"\nAdded SYNTHETIC retrain cohort: {stats}")

        # --- deployed models (per scope) ------------------------------------
        synth_entries = [e for e in mdl.list_artifacts(models_dir, "synthetic")
                         if e.get("active", True)]
        if not synth_entries:
            print("No deployed demo model found - run --mode demo first.")
            return {"compared": False, "reason": "no deployed model"}

        holdout_rows = dbm.readiness_training_rows(
            conn, data_source="synthetic", cohorts=["holdout"])
        holdout_df = mdl.build_training_frame(holdout_rows)
        if holdout_df.empty:
            print("Holdout set is empty - evidence insufficient, "
                  "keeping current model.")
            return {"compared": False, "reason": "empty holdout"}

        # candidate rows: initial + retrain, NEVER holdout
        all_candidate_rows = dbm.readiness_training_rows(
            conn, data_source="synthetic", exclude_cohorts=["holdout"])

        comparisons = []
        for entry in synth_entries:
            meta = entry["metadata"]
            sp, var = meta.get("scope_species"), meta.get("scope_variety")
            current = mdl.load_artifact(entry, models_dir, "synthetic")

            if sp is None:
                crows, hrows = all_candidate_rows, holdout_rows
            else:
                crows = [r for r in all_candidate_rows
                         if r["species"] == sp and r["variety"] == var]
                hrows = [r for r in holdout_rows
                         if r["species"] == sp and r["variety"] == var]
            cdf = mdl.build_training_frame(crows)
            hdf = mdl.build_training_frame(hrows)
            if hdf.empty or cdf.empty:
                print(f"scope {sp}/{var}: empty candidate or holdout - "
                      f"keeping current model.")
                comparisons.append({"scope": [sp, var],
                                    "compared": False,
                                    "reason": "empty candidate/holdout"})
                continue

            try:
                candidate = mdl.train_countdown_model(
                    cdf, data_source="synthetic",
                    species=sp, variety=var,
                    source_label="synthetic retrain data")
            except mdl.InsufficientDataError as exc:
                print(f"scope {sp}/{var}: candidate not trainable: {exc}")
                comparisons.append({"scope": [sp, var],
                                    "compared": False, "reason": str(exc)})
                continue

            # leakage guard: no holdout plant in candidate training
            overlap = (set(candidate["metadata"]["training_plant_ids"])
                       & set(hdf["plant_id"].unique()))
            if overlap:
                raise RuntimeError(
                    f"Leakage: holdout plants in training: {overlap}")

            current_mae = mdl.evaluate_on_holdout(current, hdf)
            candidate_mae = mdl.evaluate_on_holdout(candidate, hdf)
            decision = mdl.decide_update(current, candidate,
                                         current_mae, candidate_mae)

            print(f"\n--- scope {sp}/{var}: old vs new on the SAME held-out "
                  f"synthetic set ---")
            print(f"holdout plants        : {hdf['plant_id'].nunique()}")
            print(f"holdout rows          : {len(hdf)}")
            print(f"current  model MAE    : {current_mae:.3f} days")
            print(f"candidate model MAE   : {candidate_mae:.3f} days")
            print(f"decision              : "
                  f"{'REPLACE' if decision['replace'] else 'KEEP CURRENT'} - "
                  f"{decision['reason']}")

            promoted = False
            if decision["replace"]:
                cand_meta = candidate["metadata"]
                cand_meta["retrain_comparison"] = {
                    "holdout_plants": sorted(
                        int(p) for p in hdf["plant_id"].unique()),
                    "current_mae_days": round(current_mae, 4),
                    "candidate_mae_days": round(candidate_mae, 4),
                    "decision": decision,
                }
                mdl.retire_artifacts(models_dir, "synthetic",
                                     cand_meta.get("scope_species"),
                                     cand_meta.get("scope_variety"))
                saved = mdl.save_artifact(candidate, models_dir)
                print(f"Promoted new SYNTHETIC demo model: {saved['file']}")
                promoted = True
            else:
                print("Current model kept; candidate discarded (not better "
                      "on the shared holdout).")

            comparisons.append({"scope": [sp, var], "compared": True,
                                "promoted": promoted,
                                "current_mae_days": current_mae,
                                "candidate_mae_days": candidate_mae,
                                "decision": decision})

        any_promoted = any(c.get("promoted") for c in comparisons)
        any_compared = any(c.get("compared") for c in comparisons)
        return {"compared": any_compared, "promoted": any_promoted,
                "comparisons": comparisons}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Mode B: real data
# --------------------------------------------------------------------------- #


def run_real_training(db_path: Path, models_dir: Path,
                      csv_path: Path) -> dict:
    """Import real CSV, analyse quality/growth, attempt (and usually refuse)
    readiness-model training because no verified readiness events exist."""
    print("=" * 68)
    print(" REAL DATA MODE - Super Orange measurements")
    print("=" * 68)

    conn = dbm.init_db(db_path)
    try:
        # --- import --------------------------------------------------------
        if csv_path.exists():
            report = csv_import.import_csv(
                conn, csv_path, species=config.DEFAULT_SPECIES,
                date_started=None)
            print(f"\nCSV import report ({csv_path.name}):")
            print(json.dumps({k: v for k, v in report.items() if k != 'issues'},
                             indent=2))
            if report["issues"]:
                print(f"row issues ({len(report['issues'])}):")
                for issue in report["issues"][:10]:
                    print(f"  line {issue['line']}: {issue['problem']}")
        else:
            print(f"\nCSV not found at {csv_path} - skipping import.")

        # --- quality checks + growth analysis ------------------------------
        analysis = csv_import.analyse_real_data(
            conn, species=config.DEFAULT_SPECIES, data_source="real")
        print("\n--- data-quality & growth analysis (real) ---")
        print(f"plants                : {analysis['n_plants']}")
        print(f"observations           : {analysis['n_observations']}")
        print(f"plants with readiness  : "
              f"{analysis['n_plants_with_readiness_event']}")
        print(f"mean growth rate       : "
              f"{analysis['overall_mean_growth_rate_cm_per_day']} cm/day")
        if analysis["quality_issues"]:
            print("quality issues:")
            for issue in analysis["quality_issues"]:
                print(f"  plant {issue['plant']}: {issue['problem']}")
        else:
            print("quality issues        : none found")
        print("\nper-plant growth trends:")
        for p in analysis["per_plant"]:
            print(f"  plant {p['plant']:>3}: n={p['n_observations']:>3} "
                  f"height {p['height_first_cm']} -> {p['height_last_cm']} cm, "
                  f"mean rate {p['mean_growth_rate_cm_per_day']} cm/day, "
                  f"linear slope {p['linear_slope_cm_per_day']} cm/day, "
                  f"readiness events={p['n_readiness_events']}")

        # --- readiness model attempt (expected to refuse) -------------------
        rows = dbm.readiness_training_rows(
            conn, data_source="real", species=config.DEFAULT_SPECIES)
        df = mdl.build_training_frame(rows)
        try:
            artifact = mdl.train_countdown_model(
                df, data_source="real",
                source_label="real readiness-event data")
        except mdl.InsufficientDataError as exc:
            print(f"\n{config.UNAVAILABLE_MESSAGE}")
            print(f"detail: {exc}")
            print("\nWhat this system CAN do with the current real data:")
            print("  - preserve and check measurements")
            print("  - show growth trends per plant")
            print("  - collect readiness assessments and 14-day feedback")
            print("It will NOT train a readiness countdown until verified "
                  "readiness-event labels exist.")
            print("No synthetic model is used as a fallback for real plants "
                  "(see config.NO_SYNTHETIC_FALLBACK_NOTE).")
            return {"trained": False,
                    "message": config.UNAVAILABLE_MESSAGE,
                    "analysis": analysis}

        saved = mdl.save_artifact(artifact, models_dir)
        print(f"\nTrained real readiness model: {saved['file']}")
        return {"trained": True, "saved": saved, "analysis": analysis}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plant readiness countdown training (two explicit modes).")
    parser.add_argument("--mode", choices=["demo", "real"], required=True,
                        help="demo = synthetic demonstration; "
                             "real = Super Orange CSV import + analysis")
    parser.add_argument("--db", default=None,
                        help="SQLite database path (default: "
                             "plant_readiness.db for real mode, "
                             "plant_readiness_demo.db for demo mode)")
    parser.add_argument("--models-dir", default=None,
                        help="model artefact directory (default: models/ for "
                             "real mode, models_demo/ for demo mode)")
    parser.add_argument("--csv", default=str(config.DEFAULT_REAL_CSV),
                        help="real CSV to import (real mode)")
    parser.add_argument("--seed", type=int, default=42,
                        help="synthetic generator seed (demo mode)")
    parser.add_argument("--retrain", action="store_true",
                        help="demo mode: add a new synthetic cohort, retrain "
                             "and compare with the deployed model on holdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Synthetic runs get their own scratch database and models directory, so a
    # demo can never write synthetic plants into the real database or churn
    # the committed models/ artefacts.
    demo = args.mode == "demo"
    db_path = Path(args.db) if args.db else (
        config.DEMO_DB_PATH if demo else config.DEFAULT_DB_PATH)
    models_dir = Path(args.models_dir) if args.models_dir else (
        config.DEMO_MODELS_DIR if demo else config.MODELS_DIR)

    if args.mode == "demo":
        if args.retrain:
            result = run_demo_retrain(db_path, models_dir, seed=args.seed)
        else:
            result = run_demo_training(db_path, models_dir, seed=args.seed)
        return 0 if result.get("trained") or result.get("compared") else 1

    result = run_real_training(db_path, models_dir, Path(args.csv))
    # Real mode "succeeds" even when no model could be trained: an honest
    # refusal is the correct outcome until readiness labels exist.
    return 0


if __name__ == "__main__":
    sys.exit(main())
