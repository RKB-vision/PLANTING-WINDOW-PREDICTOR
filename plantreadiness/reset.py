"""Reset the local runtime database back to the committed CSV reference state.

Why this exists
---------------
``plantreadiness.seed`` builds the shared starting state but is *additive*: it
imports the reference cohort and skips what is already present. Anything created
afterwards - camera plants, manual experiments, test fixtures, synthetic demo
data - stays in the database. This module returns the database to the honest
baseline the repository ships with:

1. back up the current database (timestamped, git-ignored) unless disabled,
2. delete the runtime database (and its WAL/SHM sidecars),
3. rebuild it from ``data/super_orange_growth_data.csv`` via
   :func:`plantreadiness.seed.seed_database` - ``data_source='real'``,
   ``cohort='reference'``, ``calibration_source='csv_import_no_calibration'``,
4. ensure the successful-cohort reference baseline exists.
5. clear the Cloudinary processed-ID ledger so a later refresh can backfill
   camera observations after the database rebuild.

The CSV is the single source of truth. If it is missing the reset aborts
*before* touching the database, so a wipe can never destroy data that cannot be
rebuilt. Missing values stay missing; no readiness events are invented.

Usage::

    ./reset                      # back up, wipe, reseed from the CSV
    ./reset --no-backup          # skip the safety backup
    ./reset --rebuild-baseline   # force-rebuild the reference baseline
    ./reset --camera             # also clear untracked camera artefacts
    python -m plantreadiness.reset --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from . import db as dbm
from . import seed


def _backup_database(db_path: Path, backup_dir: Path) -> Path:
    """Copy ``db_path`` to a consistent timestamped backup via SQLite.

    Uses SQLite's online backup API so a WAL/journal is checkpointed into the
    copy. The backup is written under ``backup_dir`` (default
    ``data/generated/backups/``, which is git-ignored) so repeated resets never
    clutter the repository root.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.name}.bak-{stamp}"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup_path


def _remove_database_files(db_path: Path) -> list[str]:
    """Delete the database and any ``-wal``/``-shm`` sidecars. Returns names."""
    removed: list[str] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists():
            candidate.unlink()
            removed.append(candidate.name)
    return removed


def _clear_camera_artifacts(base_dir: Path) -> list[str]:
    """Remove untracked ESP32-CAM / Cloudinary runtime artefacts.

    Only git-ignored, regenerable capture inputs/caches are touched. Annotated
    plant result images are retained so reviewed measurement output survives a
    camera cleanup. Generated runtime files (e.g.
    ``plant_daily_measurements.csv``) are deliberately left alone.
    """
    removed: list[str] = []
    patterns = ["plant_captured.jpg", "Plant*.jpg"]
    for pattern in patterns:
        for path in base_dir.glob(pattern):
            if path.is_file():
                path.unlink()
                removed.append(path.name)
    captures = base_dir / "captures"
    if captures.is_dir():
        shutil.rmtree(captures)
        removed.append("captures/")
    return removed


def _clear_processed_cloudinary_ids(base_dir: Path) -> bool:
    """Clear the capture ledger after a DB reset so missing rows can be reimported."""
    ledger = base_dir / "processed_cloudinary_ids.txt"
    if not ledger.exists():
        return False
    ledger.unlink()
    return True


def _counts(db_path: Path) -> dict:
    """Read-only snapshot of the row counts that describe the reset state."""
    if not db_path.exists():
        return {"exists": False}
    conn = sqlite3.connect(str(db_path))
    try:
        plants = conn.execute("SELECT COUNT(*) FROM plants").fetchone()[0]
        observations = conn.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0]
        try:
            captures = conn.execute(
                "SELECT COUNT(*) FROM camera_captures").fetchone()[0]
        except sqlite3.OperationalError:
            captures = 0
        non_reference = conn.execute(
            "SELECT COUNT(*) FROM plants WHERE cohort IS NOT 'reference'"
        ).fetchone()[0]
        return {
            "exists": True,
            "plants": plants,
            "observations": observations,
            "captures": captures,
            "plants_outside_reference_cohort": non_reference,
        }
    finally:
        conn.close()


def reset_database(*, db_path=None, csv_path=None, models_dir=None,
                   species: Optional[str] = None, backup: bool = True,
                   backup_dir=None, rebuild_baseline: bool = False,
                   camera: bool = False, verbose: bool = True,
                   base_dir=None) -> dict:
    """Wipe the runtime database and rebuild it from the committed CSV.

    Returns a JSON-serialisable report. Raises ``FileNotFoundError`` if the CSV
    is missing (the reset must never destroy data it cannot recreate).
    """
    db_path = Path(db_path) if db_path else config.DEFAULT_DB_PATH
    csv_path = Path(csv_path) if csv_path else config.DEFAULT_REAL_CSV
    models_dir = Path(models_dir) if models_dir else config.MODELS_DIR
    backup_dir = (Path(backup_dir) if backup_dir
                  else config.GENERATED_DIR / "backups")
    base_dir = Path(base_dir) if base_dir else config.BASE_DIR

    if not csv_path.exists():
        raise FileNotFoundError(
            f"Reference CSV not found: {csv_path}. Refusing to reset the "
            "database - there would be no committed source to rebuild from.")

    before = _counts(db_path)

    backup_path: Optional[Path] = None
    if db_path.exists() and backup:
        backup_path = _backup_database(db_path, backup_dir)

    removed = _remove_database_files(db_path)

    seed_report = seed.seed_database(
        db_path=db_path, csv_path=csv_path, models_dir=models_dir,
        species=species, force=rebuild_baseline, verbose=False)

    # The DB was rebuilt from the reference CSV, so camera IDs marked processed
    # before the reset must be eligible for import again.
    processed_ids_cleared = _clear_processed_cloudinary_ids(base_dir)

    after = _counts(db_path)

    camera_removed: list[str] = []
    if camera:
        camera_removed = _clear_camera_artifacts(base_dir)

    report = {
        "db_path": str(db_path),
        "csv_path": str(csv_path),
        "models_dir": str(models_dir),
        "backup": str(backup_path) if backup_path else None,
        "removed_db_files": removed,
        "before": before,
        "after": after,
        "import": seed_report.get("import"),
        "baseline": seed_report.get("baseline"),
        "processed_cloudinary_ids_cleared": processed_ids_cleared,
        "camera_artifacts_removed": camera_removed,
    }

    if verbose:
        _print_report(report)
    return report


def _print_report(report: dict) -> None:
    before = report["before"]
    after = report["after"]
    print(f"database   : {report['db_path']}")
    print(f"source CSV : {report['csv_path']}")

    if before.get("exists"):
        print("\nbefore:")
        print(f"  plants       : {before['plants']} "
              f"({before['plants_outside_reference_cohort']} outside the "
              "reference cohort)")
        print(f"  observations : {before['observations']}")
        print(f"  captures     : {before['captures']}")
    else:
        print("\nbefore: no database present")

    if report["backup"]:
        print(f"\nbackup      : {report['backup']}")
    else:
        print("\nbackup      : skipped")

    imp = report.get("import") or {}
    print("\nrebuilt from CSV:")
    if "error" in imp:
        print(f"  {imp['error']}")
    else:
        print(f"  plants       : {imp.get('plants_imported')} imported, "
              f"{imp.get('plants_existing')} already present")
        print(f"  observations : {imp.get('observations_inserted')} inserted, "
              f"{imp.get('observations_skipped_duplicate')} duplicates skipped")
        if imp.get("issues"):
            print(f"  row issues   : {len(imp['issues'])}")

    base = report.get("baseline") or {}
    state = "rebuilt" if base.get("rebuilt") else "reused (committed)"
    print(f"  baseline     : {base.get('n_plants')} plants, "
          f"{base.get('n_points')} points ({state})")

    print("\nafter:")
    print(f"  plants       : {after.get('plants')}")
    print(f"  observations : {after.get('observations')}")
    print(f"  captures     : {after.get('captures')}")
    print("\nCloudinary processed-ID ledger: "
          f"{'cleared' if report['processed_cloudinary_ids_cleared'] else 'not present'}")

    if report["camera_artifacts_removed"]:
        print("\ncamera artefacts removed:")
        for name in report["camera_artifacts_removed"]:
            print(f"  {name}")

    print("\ndone - the database now matches the committed CSV cohort.")
    print("If the API/UI is running, restart it (./run.sh) so it reopens the "
          "database.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset the runtime database back to the committed CSV "
                    "reference cohort.")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--csv", default=str(config.DEFAULT_REAL_CSV))
    parser.add_argument("--models-dir", default=str(config.MODELS_DIR))
    parser.add_argument("--species", default=config.DEFAULT_SPECIES)
    parser.add_argument("--no-backup", action="store_true",
                        help="do not keep a timestamped copy of the old DB")
    parser.add_argument("--rebuild-baseline", action="store_true",
                        help="rebuild the reference baseline even if present")
    parser.add_argument("--camera", action="store_true",
                        help="also clear downloaded camera images and captures/")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON instead of text")
    args = parser.parse_args(argv)

    try:
        report = reset_database(
            db_path=args.db, csv_path=args.csv, models_dir=args.models_dir,
            species=args.species, backup=not args.no_backup,
            rebuild_baseline=args.rebuild_baseline, camera=args.camera,
            verbose=not args.json)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
