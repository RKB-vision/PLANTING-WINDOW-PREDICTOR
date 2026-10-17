"""CLI to record whether the configured rod length has been verified.

The configured ``CAMERA_ROD_LENGTH_CM`` is UNVERIFIED until the physical rod
is measured and recorded here. Until then, camera measurements are collected
and stored but quarantined from the growth baseline.

Usage::

    python -m plantreadiness.verify_calibration --rod-length-cm 10.0 --verified
    python -m plantreadiness.verify_calibration --status
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import calibration
from . import config
from . import db as dbm
from .repository import SQLiteRepository


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Record (or inspect) the reference-rod calibration state.")
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH))
    parser.add_argument("--rod-length-cm", type=float, default=None,
                        help="Measured rod length in cm (defaults to the "
                             "configured value).")
    parser.add_argument("--verified", action="store_true",
                        help="Mark the configured/measured rod length as verified.")
    parser.add_argument("--by", dest="verified_by", default=None,
                        help="Who performed the verification.")
    parser.add_argument("--note", default=None, help="Free-text note.")
    parser.add_argument("--status", action="store_true",
                        help="Print the current calibration state and exit.")
    args = parser.parse_args(argv)

    conn = dbm.init_db(Path(args.db))
    try:
        repo = SQLiteRepository(conn)
        if args.status:
            state = repo.current_calibration_setting()
            print(json.dumps(calibration.describe(
                state, config.CAMERA_ROD_LENGTH_CM), indent=2, default=str))
            return 0

        rod = (args.rod_length_cm if args.rod_length_cm is not None
               else config.CAMERA_ROD_LENGTH_CM)
        if rod <= 0:
            print("rod length must be > 0", flush=True)
            return 2
        setting = repo.record_calibration_setting(
            rod_length_cm=rod, verified=args.verified,
            verified_by=args.verified_by, note=args.note)
        state = repo.current_calibration_setting()
        print(json.dumps(calibration.describe(
            state, config.CAMERA_ROD_LENGTH_CM), indent=2, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
PY