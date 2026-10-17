"""Seed a fresh local database so collaborators share the same starting state.

The repository does **not** ship the SQLite file: ``plant_readiness.db`` is
runtime state that changes with every measurement, so it is git-ignored. Run
this once to rebuild the shared state from the committed sources:

    python -m plantreadiness.seed

The seed is idempotent and honest - it never invents data:

1. creates the schema (``plantreadiness.db.init_db``) and imports the committed
   historical cohort (``data/super_orange_growth_data.csv``) with
   ``data_source='real'`` and ``cohort='reference'``;
2. ensures the successful-cohort reference baseline exists under
   ``models/reference/`` - it is committed, so it is reused as-is unless it is
   missing or ``--force`` is given (rebuilding it always changes its
   ``generated_at`` timestamp, which would churn the working tree).

Re-running imports nothing new (duplicate observations are skipped) and leaves
the tree unchanged. Missing values stay missing; in particular, no readiness
events are created because the CSV contains measurements only.

Camera plants are **not** part of the seed: they are always registered
explicitly (see ``plantreadiness.camera_refresh``) and never auto-assigned.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import config
from . import csv_import
from . import db as dbm
from . import reference as ref
from . import services as svc
from .repository import SQLiteRepository


def seed_database(*, db_path=None, csv_path=None, models_dir=None,
                  species: str | None = None, force: bool = False,
                  verbose: bool = True) -> dict:
    """Create/refresh the local database and ensure the reference baseline.

    Safe to run repeatedly: it only generates what is missing. Returns a
    JSON-serialisable report.
    """
    db_path = Path(db_path) if db_path else config.DEFAULT_DB_PATH
    csv_path = Path(csv_path) if csv_path else config.DEFAULT_REAL_CSV
    models_dir = Path(models_dir) if models_dir else config.MODELS_DIR
    species = species or config.DEFAULT_SPECIES

    if verbose:
        print(f"database         : {db_path}")
        print(f"reference cohort : {csv_path}")
        print(f"models directory : {models_dir}")

    conn = dbm.init_db(db_path)
    try:
        if csv_path.exists():
            import_report = csv_import.import_csv(
                conn, csv_path, species=species, date_started=None)
        else:
            import_report = {"error": f"CSV not found: {csv_path}"}

        repo = SQLiteRepository(conn)
        baseline, rebuilt = _ensure_baseline(
            repo, species, models_dir=models_dir, force=force)
    finally:
        conn.close()

    report = {
        "db_path": str(db_path),
        "csv_path": str(csv_path),
        "species": species,
        "import": import_report,
        "baseline": {
            "path": baseline.get("path"),
            "rebuilt": rebuilt,
            "n_plants": baseline.get("n_plants"),
            "n_points": baseline.get("n_points"),
            "transplant_reference": baseline.get("transplant_reference"),
        },
    }

    if verbose:
        _print_report(report)
    return report


def _ensure_baseline(repo: SQLiteRepository, species: str, *,
                     models_dir: Path, force: bool) -> tuple[dict, bool]:
    """Return ``(baseline, rebuilt)`` - reusing the committed baseline.

    Rebuilding an existing baseline only changes its ``generated_at``
    timestamp, so it is reused unless it is missing or ``force`` is set.
    """
    if not force:
        existing = svc.load_reference_baseline(models_dir, species)
        if existing is not None:
            existing = dict(existing)
            existing["path"] = str(ref.baseline_path(models_dir, species, None))
            return existing, False
    return svc.build_reference_baseline(
        repo, species, models_dir=models_dir), True


def _print_report(report: dict) -> None:
    imp = report["import"]
    print("\nimport:")
    if "error" in imp:
        print(f"  {imp['error']}")
    else:
        print(f"  rows read              : {imp['rows_read']}")
        print(f"  plants imported        : {imp['plants_imported']}")
        print(f"  plants already present : {imp['plants_existing']}")
        print(f"  observations inserted  : {imp['observations_inserted']}")
        print(f"  duplicates skipped     : "
              f"{imp['observations_skipped_duplicate']}")
        if imp.get("issues"):
            print(f"  row issues             : {len(imp['issues'])}")

    base = report["baseline"]
    print("reference baseline:")
    print(f"  plants                 : {base.get('n_plants')}")
    print(f"  points                 : {base.get('n_points')}")
    transplant = base.get("transplant_reference") or {}
    if transplant:
        print(f"  transplant reference   : "
              f"{transplant.get('n_recorded', 0)} recorded, "
              f"{transplant.get('n_assumed', 0)} assumed")
    state = "rebuilt" if base.get("rebuilt") else "reused (already committed)"
    print(f"  {state:<24}: {base.get('path')}")
    print("\ndone - start the API/UI with ./run.sh")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the local database + reference baseline.")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--csv", default=str(config.DEFAULT_REAL_CSV))
    parser.add_argument("--models-dir", default=str(config.MODELS_DIR))
    parser.add_argument("--species", default=config.DEFAULT_SPECIES)
    parser.add_argument("--force", action="store_true",
                        help="rebuild the reference baseline even if present")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON instead of text")
    args = parser.parse_args(argv)

    report = seed_database(
        db_path=args.db, csv_path=args.csv, models_dir=args.models_dir,
        species=args.species, force=args.force, verbose=not args.json)
    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
