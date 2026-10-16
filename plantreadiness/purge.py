"""Remove SYNTHETIC demo rows from a database - real data is never touched.

Synthetic records always carry ``data_source = 'synthetic'`` (see
``plantreadiness.synthetic``). This tool deletes exactly those rows - plants,
observations, readiness events, feedback, predictions and assessments - and
nothing else, so it cannot damage the real/observational record set, which
stays append-only (see ``AGENTS.md``).

    python -m plantreadiness.purge            # dry run: show what would go
    python -m plantreadiness.purge --yes      # actually delete
    python -m plantreadiness.purge --db other.db --yes

Prefer a scratch database for synthetic experiments so they never reach the
real one in the first place:

    python model_train.py --mode demo          # -> plant_readiness_demo.db
    python demo_workflow.py                    # -> demo_workflow.db

Real records (``data_source='real'``) are deliberately out of scope: use a
separate database for experiments instead of deleting real history.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import config
from . import db as dbm

SYNTHETIC = "synthetic"

# Children before parents, so foreign keys stay satisfied. Corrections have no
# data_source of their own, so they follow their (synthetic) feedback record.
_SYNTHETIC_FEEDBACK = (
    "SELECT feedback_id FROM feedback_14d WHERE data_source = 'synthetic'"
)
_DELETE_PLAN: tuple[tuple[str, str], ...] = (
    ("feedback_corrections",
     f"feedback_id IN ({_SYNTHETIC_FEEDBACK})"),
    ("readiness_assessments", "data_source = 'synthetic'"),
    ("prediction_history", "data_source = 'synthetic'"),
    ("feedback_14d", "data_source = 'synthetic'"),
    ("readiness_events", "data_source = 'synthetic'"),
    ("observations", "data_source = 'synthetic'"),
    ("plants", "data_source = 'synthetic'"),
)


def _existing_tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(r[0]) for r in rows}


def purge_synthetic(conn, *, dry_run: bool = True) -> dict:
    """Delete rows tagged ``data_source='synthetic'``.

    Returns a report; with ``dry_run`` only counts are gathered. Tables that
    do not exist yet (some are created lazily) are skipped.
    """
    present = _existing_tables(conn)
    report: dict = {"dry_run": dry_run, "tables": {}, "total": 0}
    for table, where in _DELETE_PLAN:
        if table not in present:
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}"
        ).fetchone()[0]
        report["tables"][table] = int(n)
        if n and not dry_run:
            conn.execute(f"DELETE FROM {table} WHERE {where}")
    report["total"] = sum(report["tables"].values())
    if not dry_run:
        conn.commit()
    return report


def _print_report(report: dict) -> None:
    state = "DRY RUN - nothing deleted" if report["dry_run"] else "DELETED"
    print(f"synthetic purge ({state})")
    for table, n in report["tables"].items():
        print(f"  {table:24} {n:>6}")
    print(f"  {'TOTAL':24} {report['total']:>6}")
    if report["dry_run"]:
        if report["total"]:
            print("\nre-run with --yes to delete these rows")
        else:
            print("\nnothing synthetic to delete")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove SYNTHETIC demo rows (never real data).")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--yes", action="store_true",
                        help="actually delete (default: dry run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}")
        return 1

    conn = dbm.connect(db_path)
    try:
        report = purge_synthetic(conn, dry_run=not args.yes)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
