"""SQLite schema and data-access layer.

Design rules (see task sections 4 and 9):

* primary keys, foreign keys, CHECK constraints and uniqueness for
  duplicate protection,
* historical observations and feedback are never overwritten - inserts are
  append-only and duplicates are rejected,
* every row carries ``data_source`` ('real' or 'synthetic') so the two
  worlds can never mix,
* date arithmetic (feedback due date, growth) lives here so it is testable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from . import config

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class DatabaseError(Exception):
    """Base class for database-level problems."""


class ValidationError(DatabaseError):
    """Raised when caller-supplied data fails validation."""


class DuplicateRecordError(DatabaseError):
    """Raised when an insert would duplicate existing history."""


class NotFoundError(DatabaseError):
    """Raised when a referenced row does not exist."""


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS plants (
    plant_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    species         TEXT    NOT NULL,
    variety         TEXT,
    date_started    TEXT,                       -- ISO date, nullable
    data_source     TEXT    NOT NULL CHECK (data_source IN ('real', 'synthetic')),
    growing_conditions TEXT,                    -- free text / JSON, nullable
    external_ref    TEXT,                       -- source-system id (e.g. CSV Plant_ID)
    cohort          TEXT,                       -- e.g. 'initial' / 'holdout' / 'retrain'
                                               -- (holdout cohorts must never train)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (data_source, species, variety, external_ref)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id             INTEGER NOT NULL REFERENCES plants(plant_id)
                                       ON DELETE CASCADE,
    observation_date     TEXT    NOT NULL,
    age_days             INTEGER CHECK (age_days IS NULL OR age_days >= 0),
    height_cm            REAL    CHECK (height_cm IS NULL OR height_cm >= 0),
    -- growth rate may legitimately be negative (measurement noise); no sign CHECK
    growth_rate_cm_per_day REAL,
    canopy_width_cm      REAL    CHECK (canopy_width_cm IS NULL OR canopy_width_cm >= 0),
    stem_width_cm        REAL    CHECK (stem_width_cm IS NULL OR stem_width_cm >= 0),
    visible_true_leaf_count INTEGER
                                       CHECK (visible_true_leaf_count IS NULL
                                              OR visible_true_leaf_count >= 0),
    data_source          TEXT    NOT NULL CHECK (data_source IN ('real', 'synthetic')),
    recorded_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    -- duplicate protection: one observation per plant per date
    UNIQUE (plant_id, observation_date)
);

CREATE TABLE IF NOT EXISTS readiness_events (
    readiness_event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id                  INTEGER NOT NULL REFERENCES plants(plant_id)
                                          ON DELETE CASCADE,
    readiness_observed_date   TEXT    NOT NULL,
    readiness_observed_age_days INTEGER
                                          CHECK (readiness_observed_age_days IS NULL
                                                 OR readiness_observed_age_days >= 0),
    readiness_definition_version TEXT NOT NULL,
    assessment_source         TEXT    NOT NULL,
    data_source               TEXT    NOT NULL
                                          CHECK (data_source IN ('real', 'synthetic')),
    notes                     TEXT,
    recorded_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    -- one readiness event per plant per definition version
    UNIQUE (plant_id, readiness_definition_version)
);

CREATE TABLE IF NOT EXISTS feedback_14d (
    feedback_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id                INTEGER NOT NULL REFERENCES plants(plant_id)
                                          ON DELETE CASCADE,
    transplant_date         TEXT    NOT NULL,
    feedback_due_date       TEXT    NOT NULL,
    feedback_submitted_date TEXT,
    survived_after_14d      INTEGER CHECK (survived_after_14d IS NULL
                                           OR survived_after_14d IN (0, 1)),
    height_at_transplant_cm REAL    CHECK (height_at_transplant_cm IS NULL
                                           OR height_at_transplant_cm >= 0),
    height_after_14d_cm     REAL    CHECK (height_after_14d_cm IS NULL
                                           OR height_after_14d_cm >= 0),
    growth_after_14d_cm     REAL,               -- computed; NULL when not measurable
    visible_condition       TEXT    CHECK (visible_condition IS NULL
                                           OR visible_condition IN
                                              ('healthy', 'stressed_wilting', 'dead')),
    user_notes              TEXT,
    -- transplant record detail (assessment does NOT imply a transplant)
    transplant_occurred     INTEGER NOT NULL DEFAULT 1
                                      CHECK (transplant_occurred IN (0, 1)),
    condition_at_transplant TEXT    CHECK (condition_at_transplant IS NULL
                                           OR condition_at_transplant IN
                                              ('healthy', 'stressed_wilting', 'dead')),
    transplant_notes        TEXT,
    data_source             TEXT    NOT NULL
                                      CHECK (data_source IN ('real', 'synthetic')),
    created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    -- duplicate protection: one transplant/feedback record per plant per date
    UNIQUE (plant_id, transplant_date)
);

-- Every prediction the system showed is preserved here so it stays
-- reconstructible: a later model/rule change never rewrites history.
CREATE TABLE IF NOT EXISTS prediction_history (
    prediction_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id                INTEGER REFERENCES plants(plant_id)
                                       ON DELETE SET NULL,
    species                 TEXT    NOT NULL,
    variety                 TEXT,
    prediction_scope        TEXT    NOT NULL DEFAULT 'plant',
    observation_date        TEXT,
    method                  TEXT    NOT NULL,   -- validated_model / synthetic_demo
                                                -- / historical_reference_baseline
                                                -- / insufficient_evidence
    level                   INTEGER,
    available               INTEGER NOT NULL CHECK (available IN (0, 1)),
    synthetic               INTEGER NOT NULL DEFAULT 0
                                      CHECK (synthetic IN (0, 1)),
    provisional             INTEGER NOT NULL DEFAULT 0
                                      CHECK (provisional IN (0, 1)),
    days_remaining          REAL,
    predicted_readiness_date TEXT,
    model_name              TEXT,
    data_source             TEXT    CHECK (data_source IS NULL
                                           OR data_source IN ('real', 'synthetic')),
    baseline_version        INTEGER,
    n_independent_plants    INTEGER,
    message                 TEXT,
    limitations             TEXT,               -- JSON array
    observation_snapshot    TEXT,               -- JSON
    cohort_snapshot         TEXT,               -- JSON
    created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The user's readiness judgement. Deliberately separate from the system
-- prediction (kept in ``prediction_snapshot``) and from readiness_events
-- (which are the only training labels).
CREATE TABLE IF NOT EXISTS readiness_assessments (
    assessment_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id                INTEGER REFERENCES plants(plant_id)
                                       ON DELETE SET NULL,
    species                 TEXT    NOT NULL,
    variety                 TEXT,
    assessment_date         TEXT    NOT NULL,
    readiness_status        TEXT    NOT NULL
                                      CHECK (readiness_status IN
                                             ('ready', 'not_ready', 'uncertain')),
    notes                   TEXT,
    prediction_id           INTEGER REFERENCES prediction_history(prediction_id)
                                       ON DELETE SET NULL,
    prediction_snapshot     TEXT,               -- JSON of the prediction shown
    assessment_source       TEXT    NOT NULL DEFAULT 'user',
    data_source             TEXT    NOT NULL
                                      CHECK (data_source IN ('real', 'synthetic')),
    created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_observations_plant
    ON observations (plant_id, observation_date);
CREATE INDEX IF NOT EXISTS idx_feedback_due
    ON feedback_14d (feedback_due_date, feedback_submitted_date);
CREATE INDEX IF NOT EXISTS idx_plants_species
    ON plants (species, variety);
CREATE INDEX IF NOT EXISTS idx_predictions_plant
    ON prediction_history (plant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_predictions_species
    ON prediction_history (species, variety);
CREATE INDEX IF NOT EXISTS idx_assessments_plant
    ON readiness_assessments (plant_id, assessment_date);
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def parse_iso_date(value: str | date, field: str = "date") -> date:
    """Parse an ISO ``YYYY-MM-DD`` string (or pass a ``date`` through)."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Invalid {field} '{value}': expected ISO YYYY-MM-DD."
        ) from exc


def feedback_due_date(transplant_date: str | date) -> date:
    """14-day feedback becomes due ``FEEDBACK_WINDOW_DAYS`` after transplant."""
    return parse_iso_date(transplant_date, "transplant_date") + timedelta(
        days=config.FEEDBACK_WINDOW_DAYS
    )


def compute_growth(height_at_transplant: Optional[float],
                   height_after_14d: Optional[float]) -> Optional[float]:
    """Post-transplant growth = after - at transplant.

    Returns ``None`` (missing, never invented) whenever either height is
    unavailable - e.g. the plant died or could not be measured.
    """
    if height_at_transplant is None or height_after_14d is None:
        return None
    return round(float(height_after_14d) - float(height_at_transplant), 4)


def days_between(from_date: str | date, to_date: str | date) -> int:
    """Elapsed calendar days from ``from_date`` to ``to_date`` (signed)."""
    return (parse_iso_date(to_date, "to_date")
            - parse_iso_date(from_date, "from_date")).days


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and create if needed) the prototype database."""
    path = Path(db_path) if db_path else config.DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create tables if missing and return an open connection."""
    conn = connect(db_path)
    conn.executescript(SCHEMA_SQL)
    # Lightweight forward-migration for databases created before a column
    # existed (CREATE TABLE IF NOT EXISTS will not add columns).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(plants)")}
    if "cohort" not in cols:
        conn.execute("ALTER TABLE plants ADD COLUMN cohort TEXT")
    obs_cols = {r["name"] for r in conn.execute("PRAGMA table_info(observations)")}
    if "stem_width_cm" not in obs_cols:
        conn.execute(
            "ALTER TABLE observations ADD COLUMN stem_width_cm REAL "
            "CHECK (stem_width_cm IS NULL OR stem_width_cm >= 0)"
        )
    # feedback_14d gained transplant-record detail (assessment does not imply
    # a transplant) - add the columns to databases created before them.
    fb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback_14d)")}
    if "transplant_occurred" not in fb_cols:
        conn.execute(
            "ALTER TABLE feedback_14d ADD COLUMN transplant_occurred INTEGER "
            "NOT NULL DEFAULT 1 CHECK (transplant_occurred IN (0, 1))"
        )
    if "condition_at_transplant" not in fb_cols:
        conn.execute(
            "ALTER TABLE feedback_14d ADD COLUMN condition_at_transplant TEXT"
        )
    if "transplant_notes" not in fb_cols:
        conn.execute(
            "ALTER TABLE feedback_14d ADD COLUMN transplant_notes TEXT"
        )
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# Plants
# --------------------------------------------------------------------------- #


def register_plant(conn: sqlite3.Connection,
                   species: str,
                   variety: Optional[str] = None,
                   date_started: Optional[str] = None,
                   data_source: str = "real",
                   growing_conditions: Optional[str] = None,
                   external_ref: Optional[str] = None,
                   cohort: Optional[str] = None) -> int:
    """Register a plant. Returns the new ``plant_id``."""
    species = (species or "").strip()
    if not species:
        raise ValidationError("species is required.")
    if data_source not in config.VALID_SOURCES:
        raise ValidationError(
            f"data_source must be one of {config.VALID_SOURCES}, got '{data_source}'."
        )
    if date_started:
        parse_iso_date(date_started, "date_started")
    variety_norm = variety.strip() if variety else None

    # NULL-aware duplicate check: SQLite UNIQUE constraints treat NULLs as
    # distinct, so detect duplicates explicitly before inserting.
    existing = conn.execute(
        """SELECT plant_id FROM plants
           WHERE species = ? AND data_source = ?
             AND variety IS ? AND external_ref IS ?""",
        (species, data_source, variety_norm, external_ref),
    ).fetchone()
    if existing is not None:
        raise DuplicateRecordError(
            f"Plant already registered (plant_id={existing['plant_id']}): "
            f"species={species!r} variety={variety_norm!r} "
            f"source={data_source!r} ref={external_ref!r}"
        )

    try:
        cur = conn.execute(
            """INSERT INTO plants
               (species, variety, date_started, data_source,
                growing_conditions, external_ref, cohort)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (species, variety_norm, date_started, data_source,
             growing_conditions, external_ref, cohort),
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DuplicateRecordError(
            f"Plant already registered: species={species!r} variety={variety_norm!r} "
            f"source={data_source!r} ref={external_ref!r}"
        ) from exc
    conn.commit()
    return int(cur.lastrowid)


def get_plant(conn: sqlite3.Connection, plant_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM plants WHERE plant_id = ?", (plant_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No plant with plant_id={plant_id}.")
    return row


def list_plants(conn: sqlite3.Connection,
                species: Optional[str] = None,
                data_source: Optional[str] = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM plants WHERE 1=1"
    args: list = []
    if species:
        sql += " AND species = ?"
        args.append(species)
    if data_source:
        sql += " AND data_source = ?"
        args.append(data_source)
    sql += " ORDER BY species, variety, plant_id"
    return list(conn.execute(sql, args))


# --------------------------------------------------------------------------- #
# Observations  (append-only: never overwrite history)
# --------------------------------------------------------------------------- #


def add_observation(conn: sqlite3.Connection,
                    plant_id: int,
                    observation_date: str,
                    age_days: Optional[int] = None,
                    height_cm: Optional[float] = None,
                    growth_rate_cm_per_day: Optional[float] = None,
                    canopy_width_cm: Optional[float] = None,
                    stem_width_cm: Optional[float] = None,
                    visible_true_leaf_count: Optional[int] = None,
                    data_source: Optional[str] = None) -> int:
    """Append an observation. Duplicate (plant, date) is rejected."""
    plant = get_plant(conn, plant_id)
    obs_date = parse_iso_date(observation_date, "observation_date")
    source = data_source or plant["data_source"]
    if source not in config.VALID_SOURCES:
        raise ValidationError(f"Invalid data_source '{source}'.")
    if source != plant["data_source"]:
        raise ValidationError(
            f"Observation data_source '{source}' does not match plant "
            f"data_source '{plant['data_source']}'."
        )

    if age_days is not None and int(age_days) < 0:
        raise ValidationError("age_days must be >= 0.")
    if height_cm is not None and float(height_cm) < 0:
        raise ValidationError("height_cm must be >= 0.")
    if canopy_width_cm is not None and float(canopy_width_cm) < 0:
        raise ValidationError("canopy_width_cm must be >= 0.")
    if stem_width_cm is not None and float(stem_width_cm) < 0:
        raise ValidationError("stem_width_cm must be >= 0.")
    if visible_true_leaf_count is not None and int(visible_true_leaf_count) < 0:
        raise ValidationError("visible_true_leaf_count must be >= 0.")
    if growth_rate_cm_per_day is not None:
        # The column stores no CHECK in schema for sign (plants can shrink in
        # noise), but a value must be finite; we accept any float and simply
        # refuse to *derive* one that was not measured.
        growth_rate_cm_per_day = float(growth_rate_cm_per_day)

    # derive age if not given but the plant has a start date
    if age_days is None and plant["date_started"]:
        age_days = days_between(plant["date_started"], obs_date)
        if age_days < 0:
            raise ValidationError(
                "observation_date is before the plant's date_started."
            )

    try:
        cur = conn.execute(
            """INSERT INTO observations
               (plant_id, observation_date, age_days, height_cm,
                growth_rate_cm_per_day, canopy_width_cm, stem_width_cm,
                visible_true_leaf_count, data_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plant_id, obs_date.isoformat(), age_days, height_cm,
             growth_rate_cm_per_day, canopy_width_cm, stem_width_cm,
             visible_true_leaf_count, source),
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DuplicateRecordError(
            f"Observation for plant {plant_id} on {obs_date} already exists - "
            "historical observations are never overwritten."
        ) from exc
    conn.commit()
    return int(cur.lastrowid)


def get_observations(conn: sqlite3.Connection, plant_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT * FROM observations WHERE plant_id = ?
               ORDER BY observation_date, observation_id""",
            (plant_id,),
        )
    )


def latest_observation(conn: sqlite3.Connection, plant_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM observations WHERE plant_id = ?
           ORDER BY observation_date DESC, observation_id DESC LIMIT 1""",
        (plant_id,),
    ).fetchone()


# --------------------------------------------------------------------------- #
# Species-level dashboard / cohort selection
# --------------------------------------------------------------------------- #


def species_dashboard_catalog(conn: sqlite3.Connection,
                              data_source: str = "real") -> list[dict]:
    """Species/variety summaries used by the species-level dashboard.

    Returns one entry per species/'' variety combination (empty string for
    NULL variety) plus the distinct varieties and observation dates that
    exist for that species. No values are invented or derived here; this is
    a lightweight inventory of what the dashboard can select.
    """
    rows = conn.execute(
        """SELECT p.species AS species,
                  COALESCE(p.variety, '') AS variety,
                  COUNT(DISTINCT p.plant_id) AS plant_count,
                  COUNT(o.observation_id) AS observation_count,
                  MAX(o.observation_date) AS latest_observation_date
           FROM plants p
           LEFT JOIN observations o
                  ON o.plant_id = p.plant_id
                 AND o.data_source = ?
           GROUP BY p.species, COALESCE(p.variety, '')
           ORDER BY p.species, COALESCE(p.variety, '')""",
        (data_source,),
    ).fetchall()

    catalog: dict[tuple[str, str], dict] = {}
    for row in rows:
        item = dict(row)
        item["varieties"] = []
        item["available_dates"] = []
        catalog[(item["species"], item["variety"])] = item

    variety_rows = conn.execute(
        """SELECT DISTINCT p.species AS species,
                           COALESCE(p.variety, '') AS variety
           FROM plants p
           ORDER BY p.species, COALESCE(p.variety, '')"""
    ).fetchall()
    for row in variety_rows:
        item = dict(row)
        for key, value in catalog.items():
            if key[0] == item["species"]:
                value["varieties"].append(item["variety"])

    date_rows = conn.execute(
        """SELECT DISTINCT p.species AS species,
                           COALESCE(p.variety, '') AS variety,
                           o.observation_date AS observation_date
           FROM observations o
           JOIN plants p ON p.plant_id = o.plant_id
           WHERE o.data_source = ?
           ORDER BY p.species, COALESCE(p.variety, ''),
                    o.observation_date DESC""",
        (data_source,),
    ).fetchall()
    for row in date_rows:
        item = dict(row)
        for key, value in catalog.items():
            if key[0] == item["species"]:
                if item["observation_date"] not in value["available_dates"]:
                    value["available_dates"].append(item["observation_date"])

    # The grouped row for ('species', '') is the entry used by the UI when
    # "All varieties" is selected; keep one row per species for simplicity.
    seen_species: set[str] = set()
    simplified: list[dict] = []
    for (species, variety), item in catalog.items():
        if species in seen_species:
            base = next(x for x in simplified if x["species"] == species)
            base["varieties"].extend(
                v for v in item["varieties"] if v and v not in base["varieties"]
            )
            base["available_dates"] = sorted(
                set(base["available_dates"]) | set(item["available_dates"]),
                reverse=True,
            )
            continue
        seen_species.add(species)
        base = dict(item)
        base["varieties"] = [v for v in base["varieties"] if v]
        base["available_dates"] = sorted(
            set(base["available_dates"]), reverse=True
        )
        simplified.append(base)
    return simplified


def latest_observation_date_for_species(
        conn: sqlite3.Connection, species: str,
        variety: Optional[str] = None,
        data_source: str = "real") -> Optional[str]:
    """Most recent real observation date for a species/optional variety."""
    sql = """SELECT MAX(o.observation_date) AS latest_date
             FROM observations o
             JOIN plants p ON p.plant_id = o.plant_id
             WHERE p.species = ? AND o.data_source = ?"""
    args: list = [species, data_source]
    if variety:
        sql += " AND p.variety = ?"
        args.append(variety)
    row = conn.execute(sql, args).fetchone()
    return row["latest_date"] if row and row["latest_date"] else None


def species_cohort_snapshot(conn: sqlite3.Connection, species: str,
                            variety: Optional[str] = None,
                            observation_date: Optional[str] = None,
                            data_source: str = "real") -> dict:
    """Fetch the raw observation rows behind a species-level prediction.

    The caller may request a date explicitly, otherwise the latest available
    date is used. Missing data is preserved as missing; no rows are invented.
    """
    species = (species or "").strip()
    if not species:
        raise ValidationError("species is required.")

    target_date: Optional[str]
    if observation_date:
        target_date = parse_iso_date(
            observation_date, "observation_date").isoformat()
    else:
        target_date = latest_observation_date_for_species(
            conn, species, variety, data_source)
        if target_date:
            target_date = parse_iso_date(
                target_date, "observation_date").isoformat()

    if not target_date:
        return {
            "species": species,
            "variety": variety,
            "observation_date": None,
            "rows": [],
            "row_count": 0,
            "plants_represented": 0,
        }

    sql = """SELECT o.*, p.species, p.variety, p.external_ref
             FROM observations o
             JOIN plants p ON p.plant_id = o.plant_id
             WHERE p.species = ? AND o.observation_date = ?
               AND o.data_source = ?"""
    args: list = [species, target_date, data_source]
    if variety:
        sql += " AND p.variety = ?"
        args.append(variety)
    sql += " ORDER BY p.plant_id, o.observation_id"

    rows = [dict(r) for r in conn.execute(sql, args)]
    return {
        "species": species,
        "variety": variety,
        "observation_date": target_date,
        "rows": rows,
        "row_count": len(rows),
        "plants_represented": len({r["plant_id"] for r in rows}),
    }



# --------------------------------------------------------------------------- #
# Readiness events
# --------------------------------------------------------------------------- #


def record_readiness_event(conn: sqlite3.Connection,
                           plant_id: int,
                           readiness_observed_date: str,
                           readiness_definition_version: str,
                           assessment_source: str,
                           readiness_observed_age_days: Optional[int] = None,
                           data_source: Optional[str] = None,
                           notes: Optional[str] = None) -> int:
    """Record an explicitly assessed readiness event (the only label source)."""
    plant = get_plant(conn, plant_id)
    rdate = parse_iso_date(readiness_observed_date, "readiness_observed_date")
    source = data_source or plant["data_source"]
    if source != plant["data_source"]:
        raise ValidationError(
            "readiness event data_source must match the plant's data_source."
        )
    if not readiness_definition_version or not readiness_definition_version.strip():
        raise ValidationError(
            "readiness_definition_version is required - readiness must follow a "
            "documented assessment definition, not an arbitrary label."
        )
    if not assessment_source or not assessment_source.strip():
        raise ValidationError("assessment_source is required.")

    if readiness_observed_age_days is None and plant["date_started"]:
        readiness_observed_age_days = days_between(plant["date_started"], rdate)
    if (readiness_observed_age_days is not None
            and int(readiness_observed_age_days) < 0):
        raise ValidationError("readiness_observed_age_days must be >= 0.")

    try:
        cur = conn.execute(
            """INSERT INTO readiness_events
               (plant_id, readiness_observed_date, readiness_observed_age_days,
                readiness_definition_version, assessment_source, data_source, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plant_id, rdate.isoformat(), readiness_observed_age_days,
             readiness_definition_version.strip(), assessment_source.strip(),
             source, notes),
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DuplicateRecordError(
            f"Readiness event already recorded for plant {plant_id} under "
            f"definition '{readiness_definition_version}'."
        ) from exc
    conn.commit()
    return int(cur.lastrowid)


def get_readiness_event(conn: sqlite3.Connection,
                        plant_id: int,
                        definition_version: Optional[str] = None
                        ) -> Optional[sqlite3.Row]:
    if definition_version:
        return conn.execute(
            """SELECT * FROM readiness_events WHERE plant_id = ?
               AND readiness_definition_version = ?""",
            (plant_id, definition_version),
        ).fetchone()
    return conn.execute(
        """SELECT * FROM readiness_events WHERE plant_id = ?
           ORDER BY readiness_observed_date LIMIT 1""",
        (plant_id,),
    ).fetchone()


def readiness_event_counts(conn: sqlite3.Connection,
                           species: str,
                           variety: Optional[str] = None,
                           data_source: str = "real") -> dict:
    """How many usable readiness events exist for a species/variety."""
    sql = """SELECT COUNT(DISTINCT r.plant_id) AS plants,
                    COUNT(*) AS events
             FROM readiness_events r
             JOIN plants p ON p.plant_id = r.plant_id
             WHERE p.species = ? AND r.data_source = ?"""
    args: list = [species, data_source]
    if variety:
        sql += " AND p.variety = ?"
        args.append(variety)
    row = conn.execute(sql, args).fetchone()
    return {"independent_plants": int(row["plants"]),
            "readiness_events": int(row["events"]),
            "species": species, "variety": variety,
            "data_source": data_source}


# --------------------------------------------------------------------------- #
# 14-day feedback workflow
# --------------------------------------------------------------------------- #


def record_transplant(conn: sqlite3.Connection,
                      plant_id: int,
                      transplant_date: str,
                      height_at_transplant_cm: Optional[float] = None,
                      data_source: Optional[str] = None,
                      transplant_occurred: bool = True,
                      condition_at_transplant: Optional[str] = None,
                      transplant_notes: Optional[str] = None) -> dict:
    """Register a transplant and create the pending 14-day feedback record.

    ``transplant_occurred`` distinguishes a transplant that actually happened
    from a recorded decision/deferral: a readiness *assessment* never implies
    a transplant. Feedback is only due (and can only be submitted) when the
    transplant occurred.
    """
    plant = get_plant(conn, plant_id)
    tdate = parse_iso_date(transplant_date, "transplant_date")
    source = data_source or plant["data_source"]
    if source != plant["data_source"]:
        raise ValidationError("transplant data_source must match the plant's.")
    if height_at_transplant_cm is not None and float(height_at_transplant_cm) < 0:
        raise ValidationError("height_at_transplant_cm must be >= 0.")
    if condition_at_transplant is not None \
            and condition_at_transplant not in config.VISIBLE_CONDITIONS:
        raise ValidationError(
            f"condition_at_transplant must be one of "
            f"{config.VISIBLE_CONDITIONS}.")

    due = feedback_due_date(tdate)
    try:
        cur = conn.execute(
            """INSERT INTO feedback_14d
               (plant_id, transplant_date, feedback_due_date,
                height_at_transplant_cm, data_source, transplant_occurred,
                condition_at_transplant, transplant_notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (plant_id, tdate.isoformat(), due.isoformat(),
             height_at_transplant_cm, source,
             1 if transplant_occurred else 0,
             condition_at_transplant, transplant_notes),
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DuplicateRecordError(
            f"Transplant already recorded for plant {plant_id} on {tdate}."
        ) from exc
    conn.commit()
    return {
        "feedback_id": int(cur.lastrowid),
        "plant_id": plant_id,
        "transplant_date": tdate.isoformat(),
        "feedback_due_date": due.isoformat(),
        "height_at_transplant_cm": height_at_transplant_cm,
        "transplant_occurred": bool(transplant_occurred),
        "condition_at_transplant": condition_at_transplant,
        "transplant_notes": transplant_notes,
        "data_source": source,
    }


def get_feedback(conn: sqlite3.Connection, feedback_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM feedback_14d WHERE feedback_id = ?", (feedback_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No feedback record with feedback_id={feedback_id}.")
    return row


def pending_feedback(conn: sqlite3.Connection,
                     plant_id: Optional[int] = None,
                     as_of: Optional[date] = None) -> list[sqlite3.Row]:
    """Feedback records awaiting a submission (reminders / overdue list)."""
    as_of = as_of or date.today()
    sql = """SELECT f.*, p.species, p.variety
             FROM feedback_14d f
             JOIN plants p ON p.plant_id = f.plant_id
             WHERE f.feedback_submitted_date IS NULL
               AND f.transplant_occurred = 1"""
    args: list = []
    if plant_id is not None:
        sql += " AND f.plant_id = ?"
        args.append(plant_id)
    sql += " ORDER BY f.feedback_due_date"
    rows = list(conn.execute(sql, args))
    out = []
    for row in rows:
        due = parse_iso_date(row["feedback_due_date"])
        overdue = (as_of - due).days
        out.append({**dict(row),
                    "is_overdue": overdue > 0,
                    "days_overdue": max(overdue, 0)})
    return out


def submit_feedback(conn: sqlite3.Connection,
                    feedback_id: int,
                    feedback_submitted_date: str,
                    survived_after_14d: bool,
                    height_after_14d_cm: Optional[float] = None,
                    visible_condition: Optional[str] = None,
                    user_notes: Optional[str] = None) -> dict:
    """Submit the 14-day outcome.

    * survival is always preserved,
    * growth is computed as ``height_after_14d - height_at_transplant`` and
      left ``NULL`` when either height is unavailable (death, unmeasurable),
    * the record is updated exactly once - a second submission is rejected,
    * submission date must be on/after the feedback due date is *not*
      enforced (early submissions are allowed) but must not be before the
      transplant date.
    """
    row = get_feedback(conn, feedback_id)
    if row["feedback_submitted_date"] is not None:
        raise DuplicateRecordError(
            f"Feedback {feedback_id} was already submitted on "
            f"{row['feedback_submitted_date']} - outcomes are immutable."
        )
    if not row["transplant_occurred"]:
        raise ValidationError(
            "This record is a transplant decision that did not occur - there "
            "is no transplant outcome to follow up.")

    sub_date = parse_iso_date(feedback_submitted_date, "feedback_submitted_date")
    transplant = parse_iso_date(row["transplant_date"])
    if sub_date < transplant:
        raise ValidationError(
            "feedback_submitted_date cannot be before the transplant_date."
        )

    if visible_condition is not None and visible_condition not in config.VISIBLE_CONDITIONS:
        raise ValidationError(
            f"visible_condition must be one of {config.VISIBLE_CONDITIONS}."
        )

    survived = 1 if survived_after_14d else 0
    if not survived_after_14d:
        # dead plant: condition, if given, must be consistent
        if visible_condition is None:
            visible_condition = "dead"
        elif visible_condition != "dead":
            raise ValidationError(
                "visible_condition must be 'dead' when survived_after_14d is false."
            )
        # heights of a dead plant may legitimately be missing; keep them as given
    elif visible_condition == "dead":
        raise ValidationError(
            "visible_condition 'dead' conflicts with survived_after_14d = true."
        )

    if height_after_14d_cm is not None and float(height_after_14d_cm) < 0:
        raise ValidationError("height_after_14d_cm must be >= 0.")

    growth = compute_growth(row["height_at_transplant_cm"], height_after_14d_cm)

    conn.execute(
        """UPDATE feedback_14d
           SET feedback_submitted_date = ?,
               survived_after_14d = ?,
               height_after_14d_cm = ?,
               growth_after_14d_cm = ?,
               visible_condition = ?,
               user_notes = ?
           WHERE feedback_id = ?""",
        (sub_date.isoformat(), survived, height_after_14d_cm, growth,
         visible_condition, user_notes, feedback_id),
    )
    conn.commit()
    return dict(get_feedback(conn, feedback_id))


def feedback_history(conn: sqlite3.Connection,
                     plant_id: Optional[int] = None,
                     data_source: Optional[str] = None) -> list[dict]:
    sql = """SELECT f.*, p.species, p.variety
             FROM feedback_14d f JOIN plants p ON p.plant_id = f.plant_id
             WHERE 1=1"""
    args: list = []
    if plant_id is not None:
        sql += " AND f.plant_id = ?"
        args.append(plant_id)
    if data_source:
        sql += " AND f.data_source = ?"
        args.append(data_source)
    sql += " ORDER BY f.transplant_date, f.feedback_id"
    return [dict(r) for r in conn.execute(sql, args)]


# --------------------------------------------------------------------------- #
# Prediction history (append-only snapshots)
# --------------------------------------------------------------------------- #


def _json_dump(value) -> Optional[str]:
    return None if value is None else json.dumps(value, default=str)


def _json_load(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def save_prediction_snapshot(conn: sqlite3.Connection, *,
                             species: str,
                             method: str,
                             available: bool,
                             variety: Optional[str] = None,
                             plant_id: Optional[int] = None,
                             prediction_scope: str = "plant",
                             observation_date: Optional[str] = None,
                             level: Optional[int] = None,
                             synthetic: bool = False,
                             provisional: bool = False,
                             days_remaining: Optional[float] = None,
                             predicted_readiness_date: Optional[str] = None,
                             model_name: Optional[str] = None,
                             data_source: Optional[str] = None,
                             baseline_version: Optional[int] = None,
                             n_independent_plants: Optional[int] = None,
                             message: Optional[str] = None,
                             limitations=None,
                             observation_snapshot=None,
                             cohort_snapshot=None) -> dict:
    """Persist an immutable snapshot of a prediction shown to the user.

    Snapshots are append-only: a later model or rule change can never rewrite
    what the system predicted at the time.
    """
    species = (species or "").strip()
    if not species:
        raise ValidationError("species is required for a prediction snapshot.")
    if not method:
        raise ValidationError("method is required for a prediction snapshot.")
    if data_source is not None and data_source not in config.VALID_SOURCES:
        raise ValidationError(f"Invalid data_source '{data_source}'.")
    if plant_id is not None:
        get_plant(conn, plant_id)  # raises NotFoundError when unknown
    obs_date = (parse_iso_date(observation_date, "observation_date").isoformat()
                if observation_date else None)
    pred_date = (parse_iso_date(predicted_readiness_date,
                                "predicted_readiness_date").isoformat()
                 if predicted_readiness_date else None)

    cur = conn.execute(
        """INSERT INTO prediction_history
           (plant_id, species, variety, prediction_scope, observation_date,
            method, level, available, synthetic, provisional, days_remaining,
            predicted_readiness_date, model_name, data_source, baseline_version,
            n_independent_plants, message, limitations, observation_snapshot,
            cohort_snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (plant_id, species, variety, prediction_scope, obs_date, method, level,
         1 if available else 0, 1 if synthetic else 0,
         1 if provisional else 0, days_remaining, pred_date, model_name,
         data_source, baseline_version, n_independent_plants, message,
         _json_dump(limitations), _json_dump(observation_snapshot),
         _json_dump(cohort_snapshot)),
    )
    conn.commit()
    return get_prediction(conn, int(cur.lastrowid))


def _prediction_row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in ("limitations", "observation_snapshot", "cohort_snapshot"):
        out[key] = _json_load(out.get(key))
    out["available"] = bool(out.get("available"))
    out["synthetic"] = bool(out.get("synthetic"))
    out["provisional"] = bool(out.get("provisional"))
    return out


def get_prediction(conn: sqlite3.Connection, prediction_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM prediction_history WHERE prediction_id = ?",
        (prediction_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"No prediction with prediction_id={prediction_id}.")
    return _prediction_row_to_dict(row)


def list_prediction_history(conn: sqlite3.Connection,
                            plant_id: Optional[int] = None,
                            species: Optional[str] = None,
                            data_source: Optional[str] = None,
                            scope: Optional[str] = None,
                            limit: Optional[int] = None) -> list[dict]:
    sql = "SELECT * FROM prediction_history WHERE 1=1"
    args: list = []
    if plant_id is not None:
        sql += " AND plant_id = ?"
        args.append(plant_id)
    if species:
        sql += " AND species = ?"
        args.append(species)
    if data_source:
        sql += " AND data_source = ?"
        args.append(data_source)
    if scope:
        sql += " AND prediction_scope = ?"
        args.append(scope)
    sql += " ORDER BY created_at DESC, prediction_id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [_prediction_row_to_dict(r) for r in conn.execute(sql, args)]


# --------------------------------------------------------------------------- #
# Readiness assessments (user's judgement, distinct from prediction/labels)
# --------------------------------------------------------------------------- #


def record_readiness_assessment(conn: sqlite3.Connection, *,
                                assessment_date: str,
                                readiness_status: str,
                                species: Optional[str] = None,
                                plant_id: Optional[int] = None,
                                variety: Optional[str] = None,
                                notes: Optional[str] = None,
                                prediction_id: Optional[int] = None,
                                prediction_snapshot=None,
                                assessment_source: str = "user",
                                data_source: Optional[str] = None) -> dict:
    """Record the user's readiness judgement for a plant/species.

    This is deliberately NOT a readiness *event* (the only training label) and
    NOT the system prediction: the prediction shown at the time is preserved
    separately in ``prediction_snapshot``.
    """
    if readiness_status not in config.READINESS_ASSESSMENT_STATUSES:
        raise ValidationError(
            f"readiness_status must be one of "
            f"{config.READINESS_ASSESSMENT_STATUSES}.")
    adate = parse_iso_date(assessment_date, "assessment_date")

    plant = None
    if plant_id is not None:
        plant = get_plant(conn, plant_id)
        species = plant["species"]
        if variety is None:
            variety = plant["variety"]
        if data_source is None:
            data_source = plant["data_source"]
    species = (species or "").strip()
    if not species:
        raise ValidationError("species is required.")
    source = data_source or "real"
    if source not in config.VALID_SOURCES:
        raise ValidationError(f"Invalid data_source '{source}'.")

    if prediction_id is not None:
        get_prediction(conn, prediction_id)  # raises NotFoundError if absent

    cur = conn.execute(
        """INSERT INTO readiness_assessments
           (plant_id, species, variety, assessment_date, readiness_status,
            notes, prediction_id, prediction_snapshot, assessment_source,
            data_source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (plant_id, species, variety, adate.isoformat(), readiness_status,
         notes, prediction_id, _json_dump(prediction_snapshot),
         assessment_source or "user", source),
    )
    conn.commit()
    return list_readiness_assessments(
        conn, assessment_id=int(cur.lastrowid))[0]


def list_readiness_assessments(conn: sqlite3.Connection,
                               plant_id: Optional[int] = None,
                               species: Optional[str] = None,
                               data_source: Optional[str] = None,
                               assessment_id: Optional[int] = None
                               ) -> list[dict]:
    sql = "SELECT * FROM readiness_assessments WHERE 1=1"
    args: list = []
    if assessment_id is not None:
        sql += " AND assessment_id = ?"
        args.append(assessment_id)
    if plant_id is not None:
        sql += " AND plant_id = ?"
        args.append(plant_id)
    if species:
        sql += " AND species = ?"
        args.append(species)
    if data_source:
        sql += " AND data_source = ?"
        args.append(data_source)
    sql += " ORDER BY assessment_date DESC, assessment_id DESC"
    out = []
    for row in conn.execute(sql, args):
        item = dict(row)
        item["prediction_snapshot"] = _json_load(item.get("prediction_snapshot"))
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Training-data assembly (readiness targets only)
# --------------------------------------------------------------------------- #


def readiness_training_rows(conn: sqlite3.Connection,
                            data_source: str = "real",
                            species: Optional[str] = None,
                            variety: Optional[str] = None,
                            cohorts: Optional[Iterable[str]] = None,
                            exclude_cohorts: Optional[Iterable[str]] = None
                            ) -> list[dict]:
    """Rows for supervised countdown learning.

    For every plant with a recorded readiness event, every observation at or
    before the readiness date becomes one row with

        days_remaining = readiness_observed_date - observation_date

    Observations after the readiness event are excluded (they would leak the
    answer). Plants without a readiness event contribute nothing - their
    target stays unknown. When a plant has several readiness events the
    earliest one is used as the training target.
    """
    sql = """WITH earliest_readiness AS (
                 SELECT plant_id, MIN(readiness_observed_date) AS ready_date
                 FROM readiness_events
                 WHERE data_source = ?
                 GROUP BY plant_id
             )
             SELECT p.plant_id, p.species, p.variety, p.data_source AS plant_source,
                    p.growing_conditions, p.cohort,
                    o.observation_id, o.observation_date, o.age_days,
                    o.height_cm, o.growth_rate_cm_per_day,
                    o.stem_width_cm, o.visible_true_leaf_count,
                    er.ready_date AS readiness_observed_date,
                    (SELECT r.readiness_definition_version
                       FROM readiness_events r
                      WHERE r.plant_id = p.plant_id
                        AND r.readiness_observed_date = er.ready_date
                      ORDER BY r.readiness_event_id LIMIT 1)
                        AS readiness_definition_version
             FROM plants p
             JOIN observations o ON o.plant_id = p.plant_id
             JOIN earliest_readiness er ON er.plant_id = p.plant_id
             WHERE p.data_source = ?
               AND o.data_source = ?
               AND o.observation_date <= er.ready_date"""
    args: list = [data_source, data_source, data_source]
    if species:
        sql += " AND p.species = ?"
        args.append(species)
    if variety:
        sql += " AND p.variety = ?"
        args.append(variety)
    if cohorts:
        cohort_list = list(cohorts)
        if cohort_list:
            sql += f" AND p.cohort IN ({','.join('?' * len(cohort_list))})"
            args.extend(cohort_list)
    if exclude_cohorts:
        excl = list(exclude_cohorts)
        if excl:
            sql += (f" AND (p.cohort IS NULL OR p.cohort NOT IN "
                    f"({','.join('?' * len(excl))}))")
            args.extend(excl)
    sql += " ORDER BY p.plant_id, o.observation_date"

    rows = []
    for r in conn.execute(sql, args):
        row = dict(r)
        row["days_remaining"] = days_between(
            row["observation_date"], row["readiness_observed_date"]
        )
        rows.append(row)
    return rows


def species_data_summary(conn: sqlite3.Connection,
                         species: str,
                         variety: Optional[str] = None) -> dict:
    """Overview used by the UI: plants, observations, readiness events."""
    plant_sql = "SELECT data_source, COUNT(*) AS n FROM plants WHERE species = ?"
    plant_args: list = [species]
    if variety:
        plant_sql += " AND variety = ?"
        plant_args.append(variety)
    plant_sql += " GROUP BY data_source"
    plants = {r["data_source"]: int(r["n"])
              for r in conn.execute(plant_sql, plant_args)}

    obs_sql = """SELECT o.data_source, COUNT(*) AS n
                 FROM observations o JOIN plants p ON p.plant_id = o.plant_id
                 WHERE p.species = ?"""
    obs_args: list = [species]
    if variety:
        obs_sql += " AND p.variety = ?"
        obs_args.append(variety)
    obs_sql += " GROUP BY o.data_source"
    observations = {r["data_source"]: int(r["n"])
                    for r in conn.execute(obs_sql, obs_args)}

    events = readiness_event_counts(conn, species, variety, data_source="real")
    events_syn = readiness_event_counts(conn, species, variety, data_source="synthetic")
    return {
        "species": species,
        "variety": variety,
        "plants": plants,
        "observations": observations,
        "real_readiness_events": events,
        "synthetic_readiness_events": events_syn,
    }
