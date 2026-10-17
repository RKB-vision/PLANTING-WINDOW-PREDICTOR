"""Database repository layer (UI never embeds SQL - it calls these).

A single local backend behind one interface:

* :class:`SQLiteRepository` - wraps :mod:`plantreadiness.db` so every behaviour
  proven by the tests is preserved exactly. Data lives on this device in
  ``plant_readiness.db``.

All reads/writes return plain ``dict`` rows, so services and the front-end stay
storage-agnostic.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

from . import config
from . import db as dbm
from .db import (  # re-exported so callers import errors from one place
    DatabaseError,
    DuplicateRecordError,
    NotFoundError,
    ValidationError,
    compute_growth,
    days_between,
    feedback_due_date,
    parse_iso_date,
)

# --------------------------------------------------------------------------- #
# Errors unique to the repository layer
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Base interface
# --------------------------------------------------------------------------- #


class BaseRepository:
    """The contract every backend implements. All rows are plain dicts."""

    backend: str = "abstract"

    # -- plants ------------------------------------------------------------ #
    def register_plant(self, *, species: str, variety: Optional[str] = None,
                       date_started: Optional[str] = None,
                       data_source: str = "real",
                       growing_conditions: Optional[str] = None,
                       external_ref: Optional[str] = None,
                       cohort: Optional[str] = None) -> int:
        raise NotImplementedError

    def get_plant(self, plant_id: int) -> Optional[dict]:
        raise NotImplementedError

    def list_plants(self, *, species: Optional[str] = None,
                    data_source: Optional[str] = None,
                    search: Optional[str] = None) -> list[dict]:
        raise NotImplementedError

    def count_plants(self) -> int:
        raise NotImplementedError

    # -- observations ------------------------------------------------------ #
    def add_observation(self, *, plant_id: int, observation_date: str,
                        age_days: Optional[int] = None,
                        height_cm: Optional[float] = None,
                        growth_rate_cm_per_day: Optional[float] = None,
                        canopy_width_cm: Optional[float] = None,
                        stem_width_cm: Optional[float] = None,
                        visible_true_leaf_count: Optional[int] = None,
                        data_source: Optional[str] = None,
                        rod_length_cm: Optional[float] = None,
                        calibration_source: Optional[str] = None,
                        calibration_verified: Optional[bool] = None) -> int:
        """Append a daily observation (duplicate plant+date is rejected)."""
        raise NotImplementedError

    def upsert_daily_observation(self, **fields) -> dict:
        """Insert or recompute the single daily observation for (plant, date)."""
        raise NotImplementedError

    def update_observation(self, observation_id: int, updates: dict,
                           reason: Optional[str] = None) -> dict:
        """Apply an audited correction to a daily observation."""
        raise NotImplementedError

    def delete_observation(self, observation_id: int,
                           reason: Optional[str] = None) -> dict:
        """Delete a daily observation, preserving it in the audit trail."""
        raise NotImplementedError

    def observation_revisions(self, observation_id: Optional[int] = None,
                              limit: Optional[int] = None) -> list[dict]:
        """Audit trail of measurement edits."""
        raise NotImplementedError

    def get_observations(self, plant_id: int) -> list[dict]:
        raise NotImplementedError

    def latest_observation(self, plant_id: int) -> Optional[dict]:
        raise NotImplementedError

    def count_observations(self, data_source: Optional[str] = None) -> int:
        raise NotImplementedError

    # -- individual camera captures ----------------------------------------- #
    def add_capture(self, **fields) -> dict:
        """Record (or revise) one image capture, keyed by Cloudinary public id."""
        raise NotImplementedError

    def list_captures(self, plant_id: int,
                      observation_date: Optional[str] = None,
                      data_source: Optional[str] = None) -> list[dict]:
        raise NotImplementedError

    def captures_on_date(self, plant_id: int,
                         observation_date: str) -> list[dict]:
        raise NotImplementedError

    def count_captures(self, data_source: Optional[str] = None) -> int:
        raise NotImplementedError

    def capture_revisions(self, capture_id: Optional[int] = None,
                          limit: Optional[int] = None) -> list[dict]:
        raise NotImplementedError

    # -- rod-length verification -------------------------------------------- #
    def current_calibration_setting(self) -> Optional[dict]:
        """Newest recorded rod-length verification event (None if never)."""
        raise NotImplementedError

    def record_calibration_setting(self, **fields) -> dict:
        raise NotImplementedError

    def calibration_settings_history(self,
                                     limit: Optional[int] = None) -> list[dict]:
        raise NotImplementedError

    # -- readiness events --------------------------------------------------- #
    def record_readiness_event(self, *, plant_id: int,
                               readiness_observed_date: str,
                               readiness_definition_version: str,
                               assessment_source: str,
                               readiness_observed_age_days: Optional[int] = None,
                               data_source: Optional[str] = None,
                               notes: Optional[str] = None) -> int:
        raise NotImplementedError

    def get_readiness_event(
            self, plant_id: int,
            definition_version: Optional[str] = None) -> Optional[dict]:
        raise NotImplementedError

    def list_readiness_events(
            self, plant_id: Optional[int] = None,
            data_source: Optional[str] = None) -> list[dict]:
        raise NotImplementedError

    def readiness_event_counts(self, species: str,
                               variety: Optional[str] = None,
                               data_source: str = "real") -> dict:
        raise NotImplementedError

    def species_cohort_snapshot(self, species: str,
                                variety: Optional[str] = None,
                                observation_date: Optional[str] = None
                                ) -> dict:
        """Raw observations for a species on one date (latest if omitted)."""
        raise NotImplementedError

    # -- transplant + 14-day feedback --------------------------------------- #
    def record_transplant(self, *, plant_id: int, transplant_date: str,
                          height_at_transplant_cm: Optional[float] = None,
                          data_source: Optional[str] = None,
                          transplant_occurred: bool = True,
                          condition_at_transplant: Optional[str] = None,
                          transplant_notes: Optional[str] = None) -> dict:
        raise NotImplementedError

    def get_feedback(self, feedback_id: int) -> Optional[dict]:
        raise NotImplementedError

    def pending_feedback(self, plant_id: Optional[int] = None,
                         as_of: Optional[date] = None) -> list[dict]:
        raise NotImplementedError

    def all_feedback(self, plant_id: Optional[int] = None,
                     data_source: Optional[str] = None) -> list[dict]:
        raise NotImplementedError

    def submit_feedback(self, *, feedback_id: int,
                        feedback_submitted_date: str,
                        survived_after_14d: bool,
                        height_after_14d_cm: Optional[float] = None,
                        visible_condition: Optional[str] = None,
                        user_notes: Optional[str] = None) -> dict:
        raise NotImplementedError

    def update_feedback(self, feedback_id: int, values: dict,
                        expected_submitted: bool = True) -> dict:
        """Apply an audited correction to an existing feedback record.

        Unlike :meth:`submit_feedback` this may run more than once, but only
        on already-submitted rows (``expected_submitted``) and only through
        the service layer, which records the audit entry first.
        """
        raise NotImplementedError

    def feedback_revisions(self, feedback_id: int) -> list[dict]:
        """Audit trail of corrections for one feedback record."""
        raise NotImplementedError

    # -- prediction history + readiness assessments ------------------------- #
    def save_prediction_snapshot(self, **fields) -> dict:
        """Persist an immutable snapshot of a prediction shown to the user."""
        raise NotImplementedError

    def list_prediction_history(self, plant_id: Optional[int] = None,
                                species: Optional[str] = None,
                                data_source: Optional[str] = None,
                                scope: Optional[str] = None,
                                limit: Optional[int] = None) -> list[dict]:
        raise NotImplementedError

    def get_prediction(self, prediction_id: int) -> Optional[dict]:
        raise NotImplementedError

    def record_readiness_assessment(self, **fields) -> dict:
        """Record the user's readiness judgement (not a training label)."""
        raise NotImplementedError

    def list_readiness_assessments(self, plant_id: Optional[int] = None,
                                   species: Optional[str] = None,
                                   data_source: Optional[str] = None
                                   ) -> list[dict]:
        raise NotImplementedError

    # -- reporting ---------------------------------------------------------- #
    def species_data_summary(self, species: str,
                             variety: Optional[str] = None) -> dict:
        raise NotImplementedError

    def dashboard_stats(self) -> dict:
        raise NotImplementedError

    def readiness_counts_by_species(self) -> list[dict]:
        """Per-species readiness-event totals (real + synthetic separated)."""
        raise NotImplementedError

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:  # pragma: no cover - trivial
        pass


# --------------------------------------------------------------------------- #
# SQLite backend (preserves existing CLI behaviour byte-for-byte)
# --------------------------------------------------------------------------- #


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


def _validated_feedback_update(existing: dict, values: dict,
                               transplant_date: str,
                               height_at_transplant: Optional[float]) -> dict:
    """Validate corrected feedback values with the same rules as submit.

    Shared by both backends so a correction can never weaken the original
    validation (dates, condition/survival consistency, non-negative heights).
    """
    sub_raw = values.get("feedback_submitted_date")
    if sub_raw is None:
        raise ValidationError("feedback_submitted_date is required.")
    sub_date = parse_iso_date(sub_raw, "feedback_submitted_date")
    if sub_date < parse_iso_date(transplant_date, "transplant_date"):
        raise ValidationError(
            "feedback_submitted_date cannot be before the transplant_date.")

    survived = values.get("survived_after_14d")
    if survived is None:
        raise ValidationError("survived_after_14d is required.")
    survived = bool(survived)
    condition = values.get("visible_condition")
    if condition is not None and condition not in config.VISIBLE_CONDITIONS:
        raise ValidationError(
            f"visible_condition must be one of {config.VISIBLE_CONDITIONS}.")
    if not survived:
        if condition is None:
            condition = "dead"
        elif condition != "dead":
            raise ValidationError(
                "visible_condition must be 'dead' when "
                "survived_after_14d is false.")
    elif condition == "dead":
        raise ValidationError(
            "visible_condition 'dead' conflicts with survived_after_14d = true.")

    height_after = values.get("height_after_14d_cm")
    if height_after is not None and float(height_after) < 0:
        raise ValidationError("height_after_14d_cm must be >= 0.")

    growth = compute_growth(height_at_transplant, height_after)
    return {
        "feedback_submitted_date": sub_date.isoformat(),
        "survived_after_14d": 1 if survived else 0,
        "height_after_14d_cm": height_after,
        "growth_after_14d_cm": growth,
        "visible_condition": condition,
        "user_notes": values.get("user_notes"),
    }


class SQLiteRepository(BaseRepository):
    """Delegates to the proven :mod:`plantreadiness.db` functions."""

    backend = "sqlite"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- plants ------------------------------------------------------------ #
    def register_plant(self, *, species, variety=None, date_started=None,
                       data_source="real", growing_conditions=None,
                       external_ref=None, cohort=None) -> int:
        return dbm.register_plant(
            self.conn, species=species, variety=variety,
            date_started=date_started, data_source=data_source,
            growing_conditions=growing_conditions, external_ref=external_ref,
            cohort=cohort)

    def get_plant(self, plant_id) -> Optional[dict]:
        try:
            return _row_to_dict(dbm.get_plant(self.conn, plant_id))
        except dbm.NotFoundError:
            return None

    def list_plants(self, *, species=None, data_source=None,
                    search=None) -> list[dict]:
        if search:
            # local development convenience: match species/variety/ref text
            like = f"%{search.strip()}%"
            rows = self.conn.execute(
                """SELECT * FROM plants
                   WHERE species LIKE ? OR variety LIKE ?
                      OR external_ref LIKE ?
                   ORDER BY species, variety, plant_id""",
                (like, like, like)).fetchall()
            return [dict(r) for r in rows]
        return [dict(r) for r in
                dbm.list_plants(self.conn, species=species,
                                data_source=data_source)]

    def count_plants(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM plants").fetchone()[0])

    # -- observations ------------------------------------------------------ #
    def add_observation(self, *, plant_id, observation_date, age_days=None,
                        height_cm=None, growth_rate_cm_per_day=None,
                        canopy_width_cm=None, stem_width_cm=None,
                        visible_true_leaf_count=None,
                        data_source=None,
                        rod_length_cm=None, calibration_source=None,
                        calibration_verified=None) -> int:
        return dbm.add_observation(
            self.conn, plant_id=plant_id,
            observation_date=observation_date, age_days=age_days,
            height_cm=height_cm,
            growth_rate_cm_per_day=growth_rate_cm_per_day,
            canopy_width_cm=canopy_width_cm,
            stem_width_cm=stem_width_cm,
            visible_true_leaf_count=visible_true_leaf_count,
            data_source=data_source, rod_length_cm=rod_length_cm,
            calibration_source=calibration_source,
            calibration_verified=calibration_verified)

    def upsert_daily_observation(self, **fields) -> dict:
        return dbm.upsert_daily_observation(self.conn, **fields)

    def update_observation(self, observation_id, updates, reason=None) -> dict:
        return dbm.update_observation(self.conn, observation_id, updates,
                                      reason=reason)

    def delete_observation(self, observation_id, reason=None) -> dict:
        return dbm.delete_observation(self.conn, observation_id, reason=reason)

    def observation_revisions(self, observation_id=None, limit=None) -> list[dict]:
        return dbm.observation_revisions(self.conn, observation_id, limit)

    # -- individual camera captures ----------------------------------------- #
    def add_capture(self, **fields) -> dict:
        return dbm.add_capture(self.conn, **fields)

    def list_captures(self, plant_id, observation_date=None,
                      data_source=None) -> list[dict]:
        return dbm.list_captures(self.conn, plant_id,
                                 observation_date=observation_date,
                                 data_source=data_source)

    def captures_on_date(self, plant_id, observation_date) -> list[dict]:
        return dbm.captures_on_date(self.conn, plant_id, observation_date)

    def count_captures(self, data_source=None) -> int:
        return dbm.count_captures(self.conn, data_source)

    def capture_revisions(self, capture_id=None, limit=None) -> list[dict]:
        return dbm.capture_revisions(self.conn, capture_id, limit)

    # -- rod-length verification -------------------------------------------- #
    def current_calibration_setting(self):
        return dbm.current_calibration_setting(self.conn)

    def record_calibration_setting(self, **fields) -> dict:
        return dbm.record_calibration_setting(self.conn, **fields)

    def calibration_settings_history(self, limit=None) -> list[dict]:
        return dbm.calibration_settings_history(self.conn, limit)

    def get_observations(self, plant_id) -> list[dict]:
        return [dict(r) for r in dbm.get_observations(self.conn, plant_id)]

    def latest_observation(self, plant_id) -> Optional[dict]:
        return _row_to_dict(dbm.latest_observation(self.conn, plant_id))

    def count_observations(self, data_source=None) -> int:
        if data_source:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM observations WHERE data_source = ?",
                (data_source,)).fetchone()[0])
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0])

    # -- readiness events --------------------------------------------------- #
    def record_readiness_event(self, *, plant_id, readiness_observed_date,
                               readiness_definition_version,
                               assessment_source,
                               readiness_observed_age_days=None,
                               data_source=None, notes=None) -> int:
        return dbm.record_readiness_event(
            self.conn, plant_id=plant_id,
            readiness_observed_date=readiness_observed_date,
            readiness_definition_version=readiness_definition_version,
            assessment_source=assessment_source,
            readiness_observed_age_days=readiness_observed_age_days,
            data_source=data_source, notes=notes)

    def get_readiness_event(self, plant_id,
                            definition_version=None) -> Optional[dict]:
        return _row_to_dict(dbm.get_readiness_event(
            self.conn, plant_id, definition_version))

    def list_readiness_events(self, plant_id=None, data_source=None) -> list[dict]:
        sql = """SELECT r.*, p.species, p.variety, p.external_ref
                 FROM readiness_events r
                 JOIN plants p ON p.plant_id = r.plant_id
                 WHERE 1=1"""
        args = []
        if plant_id is not None:
            sql += " AND r.plant_id = ?"
            args.append(plant_id)
        if data_source is not None:
            sql += " AND r.data_source = ?"
            args.append(data_source)
        sql += " ORDER BY r.readiness_observed_date DESC, r.readiness_event_id DESC"
        return [dict(row) for row in self.conn.execute(sql, args)]

    def readiness_event_counts(self, species, variety=None,
                               data_source="real") -> dict:
        return dbm.readiness_event_counts(self.conn, species, variety,
                                          data_source)

    def species_cohort_snapshot(self, species, variety=None,
                                observation_date=None) -> dict:
        return dbm.species_cohort_snapshot(self.conn, species, variety,
                                           observation_date)

    # -- transplant + 14-day feedback --------------------------------------- #
    def record_transplant(self, *, plant_id, transplant_date,
                          height_at_transplant_cm=None,
                          data_source=None, transplant_occurred=True,
                          condition_at_transplant=None,
                          transplant_notes=None) -> dict:
        return dbm.record_transplant(
            self.conn, plant_id=plant_id, transplant_date=transplant_date,
            height_at_transplant_cm=height_at_transplant_cm,
            data_source=data_source,
            transplant_occurred=transplant_occurred,
            condition_at_transplant=condition_at_transplant,
            transplant_notes=transplant_notes)

    def get_feedback(self, feedback_id) -> Optional[dict]:
        try:
            return _row_to_dict(dbm.get_feedback(self.conn, feedback_id))
        except dbm.NotFoundError:
            return None

    def pending_feedback(self, plant_id=None, as_of=None) -> list[dict]:
        return dbm.pending_feedback(self.conn, plant_id=plant_id, as_of=as_of)

    def all_feedback(self, plant_id=None, data_source=None) -> list[dict]:
        return dbm.feedback_history(self.conn, plant_id=plant_id,
                                    data_source=data_source)

    def submit_feedback(self, *, feedback_id, feedback_submitted_date,
                        survived_after_14d, height_after_14d_cm=None,
                        visible_condition=None, user_notes=None) -> dict:
        return dbm.submit_feedback(
            self.conn, feedback_id=feedback_id,
            feedback_submitted_date=feedback_submitted_date,
            survived_after_14d=survived_after_14d,
            height_after_14d_cm=height_after_14d_cm,
            visible_condition=visible_condition, user_notes=user_notes)

    def update_feedback(self, feedback_id, values,
                        expected_submitted=True) -> dict:
        row = dbm.get_feedback(self.conn, feedback_id)  # raises NotFoundError
        if expected_submitted and row["feedback_submitted_date"] is None:
            raise ValidationError(
                "Only submitted feedback can be corrected - submit it first.")
        cleaned = _validated_feedback_update(
            dict(row), values, transplant_date=row["transplant_date"],
            height_at_transplant=row["height_at_transplant_cm"])
        self.conn.execute(
            """UPDATE feedback_14d
               SET feedback_submitted_date = ?, survived_after_14d = ?,
                   height_after_14d_cm = ?, growth_after_14d_cm = ?,
                   visible_condition = ?, user_notes = ?
               WHERE feedback_id = ?""",
            (cleaned["feedback_submitted_date"],
             cleaned["survived_after_14d"], cleaned["height_after_14d_cm"],
             cleaned["growth_after_14d_cm"], cleaned["visible_condition"],
             cleaned["user_notes"], feedback_id))
        self.conn.commit()
        return dict(dbm.get_feedback(self.conn, feedback_id))

    # -- reporting ---------------------------------------------------------- #
    def species_data_summary(self, species, variety=None) -> dict:
        return dbm.species_data_summary(self.conn, species, variety)

    def dashboard_stats(self) -> dict:
        c = self.conn
        n_plants = int(c.execute("SELECT COUNT(*) FROM plants").fetchone()[0])
        n_obs = int(c.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0])
        n_fb = int(c.execute(
            "SELECT COUNT(*) FROM feedback_14d").fetchone()[0])
        n_fb_done = int(c.execute(
            "SELECT COUNT(*) FROM feedback_14d "
            "WHERE feedback_submitted_date IS NOT NULL").fetchone()[0])
        pending = dbm.pending_feedback(self.conn)
        return {
            "backend": self.backend,
            "n_plants": n_plants,
            "n_observations": n_obs,
            "n_feedback_total": n_fb,
            "n_feedback_completed": n_fb_done,
            "n_feedback_pending": len(pending),
            "n_feedback_overdue": sum(1 for p in pending if p["is_overdue"]),
            "pending_feedback": pending,
        }

    def readiness_counts_by_species(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT p.species,
                      p.data_source,
                      COUNT(DISTINCT p.plant_id) AS n_plants,
                      COUNT(DISTINCT r.plant_id) AS n_plants_with_event,
                      COUNT(r.readiness_event_id) AS n_events
               FROM plants p
               LEFT JOIN readiness_events r ON r.plant_id = p.plant_id
               GROUP BY p.species, p.data_source
               ORDER BY p.species, p.data_source""").fetchall()
        return [dict(r) for r in rows]

    # -- audit (corrections) ------------------------------------------------- #
    def ensure_feedback_revisions_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback_corrections (
                correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_id  INTEGER NOT NULL,
                previous_values TEXT NOT NULL,
                corrected_at TEXT NOT NULL DEFAULT (datetime('now')),
                reason       TEXT
            )""")
        self.conn.commit()

    def record_feedback_correction(self, feedback_id: int,
                                   previous_values: dict,
                                   reason: Optional[str] = None) -> int:
        """Persist an audit entry for a corrected feedback outcome."""
        import json
        self.ensure_feedback_revisions_table()
        cur = self.conn.execute(
            """INSERT INTO feedback_corrections
               (feedback_id, previous_values, reason)
               VALUES (?, ?, ?)""",
            (feedback_id, json.dumps(previous_values, default=str), reason))
        self.conn.commit()
        return int(cur.lastrowid)

    def feedback_revisions(self, feedback_id) -> list[dict]:
        self.ensure_feedback_revisions_table()
        rows = self.conn.execute(
            """SELECT * FROM feedback_corrections
               WHERE feedback_id = ? ORDER BY corrected_at, correction_id""",
            (feedback_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- prediction history + readiness assessments ------------------------- #
    def save_prediction_snapshot(self, **fields) -> dict:
        return dbm.save_prediction_snapshot(self.conn, **fields)

    def list_prediction_history(self, plant_id=None, species=None,
                                data_source=None, scope=None,
                                limit=None) -> list[dict]:
        return dbm.list_prediction_history(
            self.conn, plant_id=plant_id, species=species,
            data_source=data_source, scope=scope, limit=limit)

    def get_prediction(self, prediction_id) -> Optional[dict]:
        try:
            return dbm.get_prediction(self.conn, prediction_id)
        except dbm.NotFoundError:
            return None

    def record_readiness_assessment(self, **fields) -> dict:
        return dbm.record_readiness_assessment(self.conn, **fields)

    def list_readiness_assessments(self, plant_id=None, species=None,
                                   data_source=None) -> list[dict]:
        return dbm.list_readiness_assessments(
            self.conn, plant_id=plant_id, species=species,
            data_source=data_source)


    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - already closed
            pass


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def create_sqlite_repository(db_path=None) -> SQLiteRepository:
    """Open (and initialise) the local SQLite database."""
    conn = dbm.init_db(db_path)
    return SQLiteRepository(conn)


