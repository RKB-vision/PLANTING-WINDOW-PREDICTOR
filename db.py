"""
db.py — Unified SQLite Data Layer for Plant Readiness

Preserves non-negotiable architectural principles:
1. Distinct record types: plants, observations, readiness_events, predictions, feedback.
2. Missing values stay None (never fabricated as 0).
3. First observation has growth_rate = None.
4. Earliest readiness event per plant establishes days_remaining target.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_DB_PATH = Path("plant_readiness.db").resolve()


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    resolved_path = Path(db_path).resolve() if db_path != ":memory:" else ":memory:"
    conn = sqlite3.connect(str(resolved_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the SQLite database schema if not already present."""
    with get_connection(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS plants (
            plant_id TEXT PRIMARY KEY,
            species TEXT NOT NULL,
            variety TEXT NOT NULL,
            planted_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_id TEXT NOT NULL REFERENCES plants(plant_id),
            observation_date DATE NOT NULL,
            age_days INTEGER,
            height_cm REAL NOT NULL,
            stem_width_cm REAL,
            visible_true_leaf_count INTEGER,
            growth_rate_cm_per_day REAL,
            source TEXT DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(plant_id, observation_date)
        );

        CREATE TABLE IF NOT EXISTS readiness_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_id TEXT NOT NULL REFERENCES plants(plant_id),
            event_date DATE NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(plant_id, event_date)
        );

        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_id TEXT,
            species TEXT NOT NULL,
            prediction_date DATE NOT NULL,
            predicted_days_remaining REAL,
            predicted_readiness_date DATE,
            method TEXT NOT NULL,
            confidence TEXT NOT NULL,
            supporting_evidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER REFERENCES predictions(id),
            plant_id TEXT REFERENCES plants(plant_id),
            stage TEXT NOT NULL,
            assessment TEXT,
            actual_transplant_date DATE,
            condition TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_obs_plant_date ON observations(plant_id, observation_date);
        CREATE INDEX IF NOT EXISTS idx_events_plant ON readiness_events(plant_id);
        """)


def parse_date(date_str: str) -> date:
    """Parses various date formats safely."""
    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unable to parse date: {date_str}")


def register_plant(
    db_path: Path | str,
    plant_id: str,
    species: str,
    variety: str,
    planted_date: Optional[date | str] = None,
) -> None:
    planted_val = str(planted_date) if planted_date else None
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO plants (plant_id, species, variety, planted_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(plant_id) DO UPDATE SET
                species=excluded.species,
                variety=excluded.variety,
                planted_date=COALESCE(excluded.planted_date, plants.planted_date)
            """,
            (str(plant_id).strip(), species.strip(), variety.strip(), planted_val),
        )


def record_observation(
    db_path: Path | str,
    plant_id: str,
    observation_date: date | str,
    height_cm: float,
    stem_width_cm: Optional[float] = None,
    leaf_count: Optional[int] = None,
    age_days: Optional[int] = None,
    source: str = "manual",
) -> None:
    """
    Records a plant observation and strictly computes growth rate
    relative to the immediate previous valid observation date.
    When only 1 measurement exists, growth_rate is None (never 0).
    """
    obs_date = parse_date(str(observation_date)) if isinstance(observation_date, str) else observation_date
    plant_id_str = str(plant_id).strip()

    with get_connection(db_path) as conn:
        # Find prior observation closest to obs_date
        cur = conn.execute(
            """
            SELECT observation_date, height_cm
            FROM observations
            WHERE plant_id = ? AND observation_date < ?
            ORDER BY observation_date DESC LIMIT 1
            """,
            (plant_id_str, obs_date.isoformat()),
        )
        prev = cur.fetchone()

        growth_rate = None
        if prev:
            prev_date = date.fromisoformat(prev["observation_date"])
            days_diff = (obs_date - prev_date).days
            if days_diff > 0:
                growth_rate = round((height_cm - prev["height_cm"]) / days_diff, 4)

        conn.execute(
            """
            INSERT INTO observations (
                plant_id, observation_date, age_days, height_cm,
                stem_width_cm, visible_true_leaf_count, growth_rate_cm_per_day, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plant_id, observation_date) DO UPDATE SET
                height_cm = excluded.height_cm,
                stem_width_cm = COALESCE(excluded.stem_width_cm, observations.stem_width_cm),
                visible_true_leaf_count = COALESCE(excluded.visible_true_leaf_count, observations.visible_true_leaf_count),
                age_days = COALESCE(excluded.age_days, observations.age_days),
                growth_rate_cm_per_day = excluded.growth_rate_cm_per_day,
                source = excluded.source
            """,
            (
                plant_id_str,
                obs_date.isoformat(),
                age_days,
                float(height_cm),
                float(stem_width_cm) if stem_width_cm is not None else None,
                int(leaf_count) if leaf_count is not None else None,
                growth_rate,
                source,
            ),
        )


def record_readiness_event(
    db_path: Path | str,
    plant_id: str,
    event_date: date | str,
    status: str = "transplant_ready",
    notes: Optional[str] = None,
) -> None:
    ev_date = parse_date(str(event_date)) if isinstance(event_date, str) else event_date
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO readiness_events (plant_id, event_date, status, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(plant_id, event_date) DO UPDATE SET
                status = excluded.status,
                notes = COALESCE(excluded.notes, readiness_events.notes)
            """,
            (str(plant_id).strip(), ev_date.isoformat(), status, notes),
        )


def seed_from_csv(
    db_path: Path | str,
    csv_path: Path | str,
    species: str = "Sunflower(SuperOrange)",
    variety: str = "SuperOrange",
) -> int:
    """
    Seeds database from historical CSV (e.g. super_orange_growth_data.csv).
    Automatically records transplant readiness on the day after the final
    monitoring observation per plant.
    """
    init_db(db_path)
    csv_file = Path(csv_path)
    if not csv_file.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_file}")

    rows_by_plant: Dict[str, List[Dict[str, Any]]] = {}

    with open(csv_file, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw_r in reader:
            # Strip whitespace from keys and values
            r = {k.strip(): v.strip() for k, v in raw_r.items() if k and v}
            p_id = r.get("Plant_ID") or r.get("plant_id")
            if not p_id:
                continue
            p_id = str(p_id).strip()

            d_str = r.get("Date") or r.get("date")
            obs_date = parse_date(d_str)

            age_str = r.get("Age") or r.get("age") or ""
            age_days = None
            if age_str:
                clean_age = "".join(ch for ch in age_str if ch.isdigit())
                if clean_age:
                    age_days = int(clean_age)

            h_str = r.get("Height_cm") or r.get("height_cm") or r.get("Height")
            if h_str is None or h_str == "":
                continue
            height_cm = float(h_str)

            stem_str = r.get("Stem_Width_cm") or r.get("stem_width_cm")
            stem_width = float(stem_str) if stem_str else None

            leaves_str = r.get("Leaf_Count") or r.get("visible_true_leaf_count")
            leaves = int(leaves_str) if leaves_str else None

            if p_id not in rows_by_plant:
                rows_by_plant[p_id] = []
            rows_by_plant[p_id].append({
                "date": obs_date,
                "age_days": age_days,
                "height_cm": height_cm,
                "stem_width_cm": stem_width,
                "leaves": leaves,
            })

    total_inserted = 0
    for plant_id, obs_list in rows_by_plant.items():
        # Sort chronologically to preserve temporal order
        obs_list.sort(key=lambda x: x["date"])
        register_plant(db_path, plant_id, species, variety)

        for item in obs_list:
            record_observation(
                db_path,
                plant_id=plant_id,
                observation_date=item["date"],
                height_cm=item["height_cm"],
                stem_width_cm=item["stem_width_cm"],
                leaf_count=item["leaves"],
                age_days=item["age_days"],
                source="historical_csv",
            )
            total_inserted += 1

        # Final monitoring day + 1 day = actual historical transplantation
        final_obs = obs_list[-1]["date"]
        from datetime import timedelta
        transplant_date = final_obs + timedelta(days=1)
        record_readiness_event(
            db_path,
            plant_id=plant_id,
            event_date=transplant_date,
            status="transplanted",
            notes="Historical successful plant cohort transplant",
        )

    return total_inserted


def get_species_list(db_path: Path | str = DEFAULT_DB_PATH) -> List[str]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT species FROM plants ORDER BY species").fetchall()
        return [r["species"] for r in rows]


def get_plants(db_path: Path | str = DEFAULT_DB_PATH, species: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        if species:
            cur = conn.execute("SELECT * FROM plants WHERE species = ? ORDER BY plant_id", (species,))
        else:
            cur = conn.execute("SELECT * FROM plants ORDER BY species, plant_id")
        return [dict(r) for r in cur.fetchall()]


def get_observations(
    db_path: Path | str = DEFAULT_DB_PATH,
    plant_id: Optional[str] = None,
    species: Optional[str] = None,
) -> List[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        query = """
        SELECT o.*, p.species, p.variety
        FROM observations o
        JOIN plants p ON o.plant_id = p.plant_id
        WHERE 1=1
        """
        params: List[Any] = []
        if plant_id:
            query += " AND o.plant_id = ?"
            params.append(str(plant_id))
        if species:
            query += " AND p.species = ?"
            params.append(species)
        query += " ORDER BY o.observation_date ASC, o.plant_id ASC"
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_training_rows(db_path: Path | str = DEFAULT_DB_PATH, species: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Extracts training samples for countdown prediction.
    Target (y) = days_remaining = (earliest_readiness_date - observation_date).
    Only includes observations at or before readiness date.
    Plants without readiness events contribute 0 rows.
    """
    with get_connection(db_path) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(readiness_events)").fetchall()]
        date_col = "readiness_observed_date" if "readiness_observed_date" in cols else "event_date"

        query = f"""
        WITH earliest_readiness AS (
            SELECT plant_id, MIN({date_col}) as readiness_date
            FROM readiness_events
            GROUP BY plant_id
        )
        SELECT
            o.plant_id,
            p.species,
            p.variety,
            o.observation_date,
            o.age_days,
            o.height_cm,
            o.stem_width_cm,
            o.visible_true_leaf_count,
            o.growth_rate_cm_per_day,
            CAST(JULIANDAY(er.readiness_date) - JULIANDAY(o.observation_date) AS INTEGER) AS days_remaining
        FROM observations o
        JOIN plants p ON o.plant_id = p.plant_id
        JOIN earliest_readiness er ON o.plant_id = er.plant_id
        WHERE o.observation_date <= er.readiness_date
        """
        params: List[Any] = []
        if species:
            # Match with or without space, e.g. Sunflower (SuperOrange) or Sunflower(SuperOrange)
            clean_spec = species.replace(" ", "")
            query += " AND REPLACE(p.species, ' ', '') = ?"
            params.append(clean_spec)
        query += " ORDER BY o.plant_id, o.observation_date"
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def save_prediction(
    db_path: Path | str,
    species: str,
    prediction_date: date | str,
    predicted_days: Optional[float],
    predicted_date: Optional[date | str],
    method: str,
    confidence: str,
    plant_id: Optional[str] = None,
    supporting_evidence: Optional[str] = None,
) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO predictions (
                plant_id, species, prediction_date, predicted_days_remaining,
                predicted_readiness_date, method, confidence, supporting_evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(plant_id) if plant_id else None,
                species,
                str(prediction_date),
                predicted_days,
                str(predicted_date) if predicted_date else None,
                method,
                confidence,
                supporting_evidence,
            ),
        )
        return cur.lastrowid


def save_feedback(
    db_path: Path | str,
    prediction_id: Optional[int],
    stage: str,
    assessment: Optional[str] = None,
    actual_transplant_date: Optional[date | str] = None,
    condition: Optional[str] = None,
    notes: Optional[str] = None,
    plant_id: Optional[str] = None,
) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback (
                prediction_id, plant_id, stage, assessment,
                actual_transplant_date, condition, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction_id,
                str(plant_id) if plant_id else None,
                stage,
                assessment,
                str(actual_transplant_date) if actual_transplant_date else None,
                condition,
                notes,
            ),
        )
        return cur.lastrowid
