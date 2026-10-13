"""Import the real Super Orange growth CSV and run quality checks.

Expected columns (the file shipped with the project):

    Date, Age_days, Plant_ID, Height_cm, Growth_rate_cm_per_day

The import is **append-only and idempotent**: rows already stored are skipped,
never overwritten. Every imported row is marked ``data_source = 'real'``.

This module deliberately does NOT create readiness events: the CSV contains
growth measurements only, no verified readiness assessments. Its job is to
preserve measurements, report data quality and describe growth trends.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import db as dbm

REQUIRED_COLUMNS = ("Date", "Age_days", "Plant_ID", "Height_cm")
OPTIONAL_COLUMNS = ("Growth_rate_cm_per_day",)


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    return int(float(value))


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def import_csv(conn, csv_path: str | Path, species: str = "Super Orange",
               variety: Optional[str] = None,
               date_started: Optional[str] = None) -> dict:
    """Import measurement rows into the database under ``data_source='real'``.

    Returns an import report: counts plus any row-level issues found.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    report = {
        "csv_path": str(csv_path),
        "rows_read": 0,
        "plants_imported": 0,
        "plants_existing": 0,
        "observations_inserted": 0,
        "observations_skipped_duplicate": 0,
        "issues": [],
    }

    # Track which plants we have already registered/looked up in this run.
    plant_ids: dict[str, int] = {}

    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV {csv_path} has no header row.")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"CSV {csv_path} is missing required columns: {missing}. "
                f"Found: {list(reader.fieldnames)}"
            )

        for line_no, row in enumerate(reader, start=2):
            report["rows_read"] += 1
            raw_ref = (row.get("Plant_ID") or "").strip()
            if not raw_ref:
                report["issues"].append(
                    {"line": line_no, "problem": "missing Plant_ID"}
                )
                continue
            try:
                obs_date = dbm.parse_iso_date(
                    (row.get("Date") or "").strip(), "Date"
                )
            except dbm.ValidationError as exc:
                report["issues"].append({"line": line_no, "problem": str(exc)})
                continue

            try:
                height = _to_float(row.get("Height_cm"))
                growth = _to_float(row.get("Growth_rate_cm_per_day"))
                age = _to_int(row.get("Age_days"))
            except (TypeError, ValueError) as exc:
                report["issues"].append(
                    {"line": line_no, "problem": f"bad numeric value: {exc}"}
                )
                continue

            if height is None:
                report["issues"].append(
                    {"line": line_no, "problem": "missing Height_cm; "
                                                 "row skipped (no invented data)"}
                )
                continue

            # register / reuse plant
            if raw_ref not in plant_ids:
                existing = conn.execute(
                    """SELECT plant_id FROM plants
                       WHERE external_ref = ? AND data_source = 'real'
                         AND species = ?""",
                    (raw_ref, species),
                ).fetchone()
                if existing:
                    plant_ids[raw_ref] = int(existing["plant_id"])
                    report["plants_existing"] += 1
                else:
                    plant_ids[raw_ref] = dbm.register_plant(
                        conn,
                        species=species,
                        variety=variety,
                        date_started=date_started,
                        data_source="real",
                        external_ref=raw_ref,
                    )
                    report["plants_imported"] += 1

            try:
                dbm.add_observation(
                    conn,
                    plant_id=plant_ids[raw_ref],
                    observation_date=obs_date.isoformat(),
                    age_days=age,
                    height_cm=height,
                    growth_rate_cm_per_day=growth,
                    data_source="real",
                )
                report["observations_inserted"] += 1
            except dbm.DuplicateRecordError:
                report["observations_skipped_duplicate"] += 1
            except dbm.ValidationError as exc:
                report["issues"].append({"line": line_no, "problem": str(exc)})

    return report


# --------------------------------------------------------------------------- #
# Data-quality checks + growth analysis (real data only)
# --------------------------------------------------------------------------- #


def analyse_real_data(conn, species: Optional[str] = None,
                      data_source: str = "real") -> dict:
    """Run quality checks and growth analysis over stored observations.

    Returns a JSON-serialisable report. This is descriptive analysis of
    measurements - it does NOT establish readiness or train a countdown.
    """
    sql = """SELECT p.plant_id, p.external_ref, p.species, p.variety,
                    o.observation_date, o.age_days, o.height_cm,
                    o.growth_rate_cm_per_day
             FROM observations o JOIN plants p ON p.plant_id = o.plant_id
             WHERE o.data_source = ?"""
    args: list = [data_source]
    if species:
        sql += " AND p.species = ?"
        args.append(species)
    sql += " ORDER BY p.plant_id, o.observation_date"

    by_plant: dict[int, list[dict]] = defaultdict(list)
    for row in conn.execute(sql, args):
        by_plant[int(row["plant_id"])].append(dict(row))

    issues: list[dict] = []
    per_plant: list[dict] = []
    all_growth_rates: list[float] = []

    for plant_id, obs in by_plant.items():
        ref = obs[0].get("external_ref") or str(plant_id)
        heights = [o["height_cm"] for o in obs]
        rates = [o["growth_rate_cm_per_day"] for o in obs
                 if o["growth_rate_cm_per_day"] is not None]

        # --- quality checks -------------------------------------------------
        missing_heights = sum(1 for h in heights if h is None)
        if missing_heights:
            issues.append({"plant": ref, "problem":
                           f"{missing_heights} observation(s) missing height"})
        if len({o["observation_date"] for o in obs}) != len(obs):
            issues.append({"plant": ref, "problem":
                           "duplicate observation dates (kept first)"})
        negative = sum(1 for h in heights if h is not None and h < 0)
        if negative:
            issues.append({"plant": ref, "problem":
                           f"{negative} negative height value(s)"})
        # growth-rate consistency: stored rate should match (h_t - h_{t-1})
        # when both heights and consecutive dates are available
        inconsistent = 0
        for prev, cur in zip(obs, obs[1:]):
            if (prev["height_cm"] is None or cur["height_cm"] is None
                    or cur["growth_rate_cm_per_day"] is None):
                continue
            delta_days = dbm.days_between(prev["observation_date"],
                                          cur["observation_date"])
            if delta_days <= 0:
                continue
            expected = (cur["height_cm"] - prev["height_cm"]) / delta_days
            if abs(expected - cur["growth_rate_cm_per_day"]) > 0.051:
                inconsistent += 1
        if inconsistent:
            issues.append({"plant": ref, "problem":
                           f"{inconsistent} stored growth rate(s) disagree with "
                           "height deltas by >0.05 cm/day"})

        # --- growth trend ---------------------------------------------------
        valid = [(o["age_days"], o["height_cm"]) for o in obs
                 if o["age_days"] is not None and o["height_cm"] is not None]
        slope = None
        if len(valid) >= 2:
            xs = [v[0] for v in valid]
            ys = [v[1] for v in valid]
            n = len(valid)
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            denom = sum((x - mean_x) ** 2 for x in xs)
            if denom > 0:
                slope = round(
                    sum((x - mean_x) * (y - mean_y)
                        for x, y in zip(xs, ys)) / denom, 4
                )

        per_plant.append({
            "plant": ref,
            "plant_id": plant_id,
            "n_observations": len(obs),
            "first_date": obs[0]["observation_date"],
            "last_date": obs[-1]["observation_date"],
            "height_first_cm": heights[0],
            "height_last_cm": heights[-1],
            "height_min_cm": min(h for h in heights if h is not None) if heights else None,
            "height_max_cm": max(h for h in heights if h is not None) if heights else None,
            "mean_growth_rate_cm_per_day": (
                round(sum(rates) / len(rates), 4) if rates else None
            ),
            "linear_slope_cm_per_day": slope,
            "n_readiness_events": int(conn.execute(
                "SELECT COUNT(*) FROM readiness_events WHERE plant_id = ?",
                (plant_id,)).fetchone()[0]),
        })
        all_growth_rates.extend(rates)

    summary = {
        "data_source": data_source,
        "species": species,
        "n_plants": len(by_plant),
        "n_observations": sum(len(v) for v in by_plant.values()),
        "n_plants_with_readiness_event": sum(
            1 for p in per_plant if p["n_readiness_events"] > 0),
        "overall_mean_growth_rate_cm_per_day": (
            round(sum(all_growth_rates) / len(all_growth_rates), 4)
            if all_growth_rates else None
        ),
        "quality_issues": issues,
        "per_plant": per_plant,
    }
    return summary
