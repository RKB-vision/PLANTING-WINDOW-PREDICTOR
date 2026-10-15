#!/usr/bin/env python
"""Migrate the local SQLite prototype database into Supabase PostgreSQL.

Design rules (task section 5):

* **dry-run by default** - nothing is written until ``--apply`` is given,
* **backup before write** - the SQLite file is copied to ``<db>.bak-<ts>``
  before the first insert in apply mode,
* **idempotent** - re-running skips rows that already exist (matched by
  natural identity) instead of duplicating them,
* **duplicate detection** - in-source duplicates and target-existing rows
  are both detected and reported separately,
* **real/synthetic separation** - synthetic rows are NEVER migrated unless
  ``--include-synthetic`` is passed explicitly, and even then they keep
  ``data_source='synthetic'`` so they can never join the real training pool,
* **no privileged keys** - the script signs in as the destination user with
  Supabase Auth (email + password) and writes with the user's own JWT, so
  RLS stamps and checks ``user_id = auth.uid()``. No service-role key is
  read, written, logged or required.
* **per-source preview** - the report groups everything by data_source.

Usage
-----
    # 1. inspect + preview (default: dry-run)
    python scripts/migrate_sqlite_to_supabase.py

    # 2. apply for real data only
    python scripts/migrate_sqlite_to_supabase.py \
        --email you@example.com --password '...' --apply

    # 3. opt-in: also carry the SYNTHETIC DEMO rows (kept separate)
    python scripts/migrate_sqlite_to_supabase.py \
        --email you@example.com --password '...' --apply \
        --include-synthetic

Configuration comes from (in order): CLI flags, environment variables
(``SUPABASE_URL``), then ``.streamlit/secrets.toml``. Secrets are never
printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plantreadiness import config  # noqa: E402
from plantreadiness.db import parse_iso_date  # noqa: E402

# Tables in dependency order (parents before children).
TABLE_ORDER = ("plants", "observations", "readiness_events",
               "transplant_events", "feedback_14d", "feedback_corrections")


class MigrationError(Exception):
    """Configuration or runtime problem - reported clearly, never guessed."""


# --------------------------------------------------------------------------- #
# Configuration (secrets are read, never echoed)
# --------------------------------------------------------------------------- #


def load_config(args: argparse.Namespace) -> dict:
    """Resolve Supabase URL/credentials from flags -> env -> secrets file."""
    url = args.url or os.environ.get("SUPABASE_URL")
    anon_key = args.anon_key or os.environ.get("SUPABASE_ANON_KEY")

    secrets_path = PROJECT_ROOT / ".streamlit" / "secrets.toml"
    if (url is None or anon_key is None) and secrets_path.exists():
        parsed = _parse_toml(secrets_path)
        section = parsed.get("supabase", {})
        url = url or section.get("url")
        anon_key = anon_key or section.get("anon_key")

    if not url or not anon_key:
        raise MigrationError(
            "Supabase is not configured. Provide --url/--anon-key, set "
            "SUPABASE_URL / SUPABASE_ANON_KEY, or create "
            ".streamlit/secrets.toml with a [supabase] section "
            "(see .streamlit/secrets.toml.example).")
    return {"url": url, "anon_key": anon_key,
            "email": args.email or os.environ.get("SUPABASE_EMAIL"),
            "password": args.password or os.environ.get("SUPABASE_PASSWORD")}


def _parse_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        raise MigrationError(
            f"Cannot parse {path}: tomllib unavailable on this Python.")
    with path.open("rb") as fh:
        return tomllib.load(fh)


# --------------------------------------------------------------------------- #
# Source inspection
# --------------------------------------------------------------------------- #


def inspect_sqlite(db_path: Path) -> dict:
    """Inspect the source schema and count rows grouped by data_source."""
    if not db_path.exists():
        raise MigrationError(f"SQLite database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "plants" not in tables:
            raise MigrationError(
                f"{db_path} has no 'plants' table - not a plant-readiness "
                "database.")

        info: dict[str, Any] = {"path": str(db_path), "tables": {},
                                "counts_by_source": {}}
        for table in ("plants", "observations", "readiness_events",
                      "feedback_14d"):
            if table not in tables:
                info["tables"][table] = {"present": False}
                continue
            cols = [r["name"] for r in conn.execute(
                f"PRAGMA table_info({table})")]
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            info["tables"][table] = {"present": True, "columns": cols,
                                     "rows": int(n)}
            if "data_source" in cols:
                by_source = {r[0]: int(r[1]) for r in conn.execute(
                    f"SELECT data_source, COUNT(*) FROM {table} "
                    f"GROUP BY data_source")}
            else:
                by_source = {"unlabelled": int(n)}
            info["counts_by_source"][table] = by_source
        return info, conn
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise MigrationError(f"Could not read {db_path}: {exc}") from exc


def read_source_rows(conn: sqlite3.Connection,
                     include_synthetic: bool) -> dict:
    """Load all rows, excluding synthetic data unless explicitly opted in."""
    def _fetch(sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in conn.execute(sql, args)]

    where = "" if include_synthetic else "WHERE data_source = 'real'"
    plants = _fetch(f"SELECT * FROM plants {where} ORDER BY plant_id")
    plant_ids = [p["plant_id"] for p in plants]
    id_list = ",".join("?" * len(plant_ids)) or "NULL"

    observations, readiness, feedback = [], [], []
    if plant_ids:
        observations = _fetch(
            f"SELECT * FROM observations WHERE plant_id IN ({id_list}) "
            "ORDER BY observation_id", tuple(plant_ids))
        readiness = _fetch(
            f"SELECT * FROM readiness_events WHERE plant_id IN ({id_list}) "
            "ORDER BY readiness_event_id", tuple(plant_ids))
        feedback = _fetch(
            f"SELECT * FROM feedback_14d WHERE plant_id IN ({id_list}) "
            "ORDER BY feedback_id", tuple(plant_ids))
    corrections = []
    if feedback:
        fb_ids = [f["feedback_id"] for f in feedback]
        fb_list = ",".join("?" * len(fb_ids))
        try:
            corrections = _fetch(
                f"SELECT * FROM feedback_corrections "
                f"WHERE feedback_id IN ({fb_list}) ORDER BY correction_id",
                tuple(fb_ids))
        except sqlite3.DatabaseError:
            corrections = []  # table only exists if a correction was made
    return {"plants": plants, "observations": observations,
            "readiness_events": readiness, "feedback_14d": feedback,
            "feedback_corrections": corrections}


# --------------------------------------------------------------------------- #
# Duplicate detection + row building
# --------------------------------------------------------------------------- #


def detect_duplicates(rows: list[dict], keys: tuple[str, ...]) -> tuple[list[dict], list[dict]]:
    """Split rows into unique ones and in-source duplicates."""
    seen: set[tuple] = set()
    unique, dupes = [], []
    for row in rows:
        ident = tuple(row.get(k) for k in keys)
        if ident in seen:
            dupes.append(row)
        else:
            seen.add(ident)
            unique.append(row)
    return unique, dupes


def _ident_plant(row: dict) -> tuple:
    """Natural identity of a plant row (used for source/target matching)."""
    return (row["species"], row["data_source"], row.get("variety"),
            row.get("external_ref"))


def build_plan(conn: sqlite3.Connection, include_synthetic: bool,
               target) -> dict:
    """Compute what would be inserted, skipped (duplicate) or failed.

    ``to_insert`` contains only rows that are NOT already in the target;
    rows found in the target land in ``duplicate_in_target`` (counted as
    skipped) and contribute their target ids to ``_plant_id_map`` so child
    rows can be matched by *target* plant id - source and target ids are
    never assumed to be equal.
    """
    source = read_source_rows(conn, include_synthetic)

    plan: dict[str, Any] = {"include_synthetic": include_synthetic,
                            "tables": {}, "skipped_synthetic": {}}

    # Report synthetic rows left behind when not opted in.
    if not include_synthetic:
        for table in ("plants", "observations", "readiness_events",
                      "feedback_14d"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE data_source = 'synthetic'").fetchone()[0]
            if n:
                plan["skipped_synthetic"][table] = int(n)

    # -- plants ----------------------------------------------------------- #
    unique_plants, dup_plants = detect_duplicates(
        source["plants"], ("data_source", "species", "variety",
                           "external_ref"))
    target_plants = target.existing_plants(unique_plants)
    target_by_ident = {_ident_plant(p): p for p in target_plants}

    plant_id_map: dict[int, int] = {}
    to_insert_plants, matched_plants = [], []
    for src in unique_plants:
        hit = target_by_ident.get(_ident_plant(src))
        if hit is not None:
            matched_plants.append(src)
            plant_id_map[src["plant_id"]] = int(hit["plant_id"])
        else:
            to_insert_plants.append(src)
    plan["tables"]["plants"] = {
        "to_insert": to_insert_plants,
        "duplicate_in_source": dup_plants,
        "duplicate_in_target": matched_plants,
    }
    plan["_plant_id_map"] = plant_id_map

    source_plant_ids = {p["plant_id"] for p in unique_plants}

    def _split_children(rows, keys, exists_check):
        """Split child rows into to_insert / duplicate_in_target.

        Rows whose parent is already in the target are checked against it
        using the mapped target id; rows of parents that still have to be
        inserted cannot pre-exist, so they always go to ``to_insert``.
        ``exists_check`` receives (mapped_target_plant_id, row) and returns
        True when the row is already present.
        """
        unique_rows, dup_src = detect_duplicates(rows, keys)
        insertable, matched = [], []
        for row in unique_rows:
            target_pid = plant_id_map.get(row["plant_id"])
            if target_pid is not None and exists_check(target_pid, row):
                matched.append({**row, "_target_plant_id": target_pid})
            else:
                insertable.append(row)
        return unique_rows, dup_src, insertable, matched

    # -- observations ------------------------------------------------------ #
    obs = [o for o in source["observations"]
           if o["plant_id"] in source_plant_ids]
    _, dup_obs, to_insert_obs, matched_obs = _split_children(
        obs, ("plant_id", "observation_date"),
        lambda tp, row: bool(target.existing_observations(
            [{"plant_id": tp,
              "observation_date": row["observation_date"]}])))
    plan["tables"]["observations"] = {
        "to_insert": to_insert_obs, "duplicate_in_source": dup_obs,
        "duplicate_in_target": matched_obs}

    # -- readiness events --------------------------------------------------- #
    rel = [r for r in source["readiness_events"]
           if r["plant_id"] in source_plant_ids]
    _, dup_re, to_insert_re, matched_re = _split_children(
        rel, ("plant_id", "readiness_definition_version"),
        lambda tp, row: bool(target.existing_readiness(
            [{"plant_id": tp, "readiness_definition_version":
              row["readiness_definition_version"]}])))
    plan["tables"]["readiness_events"] = {
        "to_insert": to_insert_re, "duplicate_in_source": dup_re,
        "duplicate_in_target": matched_re}

    # -- transplant + feedback (SQLite stores them in one table) ------------ #
    fb = [f for f in source["feedback_14d"]
          if f["plant_id"] in source_plant_ids]
    _, dup_fb, to_insert_fb, matched_fb = _split_children(
        fb, ("plant_id", "transplant_date"),
        lambda tp, row: bool(target.existing_feedback(
            [{"plant_id": tp, "transplant_date": row["transplant_date"]}])))
    plan["tables"]["feedback_14d"] = {
        "to_insert": to_insert_fb, "duplicate_in_source": dup_fb,
        "duplicate_in_target": matched_fb}

    # source feedback id -> target feedback id (for corrections)
    feedback_id_map: dict[int, int] = {}
    for row in matched_fb:
        for target_fb in target.existing_feedback(
                [{"plant_id": row["_target_plant_id"],
                  "transplant_date": row["transplant_date"]}]):
            if target_fb.get("feedback_id") is not None:
                feedback_id_map[row["feedback_id"]] = int(
                    target_fb["feedback_id"])
                break
    plan["_feedback_id_map"] = feedback_id_map

    # -- corrections --------------------------------------------------------- #
    migratable_fb = set(feedback_id_map) | {
        f["feedback_id"] for f in to_insert_fb}
    corr = [c for c in source["feedback_corrections"]
            if c["feedback_id"] in migratable_fb]
    unique_corr, dup_corr = detect_duplicates(
        corr, ("feedback_id", "corrected_at", "previous_values"))
    plan["tables"]["feedback_corrections"] = {
        "to_insert": unique_corr, "duplicate_in_source": dup_corr,
        "duplicate_in_target": [],
    }
    plan["_source_ids"] = {
        "plant": {p["plant_id"]: p for p in unique_plants},
        "feedback": {f["feedback_id"]: f
                     for f in (to_insert_fb + matched_fb)},
    }
    return plan


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def print_preview(info: dict, plan: dict) -> None:
    """Human-readable dry-run report, grouped by data source."""
    print("=" * 70)
    print("SQLITE -> SUPABASE MIGRATION PREVIEW (dry-run: nothing written)")
    print("=" * 70)
    print(f"Source: {info['path']}")
    print("\nSource rows by table and data_source:")
    for table, counts in info["counts_by_source"].items():
        if info["tables"].get(table, {}).get("present"):
            print(f"  {table:<20} {counts}")

    if plan["skipped_synthetic"]:
        print("\nSYNTHETIC rows EXCLUDED (opt in with --include-synthetic):")
        for table, n in plan["skipped_synthetic"].items():
            print(f"  {table:<20} {n} synthetic row(s) left behind")
        print("  -> synthetic data is never merged into the real pool "
              "automatically.")

    print("\nPlan per table:")
    print(f"  {'table':<22}{'insert':>8}{'dup(src)':>10}{'dup(dst)':>10}"
          f"{'failed':>8}")
    for name in TABLE_ORDER:
        entry = plan["tables"].get(name)
        if entry is None:
            continue
        print(f"  {name:<22}{len(entry['to_insert']):>8}"
              f"{len(entry['duplicate_in_source']):>10}"
              f"{len(entry['duplicate_in_target']):>10}{0:>8}")
    print("\nNo changes were made. Re-run with --apply to execute.")


def print_report(result: dict) -> None:
    print("=" * 70)
    print("MIGRATION REPORT" + (" (apply mode)" if result["applied"]
                                else " (dry-run)"))
    print("=" * 70)
    print(f"  {'table':<22}{'inserted':>10}{'skipped':>10}{'failed':>8}")
    for name, counts in result["tables"].items():
        print(f"  {name:<22}{counts['inserted']:>10}"
              f"{counts['skipped']:>10}{counts['failed']:>8}")
    if result.get("errors"):
        print("\nFailures (row-level, migration continued):")
        for err in result["errors"][:20]:
            print(f"  - {err}")
        if len(result["errors"]) > 20:
            print(f"  ... and {len(result['errors']) - 20} more")
    if result.get("backup"):
        print(f"\nSQLite backup: {result['backup']}")
    print(f"\nIdempotent: re-running will skip the "
          f"{sum(c['inserted'] for c in result['tables'].values())} "
          "row(s) already present.")


# --------------------------------------------------------------------------- #
# Target access (authenticated user, no service-role key)
# --------------------------------------------------------------------------- #


class SupabaseTarget:
    """Insert rows as the signed-in destination user (RLS-scoped)."""

    def __init__(self, client, user_id: str):
        self.client = client
        self.user_id = user_id

    # -- existing-row lookups (duplicate detection) ------------------------ #
    def _select(self, table: str, **filters):
        q = self.client.table(table).select("*").eq("user_id", self.user_id)
        for col, val in filters.items():
            if val is not None:
                q = q.eq(col, val)
        return list(q.execute().data or [])

    def existing_plants(self, rows: list[dict]) -> list[dict]:
        found = []
        for row in rows:
            matches = self._select(
                "plants", species=row["species"],
                data_source=row["data_source"], variety=row.get("variety"),
                external_ref=row.get("external_ref"))
            found.extend(matches)
        return found

    def existing_observations(self, rows: list[dict]) -> list[dict]:
        found = []
        for row in rows:
            matches = self._select(
                "observations", plant_id=row["plant_id"],
                observation_date=row["observation_date"])
            found.extend(matches)
        return found

    def existing_readiness(self, rows: list[dict]) -> list[dict]:
        found = []
        for row in rows:
            matches = self._select(
                "readiness_events", plant_id=row["plant_id"],
                readiness_definition_version=row[
                    "readiness_definition_version"])
            found.extend(matches)
        return found

    def existing_feedback(self, rows: list[dict]) -> list[dict]:
        """Feedback rows for the given (mapped) plant/transplant identities.

        Returns the *feedback* rows themselves so their ``feedback_id`` can
        be mapped for correction migration - duplicate detection for the
        transplant step uses the linked transplant event's uniqueness.
        """
        found = []
        for row in rows:
            transplants = self._select(
                "transplant_events", plant_id=row["plant_id"],
                transplant_date=row["transplant_date"])
            for t in transplants:
                found.extend(self._select(
                    "feedback_14d",
                    transplant_event_id=t["transplant_event_id"]))
        return found

    # -- writes ------------------------------------------------------------ #
    def insert(self, table: str, row: dict) -> Optional[int]:
        payload = {**row, "user_id": self.user_id}
        resp = self.client.table(table).insert(payload).execute()
        data = list(resp.data or [])
        if not data:
            return None
        id_col = next((c for c in ("plant_id", "observation_id",
                                   "readiness_event_id",
                                   "transplant_event_id", "feedback_id",
                                   "correction_id") if c in data[0]), None)
        return int(data[0][id_col]) if id_col else None


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _backup_sqlite(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = db_path.with_suffix(db_path.suffix + f".bak-{stamp}")
    shutil.copy2(db_path, backup)
    return backup


def run_migration(args: argparse.Namespace) -> dict:
    cfg = load_config(args)
    info, conn = inspect_sqlite(Path(args.sqlite))

    try:
        from supabase import create_client
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise MigrationError(
            "The 'supabase' package is not installed - run "
            "`pip install supabase`.") from exc

    try:
        client = create_client(cfg["url"], cfg["anon_key"])
    except Exception as exc:
        raise MigrationError(f"Could not create Supabase client: {exc}")

    if not cfg["email"] or not cfg["password"]:
        raise MigrationError(
            "Destination account not provided. Pass --email/--password or "
            "set SUPABASE_EMAIL / SUPABASE_PASSWORD. The migration signs in "
            "as the destination user so Row Level Security stamps the rows - "
            "no service-role key is used or needed.")
    try:
        auth = client.auth.sign_in_with_password(
            {"email": cfg["email"], "password": cfg["password"]})
    except Exception as exc:
        raise MigrationError(f"Supabase sign-in failed: {exc}")
    user = auth.user
    if user is None:
        raise MigrationError("Supabase sign-in returned no user.")
    target = SupabaseTarget(client, str(user.id))

    plan = build_plan(conn, args.include_synthetic, target)

    if not args.apply:
        print_preview(info, plan)
        result = {"applied": False, "tables": {}, "errors": [],
                  "backup": None,
                  "planned": {k: len(v["to_insert"])
                              for k, v in plan["tables"].items()}}
        return result

    # -- apply mode: backup FIRST, then write ------------------------------ #
    backup = _backup_sqlite(Path(args.sqlite))
    result: dict[str, Any] = {
        "applied": True, "backup": str(backup), "errors": [],
        "tables": {name: {"inserted": 0, "skipped": 0, "failed": 0}
                   for name in TABLE_ORDER},
    }

    def _is_duplicate(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "duplicate" in msg or "23505" in msg

    # -- plants ------------------------------------------------------------- #
    # Rows already in the target were mapped during planning (idempotent
    # re-run) and are counted as skipped, never re-inserted.
    plant_id_map: dict[int, int] = dict(plan["_plant_id_map"])
    result["tables"]["plants"]["skipped"] = len(plant_id_map)
    for src in plan["tables"]["plants"]["to_insert"]:
        try:
            new_id = target.insert("plants", {
                "species": src["species"], "variety": src.get("variety"),
                "date_started": src.get("date_started"),
                "data_source": src["data_source"],
                "growing_conditions": src.get("growing_conditions"),
                "external_ref": src.get("external_ref"),
                "cohort": src.get("cohort")})
            if new_id is not None:
                plant_id_map[src["plant_id"]] = int(new_id)
            result["tables"]["plants"]["inserted"] += 1
        except Exception as exc:
            if _is_duplicate(exc):
                # appeared between plan and write - still not a failure
                result["tables"]["plants"]["skipped"] += 1
            else:
                result["tables"]["plants"]["failed"] += 1
                result["errors"].append(f"plants[{src['plant_id']}]: {exc}")

    def _children(table: str, id_key: str, fields) -> None:
        """Insert child rows of ``table`` using mapped target plant ids."""
        matched = plan["tables"][table]["duplicate_in_target"]
        result["tables"][table]["skipped"] = len(matched)
        for src in plan["tables"][table]["to_insert"]:
            new_plant = plant_id_map.get(src["plant_id"])
            if new_plant is None:
                result["tables"][table]["failed"] += 1
                result["errors"].append(
                    f"{table}[{src[id_key]}]: parent plant not migrated")
                continue
            payload = {"plant_id": new_plant, "data_source": src["data_source"]}
            payload.update({k: src.get(v) for k, v in fields.items()})
            try:
                target.insert(table, payload)
                result["tables"][table]["inserted"] += 1
            except Exception as exc:
                if _is_duplicate(exc):
                    result["tables"][table]["skipped"] += 1
                else:
                    result["tables"][table]["failed"] += 1
                    result["errors"].append(
                        f"{table}[{src[id_key]}]: {exc}")

    # -- observations -------------------------------------------------------- #
    _children("observations", "observation_id", {
        "observation_date": "observation_date", "age_days": "age_days",
        "height_cm": "height_cm",
        "growth_rate_cm_per_day": "growth_rate_cm_per_day",
        "canopy_width_cm": "canopy_width_cm",
        "visible_true_leaf_count": "visible_true_leaf_count"})

    # -- readiness events ---------------------------------------------------- #
    _children("readiness_events", "readiness_event_id", {
        "readiness_observed_date": "readiness_observed_date",
        "readiness_observed_age_days": "readiness_observed_age_days",
        "readiness_definition_version": "readiness_definition_version",
        "assessment_source": "assessment_source", "notes": "notes"})

    # -- transplant events + feedback (split from one SQLite table) ---------- #
    fb_matched = plan["tables"]["feedback_14d"]["duplicate_in_target"]
    result["tables"]["feedback_14d"]["skipped"] = len(fb_matched)
    feedback_id_map: dict[int, int] = dict(plan["_feedback_id_map"])
    for src in plan["tables"]["feedback_14d"]["to_insert"]:
        new_plant = plant_id_map.get(src["plant_id"])
        if new_plant is None:
            result["tables"]["feedback_14d"]["failed"] += 1
            result["errors"].append(
                f"feedback_14d[{src['feedback_id']}]: parent plant not "
                "migrated")
            continue
        try:
            trans = target.insert("transplant_events", {
                "plant_id": new_plant,
                "transplant_date": str(src["transplant_date"]),
                "height_at_transplant_cm": src.get("height_at_transplant_cm"),
                "data_source": src["data_source"]})
            fb_id = target.insert("feedback_14d", {
                "plant_id": new_plant,
                "transplant_event_id": trans,
                "feedback_due_date": str(src["feedback_due_date"]),
                "feedback_submitted_date": src.get("feedback_submitted_date"),
                "survived_after_14d": src.get("survived_after_14d"),
                "height_after_14d_cm": src.get("height_after_14d_cm"),
                "growth_after_14d_cm": src.get("growth_after_14d_cm"),
                "visible_condition": src.get("visible_condition"),
                "user_notes": src.get("user_notes"),
                "data_source": src["data_source"]})
            if fb_id is not None:
                feedback_id_map[src["feedback_id"]] = int(fb_id)
            result["tables"]["feedback_14d"]["inserted"] += 1
        except Exception as exc:
            if _is_duplicate(exc):
                result["tables"]["feedback_14d"]["skipped"] += 1
            else:
                result["tables"]["feedback_14d"]["failed"] += 1
                result["errors"].append(
                    f"feedback_14d[{src['feedback_id']}]: {exc}")

    # one transplant event + one feedback row are written per migrated record
    result["tables"]["transplant_events"] = {
        "inserted": result["tables"]["feedback_14d"]["inserted"],
        "skipped": result["tables"]["feedback_14d"]["skipped"],
        "failed": result["tables"]["feedback_14d"]["failed"]}

    # -- corrections --------------------------------------------------------- #
    for src in plan["tables"]["feedback_corrections"]["to_insert"]:
        new_fb = feedback_id_map.get(src["feedback_id"])
        if new_fb is None:
            result["tables"]["feedback_corrections"]["skipped"] += 1
            continue
        try:
            target.insert("feedback_corrections", {
                "feedback_id": new_fb,
                "previous_values": (src["previous_values"]
                                    if isinstance(src["previous_values"], str)
                                    else json.dumps(src["previous_values"])),
                "reason": src.get("reason") or "migrated from SQLite"})
            result["tables"]["feedback_corrections"]["inserted"] += 1
        except Exception as exc:
            if _is_duplicate(exc):
                result["tables"]["feedback_corrections"]["skipped"] += 1
            else:
                result["tables"]["feedback_corrections"]["failed"] += 1
                result["errors"].append(
                    f"feedback_corrections[{src['correction_id']}]: {exc}")

    conn.close()
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Migrate the SQLite prototype database into Supabase "
                    "(dry-run by default; signs in as the destination user, "
                    "no service-role key).")
    p.add_argument("--sqlite", default=str(config.DEFAULT_DB_PATH),
                   help="source SQLite database")
    p.add_argument("--url", help="Supabase project URL")
    p.add_argument("--anon-key", help="Supabase anon (public) key")
    p.add_argument("--email", help="destination account email")
    p.add_argument("--password", help="destination account password")
    p.add_argument("--apply", action="store_true",
                   help="actually write (default is dry-run preview)")
    p.add_argument("--include-synthetic", action="store_true",
                   help="opt-in: also migrate SYNTHETIC DEMO rows "
                        "(they keep data_source='synthetic' and are never "
                        "merged into the real training pool)")
    p.add_argument("--json", action="store_true",
                   help="print the machine-readable result as JSON")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_migration(args)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # unexpected - reported, never swallowed
        print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif args.apply:
        print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
