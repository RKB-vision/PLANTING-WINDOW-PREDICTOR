"""Fourteen-day feedback workflow - command-line interface.

Implements the user workflow of task section 5:

  1. register or select a plant
  2. record its current measurements
  3. view a prediction (if a suitable model exists)
  4. record the transplant date
  5. receive a record showing feedback due 14 days later
  6. submit the 14-day outcome later

Reminders (including overdue feedback) are printed every time the program
opens - no cloud service or background scheduler is required.

Every command that touches synthetic data prints a SYNTHETIC DEMO banner.

Examples
--------
    python plant_feedback.py register --species "Super Orange" --variety Early
    python plant_feedback.py observe --plant 1 --height 4.2 --growth-rate 0.3
    python plant_feedback.py predict --plant 1
    python plant_feedback.py transplant --plant 1 --date 2083-05-20 \
        --height 6.4
    python plant_feedback.py due
    python plant_feedback.py submit --feedback 1 \
        --submitted-date 2083-06-03 --survived 1 --height-after 7.5 \
        --condition healthy --notes "looks good"
    python plant_feedback.py history --plant 1
    python plant_feedback.py species-status --species "Super Orange"
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from plantreadiness import config
from plantreadiness import db as dbm
from plantreadiness import services as svc
from plantreadiness.repository import SQLiteRepository


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _banner(conn, plant_id: int | None) -> None:
    """Print the persistent SYNTHETIC DEMO indicator when applicable."""
    if plant_id is None:
        n = conn.execute(
            "SELECT COUNT(*) FROM plants WHERE data_source='synthetic'"
        ).fetchone()[0]
        if n:
            print(f"[SYNTHETIC DEMO] database contains {n} synthetic plant(s) "
                  f"- synthetic rows are labelled, never mixed with real data.")
        return
    plant = dbm.get_plant(conn, plant_id)
    if plant["data_source"] == "synthetic":
        print(f"[SYNTHETIC DEMO] plant #{plant_id} is SIMULATED data.")


def _print_reminders(conn, as_of: date | None = None) -> None:
    """Overdue / due feedback reminders shown whenever the tool is opened."""
    rows = dbm.pending_feedback(conn, as_of=as_of)
    if not rows:
        return
    overdue = [r for r in rows if r["is_overdue"]]
    upcoming = [r for r in rows if not r["is_overdue"]]
    print("-" * 60)
    print(f"14-DAY FEEDBACK REMINDERS ({len(rows)} pending, "
          f"{len(overdue)} OVERDUE)")
    for r in overdue:
        print(f"  ! OVERDUE by {r['days_overdue']}d: plant #{r['plant_id']} "
              f"({r['species']}"
              f"{'/' + r['variety'] if r['variety'] else ''}) "
              f"transplanted {r['transplant_date']}, "
              f"due {r['feedback_due_date']}")
    for r in upcoming:
        print(f"  . pending: plant #{r['plant_id']} "
              f"({r['species']}"
              f"{'/' + r['variety'] if r['variety'] else ''}) "
              f"transplanted {r['transplant_date']}, "
              f"due {r['feedback_due_date']}")
    print("-" * 60)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_register(conn, args) -> int:
    repo = SQLiteRepository(conn)
    pid = svc.register_plant(
        repo, species=args.species, variety=args.variety,
        date_started=args.date_started,
        data_source=args.source,
        growing_conditions=args.conditions,
        external_ref=args.ref)
    _banner(conn, pid)
    print(f"Registered plant #{pid}: {args.species}"
          f"{' / ' + args.variety if args.variety else ''} "
          f"(data_source={args.source})")
    return 0


def cmd_observe(conn, args) -> int:
    _banner(conn, args.plant)
    oid = dbm.add_observation(
        conn, plant_id=args.plant, observation_date=args.date,
        age_days=args.age_days, height_cm=args.height,
        growth_rate_cm_per_day=args.growth_rate,
        stem_width_cm=args.stem_width,
        visible_true_leaf_count=args.true_leaves,
        data_source=args.source)
    print(f"Recorded observation #{oid} for plant #{args.plant} "
          f"on {args.date}.")
    return 0


def cmd_readiness(conn, args) -> int:
    _banner(conn, args.plant)
    repo = SQLiteRepository(conn)
    res = svc.record_readiness_event(
        repo,
        plant_id=args.plant,
        readiness_observed_date=args.date,
        readiness_definition_version=args.version,
        assessment_source=args.source,
        notes=args.notes,
    )
    print(f"Recorded readiness event #{res['readiness_event_id']} for plant #{args.plant} "
          f"({res.get('species')}{'/' + res['variety'] if res.get('variety') else ''}) "
          f"on {args.date} [version: {args.version}, observer: {args.source}].")
    return 0


def cmd_import_camera(conn, args) -> int:
    repo = SQLiteRepository(conn)
    csv_path = Path(args.csv)
    print(f"Importing camera measurements from {csv_path} ...")
    report = svc.import_camera_measurements(
        repo,
        csv_path,
        default_species=args.species,
        default_variety=args.variety,
        auto_register=not args.no_auto_register,
    )
    print("Camera measurement import complete:")
    print(f"  Rows read            : {report['rows_read']}")
    print(f"  Observations inserted: {report['observations_inserted']}")
    print(f"  Duplicates skipped   : {report['observations_skipped_duplicate']}")
    print(f"  Plants auto-created  : {report['plants_registered']}")
    if report["unmatched_plants"]:
        print(f"  Unmatched plant IDs  : {report['unmatched_plants']}")
    if report["issues"]:
        print(f"  Issues encountered   : {len(report['issues'])}")
        for issue in report["issues"][:5]:
            print(f"    - line {issue.get('line')}: {issue.get('problem')}")
    return 0


def cmd_predict(conn, args) -> int:
    from prediction import predict_for_plant, format_result
    _banner(conn, args.plant)
    result = predict_for_plant(conn, args.plant, Path(args.models_dir),
                               allow_synthetic=args.allow_synthetic)
    print(format_result(result))
    return 0


def cmd_transplant(conn, args) -> int:
    _banner(conn, args.plant)
    repo = SQLiteRepository(conn)
    rec = svc.record_transplant(
        repo, plant_id=args.plant, transplant_date=args.date,
        height_at_transplant_cm=args.height, data_source=args.source)
    print(f"Transplant recorded for plant #{args.plant} on "
          f"{rec['transplant_date']}.")
    print(f"14-day feedback record #{rec['feedback_id']} created - "
          f"DUE on {rec['feedback_due_date']} "
          f"(transplant + {config.FEEDBACK_WINDOW_DAYS} days).")
    print("Submit the outcome with:  python plant_feedback.py submit "
          f"--feedback {rec['feedback_id']} --submitted-date YYYY-MM-DD "
          "--survived 0|1 [--height-after CM] "
          "--condition healthy|stressed_wilting|dead")
    return 0


def cmd_due(conn, args) -> int:
    as_of = None
    if getattr(args, "as_of", None):
        as_of = dbm.parse_iso_date(args.as_of, "--as-of")
    rows = SQLiteRepository(conn).pending_feedback(as_of=as_of)
    if not rows:
        print("No pending 14-day feedback.")
        return 0
    for r in rows:
        status = (f"OVERDUE by {r['days_overdue']} days"
                  if r["is_overdue"]
                  else f"due {r['feedback_due_date']}")
        src = (" [SYNTHETIC DEMO]" if r["data_source"] == "synthetic" else "")
        print(f"feedback #{r['feedback_id']}: plant #{r['plant_id']} "
              f"({r['species']}{'/' + r['variety'] if r['variety'] else ''}) "
              f"transplanted {r['transplant_date']} -> {status}{src}")
    return 0


def cmd_submit(conn, args) -> int:
    survived = bool(int(args.survived))
    fb = dbm.get_feedback(conn, args.feedback)
    _banner(conn, fb["plant_id"])
    result = svc.submit_feedback(
        SQLiteRepository(conn),
        feedback_id=args.feedback,
        feedback_submitted_date=args.submitted_date,
        survived_after_14d=survived,
        height_after_14d_cm=args.height_after,
        visible_condition=args.condition,
        user_notes=args.notes)
    print(f"Feedback #{args.feedback} submitted on "
          f"{result['feedback_submitted_date']}.")
    print(f"  survived after 14 days : {bool(result['survived_after_14d'])}")
    print(f"  height at transplant   : "
          f"{result['height_at_transplant_cm']} cm")
    print(f"  height after 14 days   : {result['height_after_14d_cm']} cm")
    if result["growth_after_14d_cm"] is None:
        print("  growth after 14 days   : (not measurable - left missing, "
              "no value invented)")
    else:
        print(f"  growth after 14 days   : "
              f"{result['growth_after_14d_cm']} cm "
              f"(after - at transplant)")
    print(f"  visible condition      : {result['visible_condition']}")
    if result.get("user_notes"):
        print(f"  notes                  : {result['user_notes']}")
    print("\nNote: a 14-day outcome records survival/growth. It does NOT by "
          "itself reveal the exact day readiness began, and it is not "
          "auto-converted into a readiness label.")
    return 0


def cmd_correct(conn, args) -> int:
    """Amend a submitted outcome - the only path that may edit history."""
    survived = None if args.survived is None else bool(int(args.survived))
    result = svc.correct_feedback(
        SQLiteRepository(conn),
        feedback_id=args.feedback,
        feedback_submitted_date=args.submitted_date,
        survived_after_14d=survived,
        height_after_14d_cm=args.height_after,
        visible_condition=args.condition,
        user_notes=args.notes,
        reason=args.reason)
    print(f"Feedback #{args.feedback} corrected (reason: {args.reason}).")
    print(f"  survived after 14 days : {bool(result['survived_after_14d'])}")
    print(f"  height after 14 days   : {result['height_after_14d_cm']} cm")
    print(f"  growth after 14 days   : {result['growth_after_14d_cm']}")
    print(f"  visible condition      : {result['visible_condition']}")
    revisions = SQLiteRepository(conn).feedback_revisions(args.feedback)
    print(f"  audit history          : {len(revisions)} correction(s) "
          "recorded - original values preserved.")
    return 0


def cmd_history(conn, args) -> int:
    _banner(conn, args.plant)
    plant = dbm.get_plant(conn, args.plant)
    print(f"Plant #{plant['plant_id']}: {plant['species']}"
          f"{' / ' + plant['variety'] if plant['variety'] else ''} "
          f"(data_source={plant['data_source']}, "
          f"started={plant['date_started']})")
    obs = dbm.get_observations(conn, args.plant)
    print(f"\nObservations ({len(obs)}):")
    for o in obs:
        print(f"  {o['observation_date']}  age={o['age_days']}  "
              f"h={o['height_cm']}  rate={o['growth_rate_cm_per_day']}  "
              f"[{o['data_source']}]")

    # growth trend summary (latest measurements + simple linear slope)
    valid = [(o["age_days"], o["height_cm"]) for o in obs
             if o["age_days"] is not None and o["height_cm"] is not None]
    if valid:
        latest_age, latest_h = valid[-1]
        rates = [o["growth_rate_cm_per_day"] for o in obs
                 if o["growth_rate_cm_per_day"] is not None]
        slope = None
        if len(valid) >= 2:
            xs = [v[0] for v in valid]
            ys = [v[1] for v in valid]
            n = len(valid)
            mx, my = sum(xs) / n, sum(ys) / n
            denom = sum((x - mx) ** 2 for x in xs)
            if denom > 0:
                slope = round(sum((x - mx) * (y - my)
                                  for x, y in zip(xs, ys)) / denom, 4)
        print("\nGrowth trend:")
        print(f"  latest measurement    : age {latest_age} d, "
              f"height {latest_h} cm "
              f"({obs[-1]['observation_date']})")
        if rates:
            print(f"  mean growth rate      : "
                  f"{round(sum(rates) / len(rates), 4)} cm/day")
        print(f"  height first -> last  : {valid[0][1]} -> {valid[-1][1]} cm")
        print(f"  linear slope          : "
              f"{slope if slope is not None else 'n/a'} cm/day")
    ev = dbm.get_readiness_event(conn, args.plant)
    if ev:
        print(f"\nReadiness event: {ev['readiness_observed_date']} "
              f"(age {ev['readiness_observed_age_days']} d) "
              f"definition={ev['readiness_definition_version']} "
              f"source={ev['assessment_source']} [{ev['data_source']}]")
    else:
        print("\nReadiness event: NONE recorded - target unknown "
              "(no label invented).")
    print("\nTransplant / 14-day feedback:")
    rows = dbm.feedback_history(conn, plant_id=args.plant)
    if not rows:
        print("  none")
    for r in rows:
        state = ("submitted " + r["feedback_submitted_date"]
                 if r["feedback_submitted_date"]
                 else f"pending, due {r['feedback_due_date']}")
        print(f"  transplant {r['transplant_date']} -> {state} "
              f"[{r['data_source']}]")
        if r["feedback_submitted_date"]:
            print(f"    survived={bool(r['survived_after_14d'])} "
                  f"height_after={r['height_after_14d_cm']} "
                  f"growth={r['growth_after_14d_cm']} "
                  f"condition={r['visible_condition']}")
    return 0


def cmd_species_status(conn, args) -> int:
    summary = SQLiteRepository(conn).species_data_summary(
        args.species, args.variety)
    print(f"Species status: {args.species}"
          f"{' / ' + args.variety if args.variety else ''}")
    print(f"  plants by source         : {summary['plants']}")
    print(f"  observations by source   : {summary['observations']}")
    print(f"  REAL readiness events    : "
          f"{summary['real_readiness_events']['readiness_events']} "
          f"from {summary['real_readiness_events']['independent_plants']} "
          f"plants")
    print(f"  SYNTHETIC readiness events: "
          f"{summary['synthetic_readiness_events']['readiness_events']} "
          f"from {summary['synthetic_readiness_events']['independent_plants']} "
          f"plants [SYNTHETIC DEMO]")
    real_events = summary["real_readiness_events"]["readiness_events"]
    if real_events < config.MIN_READINESS_EVENTS:
        print(f"  => {config.UNAVAILABLE_MESSAGE}")
        print(f"     (needs >= {config.MIN_READINESS_EVENTS} verified real "
              f"readiness events from >= "
              f"{config.MIN_INDEPENDENT_PLANTS} independent plants; "
              f"currently collecting data)")
    return 0


def cmd_list(conn, args) -> int:
    rows = dbm.list_plants(conn, species=args.species,
                           data_source=args.source)
    if not rows:
        print("No plants registered.")
        return 0
    for r in rows:
        src = (" [SYNTHETIC DEMO]" if r["data_source"] == "synthetic" else "")
        print(f"#{r['plant_id']:>3}  {r['species']}"
              f"{'/' + r['variety'] if r['variety'] else ''}  "
              f"started={r['date_started']}  "
              f"source={r['data_source']}{src}")
    return 0


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plant registration, measurements, transplant and "
                    "14-day feedback workflow.")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--models-dir", default=str(config.MODELS_DIR))
    # Repeat the common options on every subcommand (with SUPPRESS defaults
    # so they don't clobber values given before the subcommand). This lets
    # users write either `--db X register ...` or `register ... --db X`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS)
    common.add_argument("--models-dir", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", parents=[common],
                       help="register/select a plant")
    p.add_argument("--species", required=True)
    p.add_argument("--variety")
    p.add_argument("--date-started")
    p.add_argument("--source", default="real",
                   choices=list(config.VALID_SOURCES))
    p.add_argument("--conditions", help="growing conditions (free text)")
    p.add_argument("--ref", help="external reference id")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("observe", parents=[common],
                       help="record current measurements")
    p.add_argument("--plant", type=int, required=True)
    p.add_argument("--date", default=None,
                   help="observation date (default: today)")
    p.add_argument("--age-days", type=int)
    p.add_argument("--height", type=float)
    p.add_argument("--growth-rate", type=float)
    p.add_argument("--stem-width", type=float)
    p.add_argument("--true-leaves", type=int)
    p.add_argument("--source", choices=list(config.VALID_SOURCES))
    p.set_defaults(func=cmd_observe)

    p = sub.add_parser("predict", parents=[common],
                       help="view countdown prediction")
    p.add_argument("--plant", type=int, required=True)
    p.add_argument("--allow-synthetic", action="store_true")
    p.set_defaults(func=cmd_predict)

    p = sub.add_parser("transplant", parents=[common],
                       help="record the transplant date")
    p.add_argument("--plant", type=int, required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--height", type=float,
                   help="height at transplant (cm)")
    p.add_argument("--source", choices=list(config.VALID_SOURCES))
    p.set_defaults(func=cmd_transplant)

    p = sub.add_parser("due", parents=[common],
                       help="list pending/overdue 14-day feedback")
    p.add_argument("--as-of", dest="as_of",
                   help="evaluate due/overdue on this date (default: today)")
    p.set_defaults(func=cmd_due)

    p = sub.add_parser("submit", parents=[common],
                       help="submit a 14-day outcome")
    p.add_argument("--feedback", type=int, required=True)
    p.add_argument("--submitted-date", default=None,
                   help="default: today")
    p.add_argument("--survived", required=True, choices=["0", "1"],
                   help="0 = died, 1 = survived")
    p.add_argument("--height-after", type=float,
                   help="height after 14 days (omit if unmeasurable)")
    p.add_argument("--condition", choices=list(config.VISIBLE_CONDITIONS))
    p.add_argument("--notes")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("correct", parents=[common],
                       help="amend a submitted 14-day outcome (audited)")
    p.add_argument("--feedback", type=int, required=True)
    p.add_argument("--reason", required=True,
                   help="why the correction is being made (kept in audit)")
    p.add_argument("--submitted-date", dest="submitted_date")
    p.add_argument("--survived", choices=["0", "1"],
                   help="0 = died, 1 = survived")
    p.add_argument("--height-after", dest="height_after", type=float,
                   help="height after 14 days (omit to keep current)")
    p.add_argument("--condition", choices=list(config.VISIBLE_CONDITIONS))
    p.add_argument("--notes")
    p.set_defaults(func=cmd_correct)

    p = sub.add_parser("history", parents=[common],
                       help="plant history: obs, readiness, feedback")
    p.add_argument("--plant", type=int, required=True)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("species-status", parents=[common],
                       help="usable readiness events per species")
    p.add_argument("--species", required=True)
    p.add_argument("--variety")
    p.set_defaults(func=cmd_species_status)

    p = sub.add_parser("list", parents=[common],
                       help="list registered plants")
    p.add_argument("--species")
    p.add_argument("--source", choices=list(config.VALID_SOURCES))
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("readiness", parents=[common],
                       help="record an observed readiness event (ground truth training label)")
    p.add_argument("--plant", type=int, required=True)
    p.add_argument("--date", help="date readiness was verified (default: today)")
    p.add_argument("--version", required=True,
                   help="readiness definition version (e.g. v1-nursery-visual)")
    p.add_argument("--source", required=True,
                   help="assessment source or observer (e.g. grower-inspection)")
    p.add_argument("--notes", help="notes on visual criteria observed")
    p.set_defaults(func=cmd_readiness)

    p = sub.add_parser("import-camera", parents=[common],
                       help="import daily measurements from ESP32-CAM/OpenCV CSV")
    p.add_argument("--csv", default=str(config.CAMERA_CSV_PATH),
                   help="path to camera CSV "
                        "(default: data/generated/plant_daily_measurements.csv)")
    p.add_argument("--species", default="Super Orange",
                   help="default species if plant must be registered (default: Super Orange)")
    p.add_argument("--variety", help="default variety if plant must be registered")
    p.add_argument("--no-auto-register", action="store_true",
                   help="do not register unknown plants automatically")
    p.set_defaults(func=cmd_import_camera)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # default dates
    if args.command == "observe" and args.date is None:
        args.date = date.today().isoformat()
    if args.command == "readiness" and args.date is None:
        args.date = date.today().isoformat()
    if args.command == "submit" and args.submitted_date is None:
        args.submitted_date = date.today().isoformat()

    conn = dbm.init_db(Path(args.db))
    try:
        # reminders every time the tool opens (section 5.5 / 11)
        if args.command != "due":
            _print_reminders(conn)
        return args.func(conn, args)
    except dbm.DatabaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
