"""Database repository layer (UI never embeds SQL - it calls these).

Two interchangeable backends behind one interface:

* :class:`SQLiteRepository` - wraps the existing :mod:`plantreadiness.db`
  functions so every behaviour proven by the 82 CLI tests is preserved
  exactly. Used for local development, the migration source and tests.
* :class:`SupabaseRepository` - Supabase/PostgREST client scoped to one
  authenticated user. Row Level Security is the enforcement layer; every
  query *also* filters by ``user_id`` as defence in depth.

Both return plain ``dict`` rows with the same keys, so services and the UI
are backend-agnostic.

Ownership model
---------------
Supabase tables carry ``user_id uuid`` and RLS policies constrain every
read/write to ``auth.uid()``. The SQLite backend is single-user by design
(local development fallback) and performs no ownership checks.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Optional

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


class ConfigurationError(Exception):
    """Secrets/config missing or invalid - show setup help, never guess."""


class ConnectionFailedError(Exception):
    """Could not reach the configured database backend."""


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
                        visible_true_leaf_count: Optional[int] = None,
                        data_source: Optional[str] = None) -> int:
        raise NotImplementedError

    def get_observations(self, plant_id: int) -> list[dict]:
        raise NotImplementedError

    def latest_observation(self, plant_id: int) -> Optional[dict]:
        raise NotImplementedError

    def count_observations(self, data_source: Optional[str] = None) -> int:
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

    # -- transplant + 14-day feedback --------------------------------------- #
    def record_transplant(self, *, plant_id: int, transplant_date: str,
                          height_at_transplant_cm: Optional[float] = None,
                          data_source: Optional[str] = None) -> dict:
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
                        canopy_width_cm=None, visible_true_leaf_count=None,
                        data_source=None) -> int:
        return dbm.add_observation(
            self.conn, plant_id=plant_id,
            observation_date=observation_date, age_days=age_days,
            height_cm=height_cm,
            growth_rate_cm_per_day=growth_rate_cm_per_day,
            canopy_width_cm=canopy_width_cm,
            visible_true_leaf_count=visible_true_leaf_count,
            data_source=data_source)

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

    # -- transplant + 14-day feedback --------------------------------------- #
    def record_transplant(self, *, plant_id, transplant_date,
                          height_at_transplant_cm=None,
                          data_source=None) -> dict:
        return dbm.record_transplant(
            self.conn, plant_id=plant_id, transplant_date=transplant_date,
            height_at_transplant_cm=height_at_transplant_cm,
            data_source=data_source)

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


    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - already closed
            pass


# --------------------------------------------------------------------------- #
# Supabase backend
# --------------------------------------------------------------------------- #


class SupabaseRepository(BaseRepository):
    """PostgREST-backed repository scoped to one authenticated user.

    ``user_id`` is stamped on every write and filtered on every read, so the
    app never depends on a caller remembering to scope a query. RLS policies
    in ``supabase/migrations`` enforce the same rule at the database layer.
    """

    backend = "supabase"

    def __init__(self, client: Any, user_id: str):
        if not user_id:
            raise ConfigurationError(
                "SupabaseRepository requires an authenticated user id.")
        self.client = client
        self.user_id = str(user_id)

    # -- internal helpers --------------------------------------------------- #
    def _table(self, name: str):
        try:
            return self.client.table(name)
        except Exception as exc:  # network failure surfaces here first
            raise _translate_supabase_error(exc) from exc

    def _owned(self, name: str, **filters):
        """A select pre-scoped to this user (+ optional exact filters)."""
        try:
            q = self._table(name).select("*").eq("user_id", self.user_id)
            for col, val in filters.items():
                if val is not None:
                    q = q.eq(col, val)
            return q
        except Exception as exc:
            raise _translate_supabase_error(exc) from exc

    @staticmethod
    def _run(builder):
        """Execute a postgrest builder, translating transport failures."""
        try:
            return builder.execute()
        except Exception as exc:
            raise _translate_supabase_error(exc) from exc

    def _data(self, query_or_response) -> list[dict]:
        """Return rows from a postgrest builder *or* an executed response.

        Builders are executed lazily here so every call site can pass the
        chained query directly (``self._data(self._owned(...))``) while
        already-executed responses pass through untouched. Transport and
        policy failures are translated to repository errors.
        """
        obj = query_or_response
        execute = getattr(obj, "execute", None)
        if callable(execute) and not hasattr(obj, "data"):
            try:
                obj = execute()
            except Exception as exc:
                raise _translate_supabase_error(exc) from exc
        return list(getattr(obj, "data", None) or [])

    def _insert(self, name: str, row: dict) -> dict:
        row = {**row, "user_id": self.user_id}
        try:
            resp = self._table(name).insert(row).execute()
        except Exception as exc:  # network / constraint violation
            raise _translate_supabase_error(exc) from exc
        data = self._data(resp)
        return data[0] if data else row

    # -- plants ------------------------------------------------------------ #
    def register_plant(self, *, species, variety=None, date_started=None,
                       data_source="real", growing_conditions=None,
                       external_ref=None, cohort=None) -> int:
        species = (species or "").strip()
        if not species:
            raise ValidationError("species is required.")
        if data_source not in config.VALID_SOURCES:
            raise ValidationError(
                f"data_source must be one of {config.VALID_SOURCES}, "
                f"got '{data_source}'.")
        if date_started:
            parse_iso_date(date_started, "date_started")
        variety_norm = variety.strip() if variety else None

        q = self._owned("plants", species=species, data_source=data_source,
                        variety=variety_norm, external_ref=external_ref)
        if self._data(q):
            raise DuplicateRecordError(
                f"Plant already registered: species={species!r} "
                f"variety={variety_norm!r} source={data_source!r} "
                f"ref={external_ref!r}")

        row = self._insert("plants", {
            "species": species,
            "variety": variety_norm,
            "date_started": date_started,
            "data_source": data_source,
            "growing_conditions": growing_conditions,
            "external_ref": external_ref,
            "cohort": cohort,
        })
        return int(row["plant_id"])

    def get_plant(self, plant_id) -> Optional[dict]:
        data = self._data(self._owned("plants").eq(
            "plant_id", plant_id).limit(1))
        return data[0] if data else None

    def list_plants(self, *, species=None, data_source=None,
                    search=None) -> list[dict]:
        q = self._owned("plants")
        if species:
            q = q.eq("species", species)
        if data_source:
            q = q.eq("data_source", data_source)
        data = self._data(q.order("species").order("variety").order(
            "plant_id"))
        if search:
            needle = search.strip().lower()
            data = [p for p in data if needle in (
                (p.get("species") or "") + " " + (p.get("variety") or "")
                + " " + (p.get("external_ref") or "")).lower()]
        return data

    def count_plants(self) -> int:
        return len(self._data(self._owned("plants").select("plant_id")))

    # -- observations ------------------------------------------------------ #
    def add_observation(self, *, plant_id, observation_date, age_days=None,
                        height_cm=None, growth_rate_cm_per_day=None,
                        canopy_width_cm=None, visible_true_leaf_count=None,
                        data_source=None) -> int:
        plant = self.get_plant(plant_id)
        if plant is None:
            raise NotFoundError(f"No plant with plant_id={plant_id}.")
        obs_date = parse_iso_date(observation_date, "observation_date")
        source = data_source or plant["data_source"]
        if source not in config.VALID_SOURCES:
            raise ValidationError(f"Invalid data_source '{source}'.")
        if source != plant["data_source"]:
            raise ValidationError(
                f"Observation data_source '{source}' does not match plant "
                f"data_source '{plant['data_source']}'.")

        if age_days is not None and int(age_days) < 0:
            raise ValidationError("age_days must be >= 0.")
        if height_cm is not None and float(height_cm) < 0:
            raise ValidationError("height_cm must be >= 0.")
        if canopy_width_cm is not None and float(canopy_width_cm) < 0:
            raise ValidationError("canopy_width_cm must be >= 0.")
        if visible_true_leaf_count is not None \
                and int(visible_true_leaf_count) < 0:
            raise ValidationError("visible_true_leaf_count must be >= 0.")
        if growth_rate_cm_per_day is not None:
            growth_rate_cm_per_day = float(growth_rate_cm_per_day)

        if age_days is None and plant.get("date_started"):
            age_days = days_between(plant["date_started"], obs_date)
            if age_days < 0:
                raise ValidationError(
                    "observation_date is before the plant's date_started.")

        dup = self._data(self._owned(
            "observations", plant_id=plant_id,
            observation_date=obs_date.isoformat()))
        if dup:
            raise DuplicateRecordError(
                f"Observation for plant {plant_id} on {obs_date} already "
                "exists - historical observations are never overwritten.")

        row = self._insert("observations", {
            "plant_id": plant_id,
            "observation_date": obs_date.isoformat(),
            "age_days": age_days,
            "height_cm": height_cm,
            "growth_rate_cm_per_day": growth_rate_cm_per_day,
            "canopy_width_cm": canopy_width_cm,
            "visible_true_leaf_count": visible_true_leaf_count,
            "data_source": source,
        })
        return int(row["observation_id"])

    def get_observations(self, plant_id) -> list[dict]:
        data = self._data(self._owned(
            "observations", plant_id=plant_id).order(
                "observation_date").order("observation_id"))
        return data

    def latest_observation(self, plant_id) -> Optional[dict]:
        data = self._data(self._owned(
            "observations", plant_id=plant_id).order(
                "observation_date", desc=True).order(
                    "observation_id", desc=True).limit(1))
        return data[0] if data else None

    def count_observations(self, data_source=None) -> int:
        q = self._owned("observations")
        if data_source:
            q = q.eq("data_source", data_source)
        return len(self._data(q.select("observation_id")))

    # -- readiness events --------------------------------------------------- #
    def record_readiness_event(self, *, plant_id, readiness_observed_date,
                               readiness_definition_version,
                               assessment_source,
                               readiness_observed_age_days=None,
                               data_source=None, notes=None) -> int:
        plant = self.get_plant(plant_id)
        if plant is None:
            raise NotFoundError(f"No plant with plant_id={plant_id}.")
        rdate = parse_iso_date(readiness_observed_date,
                               "readiness_observed_date")
        source = data_source or plant["data_source"]
        if source != plant["data_source"]:
            raise ValidationError(
                "readiness event data_source must match the plant's "
                "data_source.")
        if not readiness_definition_version \
                or not readiness_definition_version.strip():
            raise ValidationError(
                "readiness_definition_version is required - readiness must "
                "follow a documented assessment definition, not an arbitrary "
                "label.")
        if not assessment_source or not assessment_source.strip():
            raise ValidationError("assessment_source is required.")
        if readiness_observed_age_days is None and plant.get("date_started"):
            readiness_observed_age_days = days_between(
                plant["date_started"], rdate)
        if readiness_observed_age_days is not None \
                and int(readiness_observed_age_days) < 0:
            raise ValidationError("readiness_observed_age_days must be >= 0.")

        dup = self._data(self._owned(
            "readiness_events", plant_id=plant_id,
            readiness_definition_version=readiness_definition_version.strip()))
        if dup:
            raise DuplicateRecordError(
                f"Readiness event already recorded for plant {plant_id} "
                f"under definition '{readiness_definition_version}'.")

        row = self._insert("readiness_events", {
            "plant_id": plant_id,
            "readiness_observed_date": rdate.isoformat(),
            "readiness_observed_age_days": readiness_observed_age_days,
            "readiness_definition_version": readiness_definition_version.strip(),
            "assessment_source": assessment_source.strip(),
            "data_source": source,
            "notes": notes,
        })
        return int(row["readiness_event_id"])

    def get_readiness_event(self, plant_id,
                            definition_version=None) -> Optional[dict]:
        q = self._owned("readiness_events", plant_id=plant_id)
        if definition_version:
            q = q.eq("readiness_definition_version", definition_version)
            data = self._data(q.limit(1))
        else:
            data = self._data(q.order("readiness_observed_date").limit(1))
        return data[0] if data else None

    def list_readiness_events(self, plant_id=None, data_source=None) -> list[dict]:
        q = self._owned("readiness_events")
        if plant_id is not None:
            q = q.eq("plant_id", plant_id)
        if data_source is not None:
            q = q.eq("data_source", data_source)
        data = self._data(q.order("readiness_observed_date", desc=True))
        if not data:
            return []
        plant_ids = {r["plant_id"] for r in data}
        plants = {p["plant_id"]: p for p in self._data(
            self._owned("plants").in_("plant_id", list(plant_ids)))}
        out = []
        for r in data:
            p = plants.get(r["plant_id"], {})
            out.append({
                **r,
                "species": p.get("species"),
                "variety": p.get("variety"),
                "external_ref": p.get("external_ref"),
            })
        return out

    def readiness_event_counts(self, species, variety=None,
                               data_source="real") -> dict:
        q = self._owned("plants", species=species, data_source=data_source)
        if variety:
            q = q.eq("variety", variety)
        plants = self._data(q.select("plant_id"))
        ids = [p["plant_id"] for p in plants]
        events = []
        if ids:
            events = self._data(self._owned("readiness_events").in_(
                "plant_id", ids).select("readiness_event_id, plant_id"))
        return {
            "independent_plants": len({e["plant_id"] for e in events}),
            "readiness_events": len(events),
            "species": species, "variety": variety,
            "data_source": data_source,
        }

    # -- transplant + 14-day feedback --------------------------------------- #
    def record_transplant(self, *, plant_id, transplant_date,
                          height_at_transplant_cm=None,
                          data_source=None) -> dict:
        plant = self.get_plant(plant_id)
        if plant is None:
            raise NotFoundError(f"No plant with plant_id={plant_id}.")
        tdate = parse_iso_date(transplant_date, "transplant_date")
        source = data_source or plant["data_source"]
        if source != plant["data_source"]:
            raise ValidationError(
                "transplant data_source must match the plant's.")
        if height_at_transplant_cm is not None \
                and float(height_at_transplant_cm) < 0:
            raise ValidationError("height_at_transplant_cm must be >= 0.")

        dup = self._data(self._owned(
            "transplant_events", plant_id=plant_id,
            transplant_date=tdate.isoformat()))
        if dup:
            raise DuplicateRecordError(
                f"Transplant already recorded for plant {plant_id} "
                f"on {tdate}.")

        ev = self._insert("transplant_events", {
            "plant_id": plant_id,
            "transplant_date": tdate.isoformat(),
            "height_at_transplant_cm": height_at_transplant_cm,
            "data_source": source,
        })
        due = feedback_due_date(tdate)
        fb = self._insert("feedback_14d", {
            "plant_id": plant_id,
            "transplant_event_id": ev["transplant_event_id"],
            "feedback_due_date": due.isoformat(),
            "data_source": source,
        })
        return {
            "feedback_id": int(fb["feedback_id"]),
            "transplant_event_id": int(ev["transplant_event_id"]),
            "plant_id": plant_id,
            "transplant_date": tdate.isoformat(),
            "feedback_due_date": due.isoformat(),
            "height_at_transplant_cm": height_at_transplant_cm,
            "data_source": source,
        }

    def get_feedback(self, feedback_id) -> Optional[dict]:
        data = self._data(self._owned("feedback_14d").eq(
            "feedback_id", feedback_id).limit(1))
        return data[0] if data else None

    def _feedback_with_plant(self, submitted: Optional[bool],
                             plant_id: Optional[int] = None) -> list[dict]:
        """Feedback rows joined with plant + transplant data for display.

        The Supabase schema normalises transplant events into their own
        table, so the joined view re-adds ``transplant_date`` and
        ``height_at_transplant_cm`` to keep one uniform row shape across
        backends (the CLI and services read those keys directly).
        """
        q = self._owned("feedback_14d")
        if plant_id is not None:
            q = q.eq("plant_id", plant_id)
        data = self._data(q.order("feedback_due_date"))
        if submitted is False:
            data = [r for r in data if r.get("feedback_submitted_date") is None]
        elif submitted is True:
            data = [r for r in data if r.get("feedback_submitted_date")]
        if not data:
            return []
        plant_ids = {r["plant_id"] for r in data}
        plants = {p["plant_id"]: p for p in self._data(
            self._owned("plants").in_("plant_id", list(plant_ids)))}
        transplant_ids = [r["transplant_event_id"] for r in data
                          if r.get("transplant_event_id") is not None]
        transplants = {t["transplant_event_id"]: t for t in (
            self._data(self._owned("transplant_events").in_(
                "transplant_event_id", transplant_ids))
            if transplant_ids else [])}
        out = []
        today = date.today()
        for r in data:
            plant = plants.get(r["plant_id"], {})
            trans = transplants.get(r.get("transplant_event_id"), {})
            merged = {**r,
                      "species": plant.get("species"),
                      "variety": plant.get("variety"),
                      "transplant_date": trans.get("transplant_date"),
                      "height_at_transplant_cm": trans.get(
                          "height_at_transplant_cm")}
            try:
                due = parse_iso_date(r["feedback_due_date"])
                overdue = (today - due).days
                merged["is_overdue"] = overdue > 0
                merged["days_overdue"] = max(overdue, 0)
            except Exception:
                merged["is_overdue"] = False
                merged["days_overdue"] = 0
            out.append(merged)
        return out

    def pending_feedback(self, plant_id=None, as_of=None) -> list[dict]:
        rows = self._feedback_with_plant(submitted=False, plant_id=plant_id)
        if as_of is not None:
            for r in rows:
                due = parse_iso_date(r["feedback_due_date"])
                overdue = (as_of - due).days
                r["is_overdue"] = overdue > 0
                r["days_overdue"] = max(overdue, 0)
        return rows

    def all_feedback(self, plant_id=None, data_source=None) -> list[dict]:
        rows = self._feedback_with_plant(submitted=None, plant_id=plant_id)
        if data_source:
            rows = [r for r in rows if r.get("data_source") == data_source]
        return rows

    def _transplant_height(self, feedback_row: dict) -> Optional[float]:
        tid = feedback_row.get("transplant_event_id")
        if tid is None:
            return None
        data = self._data(self._owned("transplant_events").eq(
            "transplant_event_id", tid).limit(1))
        return data[0].get("height_at_transplant_cm") if data else None

    def submit_feedback(self, *, feedback_id, feedback_submitted_date,
                        survived_after_14d, height_after_14d_cm=None,
                        visible_condition=None, user_notes=None) -> dict:
        row = self.get_feedback(feedback_id)
        if row is None:
            raise NotFoundError(
                f"No feedback record with feedback_id={feedback_id}.")
        if row.get("feedback_submitted_date") is not None:
            raise DuplicateRecordError(
                f"Feedback {feedback_id} was already submitted on "
                f"{row['feedback_submitted_date']} - outcomes are immutable. "
                "Use an explicit correction to amend it (audit trail kept).")

        sub_date = parse_iso_date(feedback_submitted_date,
                                  "feedback_submitted_date")
        transplant = parse_iso_date(self._transplant_event_date(row))
        if sub_date < transplant:
            raise ValidationError(
                "feedback_submitted_date cannot be before the transplant_date.")
        if visible_condition is not None \
                and visible_condition not in config.VISIBLE_CONDITIONS:
            raise ValidationError(
                f"visible_condition must be one of "
                f"{config.VISIBLE_CONDITIONS}.")

        survived = 1 if survived_after_14d else 0
        if not survived_after_14d:
            if visible_condition is None:
                visible_condition = "dead"
            elif visible_condition != "dead":
                raise ValidationError(
                    "visible_condition must be 'dead' when "
                    "survived_after_14d is false.")
        elif visible_condition == "dead":
            raise ValidationError(
                "visible_condition 'dead' conflicts with "
                "survived_after_14d = true.")
        if height_after_14d_cm is not None \
                and float(height_after_14d_cm) < 0:
            raise ValidationError("height_after_14d_cm must be >= 0.")

        height_at = self._transplant_height(row)
        growth = compute_growth(height_at, height_after_14d_cm)

        try:
            resp = self._table("feedback_14d").update({
                "feedback_submitted_date": sub_date.isoformat(),
                "survived_after_14d": survived,
                "height_after_14d_cm": height_after_14d_cm,
                "growth_after_14d_cm": growth,
                "visible_condition": visible_condition,
                "user_notes": user_notes,
            }).eq("feedback_id", feedback_id).eq(
                "user_id", self.user_id).execute()
        except Exception as exc:
            raise _translate_supabase_error(exc) from exc
        data = self._data(resp)
        return data[0] if data else {**row, "feedback_submitted_date":
                                     sub_date.isoformat()}

    def _transplant_event_date(self, feedback_row: dict) -> str:
        tid = feedback_row.get("transplant_event_id")
        if tid is None:
            raise ValidationError("feedback record has no transplant event.")
        data = self._data(self._owned("transplant_events").eq(
            "transplant_event_id", tid).limit(1))
        if not data:
            raise NotFoundError("linked transplant event not found.")
        return data[0]["transplant_date"]

    def update_feedback(self, feedback_id, values,
                        expected_submitted=True) -> dict:
        row = self.get_feedback(feedback_id)
        if row is None:
            raise NotFoundError(
                f"No feedback record with feedback_id={feedback_id}.")
        if expected_submitted and row.get("feedback_submitted_date") is None:
            raise ValidationError(
                "Only submitted feedback can be corrected - submit it first.")
        cleaned = _validated_feedback_update(
            row, values,
            transplant_date=self._transplant_event_date(row),
            height_at_transplant=self._transplant_height(row))
        try:
            resp = self._table("feedback_14d").update(cleaned).eq(
                "feedback_id", feedback_id).eq(
                    "user_id", self.user_id).execute()
        except Exception as exc:
            raise _translate_supabase_error(exc) from exc
        data = self._data(resp)
        return data[0] if data else {**row, **cleaned}

    # -- corrections (audit trail) ------------------------------------------ #
    def record_feedback_correction(self, feedback_id: int,
                                   previous_values: dict,
                                   reason: Optional[str] = None) -> int:
        import json
        row = self._insert("feedback_corrections", {
            "feedback_id": feedback_id,
            "previous_values": json.dumps(previous_values, default=str),
            "reason": reason,
        })
        return int(row["correction_id"])

    def feedback_revisions(self, feedback_id) -> list[dict]:
        return self._data(self._owned(
            "feedback_corrections", feedback_id=feedback_id).order(
                "corrected_at"))

    # -- reporting ---------------------------------------------------------- #
    def species_data_summary(self, species, variety=None) -> dict:
        summary = {}
        for source in config.VALID_SOURCES:
            q = self._owned("plants", species=species, data_source=source)
            if variety:
                q = q.eq("variety", variety)
            plants = self._data(q.select("plant_id"))
            ids = [p["plant_id"] for p in plants]
            obs_count = 0
            if ids:
                obs = self._data(self._owned("observations").in_(
                    "plant_id", ids).select("observation_id"))
                obs_count = len(obs)
            summary[source] = {"n_plants": len(ids),
                               "n_observations": obs_count}
        real_counts = self.readiness_event_counts(
            species, variety, data_source="real")
        syn_counts = self.readiness_event_counts(
            species, variety, data_source="synthetic")
        return {
            "species": species, "variety": variety,
            "plants": {k: v["n_plants"] for k, v in summary.items()},
            "observations": {k: v["n_observations"] for k, v in summary.items()},
            "real_readiness_events": real_counts,
            "synthetic_readiness_events": syn_counts,
        }

    def dashboard_stats(self) -> dict:
        plants = self._data(self._owned("plants"))
        obs = self._data(self._owned("observations").select("observation_id"))
        fb = self._data(self._owned("feedback_14d"))
        pending = self.pending_feedback()
        done = [r for r in fb if r.get("feedback_submitted_date")]
        return {
            "backend": self.backend,
            "n_plants": len(plants),
            "n_observations": len(obs),
            "n_feedback_total": len(fb),
            "n_feedback_completed": len(done),
            "n_feedback_pending": len(pending),
            "n_feedback_overdue": sum(1 for p in pending if p["is_overdue"]),
            "pending_feedback": pending,
        }

    def readiness_counts_by_species(self) -> list[dict]:
        plants = self._data(self._owned("plants"))
        events = self._data(self._owned("readiness_events").select(
            "readiness_event_id, plant_id"))
        ev_by_plant = {}
        for e in events:
            ev_by_plant.setdefault(e["plant_id"], 0)
            ev_by_plant[e["plant_id"]] += 1
        agg: dict[tuple, dict] = {}
        for p in plants:
            key = (p["species"], p["data_source"])
            slot = agg.setdefault(key, {
                "species": p["species"], "data_source": p["data_source"],
                "n_plants": 0, "n_plants_with_event": 0, "n_events": 0})
            slot["n_plants"] += 1
            n = ev_by_plant.get(p["plant_id"], 0)
            if n:
                slot["n_plants_with_event"] += 1
                slot["n_events"] += n
        return sorted(agg.values(), key=lambda r: (r["species"],
                                                   r["data_source"]))


# --------------------------------------------------------------------------- #
# Error translation + factory
# --------------------------------------------------------------------------- #


def _translate_supabase_error(exc: Exception) -> Exception:
    """Map PostgREST/HTTP failures to the repository's error vocabulary."""
    text = str(exc)
    lowered = text.lower()
    if "duplicate key" in lowered or "unique constraint" in lowered \
            or "23505" in text:
        return DuplicateRecordError(
            f"Duplicate record rejected by the database: {text}")
    if "row-level security" in lowered or "42501" in text:
        return DatabaseError(
            "Row-level security policy rejected this write - the record "
            "does not belong to this user or the policy is missing.")
    if "failed to fetch" in lowered or "connection" in lowered \
            or "timeout" in lowered or "name or service not known" in lowered \
            or "refused" in lowered:
        return ConnectionFailedError(
            f"Could not reach Supabase: {text}")
    return DatabaseError(f"Database error: {text}")


def create_sqlite_repository(db_path=None) -> SQLiteRepository:
    """Open (and initialise) the local SQLite database."""
    conn = dbm.init_db(db_path)
    return SQLiteRepository(conn)


def create_supabase_repository(client: Any, user_id: str) -> SupabaseRepository:
    return SupabaseRepository(client, user_id)
