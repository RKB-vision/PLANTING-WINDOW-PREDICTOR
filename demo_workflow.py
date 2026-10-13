"""End-to-end SYNTHETIC DEMONSTRATION workflow (Mode A, scripted).

Runs the complete loop required by the task, entirely on simulated data:

  1. build a synthetic database (if needed) and train the demo models,
  2. register a plant,
  3. record its measurements,
  4. generate a countdown prediction (explicitly enabled, labelled),
  5. record the transplant and the due date (transplant + 14 days),
  6. submit the 14-day feedback outcome after the interval,
  7. retrain the demonstration model with a new cohort,
  8. compare old vs new models on held-out synthetic examples.

Every output is prefixed with a SYNTHETIC DEMO marker. Nothing here is a
botanical finding - the generator's rules are documented assumptions.

Run:

    python demo_workflow.py [--db demo.db] [--models-dir models_demo]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from plantreadiness import config
from plantreadiness import db as dbm
from plantreadiness import model as mdl
from plantreadiness import synthetic


def step(title: str) -> None:
    print("\n" + "=" * 68)
    print(f" {title}")
    print("=" * 68)


def run(db_path: Path, models_dir: Path, seed: int = 42) -> dict:
    summary: dict = {"steps": []}
    conn = dbm.init_db(db_path)
    try:
        # ------------------------------------------------------------------
        step("STEP 0 - SYNTHETIC DATA GENERATION + DEMO MODEL TRAINING")
        print(config.SYNTHETIC_BANNER)
        import model_train  # reuse Mode A exactly as `--mode demo` does
        existing = mdl.list_artifacts(models_dir, "synthetic")
        if not existing:
            train_res = model_train.run_demo_training(
                db_path, models_dir, seed=seed)
            summary["steps"].append(
                {"step": "train_initial", "ok": bool(train_res.get("trained"))})
        else:
            print(f"Existing synthetic models found ({len(existing)}) - "
                  f"reusing them.")
            summary["steps"].append({"step": "train_initial",
                                     "ok": True, "reused": True})

        # ------------------------------------------------------------------
        step("STEP 1 - REGISTER A SYNTHETIC PLANT")
        pid = dbm.register_plant(
            conn, species="Super Orange", variety="Early",
            date_started="2083-04-24", data_source="synthetic",
            growing_conditions="simulated_demo_conditions",
            external_ref="DEMO-NEW-001", cohort="demo_workflow")
        print(f"[SYNTHETIC DEMO] registered plant #{pid} "
              f"(Super Orange / Early)")
        summary["steps"].append({"step": "register", "ok": True,
                                 "plant_id": pid})

        # ------------------------------------------------------------------
        step("STEP 2 - RECORD MEASUREMENTS")
        demo_obs = [
            ("2083-05-14", 20, 4.4, 0.45),
            ("2083-05-19", 25, 5.6, 0.28),
            ("2083-05-24", 30, 6.6, 0.24),
        ]
        for d, age, h, rate in demo_obs:
            oid = dbm.add_observation(
                conn, pid, d, age_days=age, height_cm=h,
                growth_rate_cm_per_day=rate, data_source="synthetic")
            print(f"[SYNTHETIC DEMO] observation #{oid}: {d} age={age}d "
                  f"height={h}cm rate={rate}cm/day")
        summary["steps"].append({"step": "observe", "ok": True,
                                 "n": len(demo_obs)})

        # ------------------------------------------------------------------
        step("STEP 3 - COUNTDOWN PREDICTION (explicit synthetic opt-in)")
        from prediction import predict_for_plant, format_result
        result = predict_for_plant(conn, pid, models_dir,
                                   allow_synthetic=True)
        print(format_result(result))
        summary["steps"].append(
            {"step": "predict", "ok": bool(result.get("available")),
             "level": result.get("level"),
             "days_remaining": result.get("days_remaining"),
             "synthetic": bool(result.get("synthetic"))})
        if not result.get("available"):
            print("No demo model available; aborting remaining steps.")
            return summary

        # ------------------------------------------------------------------
        step("STEP 4 - RECORD TRANSPLANT + FEEDBACK DUE DATE")
        rec = dbm.record_transplant(
            conn, pid, "2083-05-26", height_at_transplant_cm=7.0,
            data_source="synthetic")
        due = dbm.parse_iso_date(rec["feedback_due_date"])
        print(f"[SYNTHETIC DEMO] transplant on {rec['transplant_date']}; "
              f"feedback record #{rec['feedback_id']} DUE "
              f"{rec['feedback_due_date']} "
              f"(+{config.FEEDBACK_WINDOW_DAYS} days)")
        summary["steps"].append(
            {"step": "transplant", "ok": True,
             "due_date": rec["feedback_due_date"]})

        # ------------------------------------------------------------------
        step("STEP 5 - SUBMIT 14-DAY FEEDBACK AFTER THE INTERVAL")
        submitted_on = due.isoformat()
        print(f"[SYNTHETIC DEMO] simulated interval elapsed; submitting on "
              f"{submitted_on}")
        out = dbm.submit_feedback(
            conn, rec["feedback_id"], feedback_submitted_date=submitted_on,
            survived_after_14d=True, height_after_14d_cm=8.2,
            visible_condition="healthy",
            user_notes="SIMULATED outcome (synthetic demo).")
        print(f"[SYNTHETIC DEMO] outcome saved: survived="
              f"{bool(out['survived_after_14d'])} "
              f"growth={out['growth_after_14d_cm']}cm "
              f"condition={out['visible_condition']}")
        print("Reminder: this outcome does NOT create a readiness label.")
        summary["steps"].append(
            {"step": "feedback", "ok": True,
             "growth_after_14d_cm": out["growth_after_14d_cm"]})

        # demonstrate the dead-plant path too (missing measurements kept null)
        pid2 = dbm.register_plant(
            conn, species="Super Orange", variety="Early",
            date_started="2083-04-24", data_source="synthetic",
            external_ref="DEMO-NEW-002", cohort="demo_workflow")
        dbm.add_observation(conn, pid2, "2083-05-24", age_days=30,
                            height_cm=6.5, growth_rate_cm_per_day=0.2,
                            data_source="synthetic")
        rec2 = dbm.record_transplant(conn, pid2, "2083-05-26",
                                     height_at_transplant_cm=6.9,
                                     data_source="synthetic")
        out2 = dbm.submit_feedback(
            conn, rec2["feedback_id"],
            feedback_submitted_date="2083-06-09",
            survived_after_14d=False, height_after_14d_cm=None,
            visible_condition="dead",
            user_notes="SIMULATED mortality (synthetic demo).")
        assert out2["growth_after_14d_cm"] is None, \
            "growth must stay missing for a dead plant"
        print(f"[SYNTHETIC DEMO] second plant #{pid2}: died, survival=0, "
              f"growth left missing (no invented measurement)")

        # ------------------------------------------------------------------
        step("STEP 6 - RETRAIN + COMPARE OLD vs NEW ON HELD-OUT EXAMPLES")
        retrain_res = model_train.run_demo_retrain(db_path, models_dir,
                                                   seed=seed)
        summary["steps"].append(
            {"step": "retrain_compare",
             "ok": bool(retrain_res.get("compared")),
             "promoted": bool(retrain_res.get("promoted")),
             "comparisons": retrain_res.get("comparisons")})

        # ------------------------------------------------------------------
        step("DEMONSTRATION COMPLETE")
        print(config.SYNTHETIC_BANNER)
        print("All records above are SIMULATED. The countdown model, its MAE "
              "and the feedback outcomes demonstrate software behaviour only.")
        ok = all(s.get("ok") for s in summary["steps"])
        summary["ok"] = ok
        print(f"Overall: {'ALL STEPS OK' if ok else 'SOME STEPS FAILED'}")
        return summary
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scripted SYNTHETIC DEMO end-to-end workflow.")
    parser.add_argument("--db", default="demo_workflow.db")
    parser.add_argument("--models-dir", default="models_demo")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    summary = run(Path(args.db), Path(args.models_dir), seed=args.seed)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
