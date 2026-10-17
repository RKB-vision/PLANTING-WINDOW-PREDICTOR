"""One-time migration: rename 'Super Orange' -> 'Sunflower (SuperOrange)'.

Dry-run by default. Pass ``--yes`` to apply. Never deletes records; only
updates the species label on plants and the matching historical reference
baseline filenames are left for the user to rebuild (the old file is kept).
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from . import config
from . import db as dbm


def plan(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT plant_id, species, variety, external_ref, cohort, data_source "
        "FROM plants WHERE species = ?",
        (config.LEGACY_SPECIES_SUPER_ORANGE,)).fetchall()
    return [dict(r) for r in rows]


def apply(conn) -> int:
    cur = conn.execute(
        "UPDATE plants SET species = ? WHERE species = ?",
        (config.SPECIES_SUNFLOWER_SUPER_ORANGE,
         config.LEGACY_SPECIES_SUPER_ORANGE))
    conn.commit()
    return cur.rowcount


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Rename 'Super Orange' to 'Sunflower (SuperOrange)'.")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--yes", action="store_true",
                        help="Apply the migration (default is dry-run).")
    args = parser.parse_args(argv)

    conn = dbm.connect(Path(args.db))
    try:
        rows = plan(conn)
        print(f"Plants to rename: {len(rows)}")
        for row in rows:
            print("  plant_id=%s external_ref=%s cohort=%s" % (
                row["plant_id"], row["external_ref"], row["cohort"]))
        if not args.yes:
            print("Dry run only. Re-run with --yes to apply.")
            return 0
        n = apply(conn)
        print(f"Renamed {n} plant(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
PY