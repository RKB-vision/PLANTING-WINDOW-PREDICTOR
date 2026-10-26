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
import re
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
            variety TEXT,
            external_ref TEXT,
            cohort TEXT,
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
            calibration_source TEXT DEFAULT 'csv_import_no_calibration',
            calibration_verified INTEGER DEFAULT 0,
            rod_length_cm REAL,
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


def normalize_plant_ref(raw: str | None) -> str:
    """Normalize a plant reference for grouping/comparison.

    Case, whitespace, hyphens and underscores are ignored, but zero-padding
    is significant: 'Plant01' != 'Plant1' != 'Plant001'.
    """
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    # collapse whitespace, hyphens, underscores
    text = re.sub(r"[\s\-_]+", "", text)
    return text


def register_plant(
    db_path: Path | str,
    plant_id: str,
    species: str,
    variety: str = "",
    planted_date: Optional[date | str] = None,
    cohort: Optional[str] = None,
    external_ref: Optional[str] = None,
) -> Any:
    planted_val = str(planted_date) if planted_date else None
    plant_id_str = str(plant_id).strip()
    if external_ref is None:
        external_ref = plant_id_str
    with get_connection(db_path) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(plants)").fetchall()]
        date_col = "planted_date" if "planted_date" in cols else "date_started"

        if "data_source" in cols:
            # Legacy schema: plant_id is INTEGER PRIMARY KEY AUTOINCREMENT
            row = conn.execute(
                "SELECT plant_id FROM plants WHERE external_ref = ? OR CAST(plant_id AS TEXT) = ?",
                (plant_id_str, plant_id_str),
            ).fetchone()
            if row:
                conn.execute(
                    f"UPDATE plants SET species = ?, variety = ?, {date_col} = COALESCE(?, {date_col}) WHERE plant_id = ?",
                    (species.strip(), variety.strip(), planted_val, row[0]),
                )
                return row[0]

            try:
                num_id = int(plant_id_str)
            except ValueError:
                num_id = None

            if num_id is not None:
                conn.execute(
                    f"""
                    INSERT INTO plants (plant_id, species, variety, {date_col}, data_source, external_ref)
                    VALUES (?, ?, ?, ?, 'real', ?)
                    """,
                    (num_id, species.strip(), variety.strip(), planted_val, plant_id_str),
                )
                return num_id
            else:
                cur = conn.execute(
                    f"""
                    INSERT INTO plants (species, variety, {date_col}, data_source, external_ref)
                    VALUES (?, ?, ?, 'real', ?)
                    """,
                    (species.strip(), variety.strip(), planted_val, plant_id_str),
                )
                return cur.lastrowid
        else:
            # Clean schema: plant_id is TEXT PRIMARY KEY
            if "external_ref" in cols:
                conn.execute(
                    """
                    INSERT INTO plants (plant_id, species, variety, external_ref, cohort, planted_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plant_id) DO UPDATE SET
                        species=excluded.species,
                        variety=COALESCE(excluded.variety, plants.variety),
                        external_ref=COALESCE(excluded.external_ref, plants.external_ref),
                        cohort=COALESCE(excluded.cohort, plants.cohort),
                        planted_date=COALESCE(excluded.planted_date, plants.planted_date)
                    """,
                    (plant_id_str, species.strip(), variety.strip() if variety else None,
                     external_ref, cohort, planted_val),
                )
            else:
                conn.execute(
                    f"""
                    INSERT INTO plants (plant_id, species, variety, {date_col})
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(plant_id) DO UPDATE SET
                        species=excluded.species,
                        variety=excluded.variety,
                        {date_col}=COALESCE(excluded.{date_col}, plants.{date_col})
                    """,
                    (plant_id_str, species.strip(), variety.strip() if variety else None, planted_val),
                )
            return plant_id_str


def record_observation(
    db_path: Path | str,
    plant_id: str,
    observation_date: date | str,
    height_cm: float,
    stem_width_cm: Optional[float] = None,
    leaf_count: Optional[int] = None,
    age_days: Optional[int] = None,
    source: str = "manual",
    calibration_source: str = "csv_import_no_calibration",
    calibration_verified: bool = False,
    rod_length_cm: Optional[float] = None,
) -> None:
    obs_date = parse_date(str(observation_date)) if isinstance(observation_date, str) else observation_date
    plant_id_str = str(plant_id).strip()

    with get_connection(db_path) as conn:
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

        cols = [c[1] for c in conn.execute("PRAGMA table_info(observations)").fetchall()]
        has_source = "source" in cols
        src_col = "source" if has_source else "data_source"
        src_val = "real" if (not has_source and source not in ("real", "synthetic")) else source

        cols = [c[1] for c in conn.execute("PRAGMA table_info(observations)").fetchall()]
        has_calib = "calibration_source" in cols

        if has_calib:
            conn.execute(
                f"""
                INSERT INTO observations (
                    plant_id, observation_date, age_days, height_cm,
                    stem_width_cm, visible_true_leaf_count, growth_rate_cm_per_day, {src_col},
                    calibration_source, calibration_verified, rod_length_cm
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plant_id, observation_date) DO UPDATE SET
                    height_cm = excluded.height_cm,
                    stem_width_cm = COALESCE(excluded.stem_width_cm, observations.stem_width_cm),
                    visible_true_leaf_count = COALESCE(excluded.visible_true_leaf_count, observations.visible_true_leaf_count),
                    age_days = COALESCE(excluded.age_days, observations.age_days),
                    growth_rate_cm_per_day = excluded.growth_rate_cm_per_day,
                    {src_col} = excluded.{src_col},
                    calibration_source = COALESCE(excluded.calibration_source, observations.calibration_source),
                    calibration_verified = excluded.calibration_verified,
                    rod_length_cm = COALESCE(excluded.rod_length_cm, observations.rod_length_cm)
                """,
                (
                    plant_id_str,
                    obs_date.isoformat(),
                    age_days,
                    float(height_cm),
                    float(stem_width_cm) if stem_width_cm is not None else None,
                    int(leaf_count) if leaf_count is not None else None,
                    growth_rate,
                    src_val,
                    calibration_source,
                    1 if calibration_verified else 0,
                    rod_length_cm,
                ),
            )
        else:
            conn.execute(
                f"""
                INSERT INTO observations (
                    plant_id, observation_date, age_days, height_cm,
                    stem_width_cm, visible_true_leaf_count, growth_rate_cm_per_day, {src_col}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plant_id, observation_date) DO UPDATE SET
                    height_cm = excluded.height_cm,
                    stem_width_cm = COALESCE(excluded.stem_width_cm, observations.stem_width_cm),
                    visible_true_leaf_count = COALESCE(excluded.visible_true_leaf_count, observations.visible_true_leaf_count),
                    age_days = COALESCE(excluded.age_days, observations.age_days),
                    growth_rate_cm_per_day = excluded.growth_rate_cm_per_day,
                    {src_col} = excluded.{src_col}
                """,
                (
                    plant_id_str,
                    obs_date.isoformat(),
                    age_days,
                    float(height_cm),
                    float(stem_width_cm) if stem_width_cm is not None else None,
                    int(leaf_count) if leaf_count is not None else None,
                    growth_rate,
                    src_val,
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
    plant_id_str = str(plant_id).strip()
    with get_connection(db_path) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(readiness_events)").fetchall()]
        d_col = "readiness_observed_date" if "readiness_observed_date" in cols else "event_date"

        # Check if already recorded
        existing = conn.execute(
            f"SELECT 1 FROM readiness_events WHERE plant_id = ? AND {d_col} = ?",
            (plant_id_str, ev_date.isoformat()),
        ).fetchone()

        if existing:
            return

        if "readiness_definition_version" in cols:
            conn.execute(
                f"""
                INSERT INTO readiness_events (
                    plant_id, {d_col}, readiness_definition_version, assessment_source, data_source, notes
                ) VALUES (?, ?, 'v1', 'cli', 'real', ?)
                """,
                (plant_id_str, ev_date.isoformat(), f"{status}: {notes}" if notes else status),
            )
        else:
            conn.execute(
                f"""
                INSERT INTO readiness_events (plant_id, {d_col}, status, notes)
                VALUES (?, ?, ?, ?)
                """,
                (plant_id_str, ev_date.isoformat(), status, notes),
            )


def seed_from_csv(
    db_path: Path | str,
    csv_source: Union[Path, str, bytes, io.StringIO],
    species: str = "Sunflower (SuperOrange)",
    variety: str = "SuperOrange",
) -> int:
    """
    Seeds database from historical CSV file, path, or string content.
    Supports columns:
      - Plant_ID / plant_id / plant_ref
      - Date / date / observation_date
      - Age / age / age_days
      - Height_cm / height_cm / height
      - Stem_Width_cm / stem_width_cm (optional)
      - Leaf_Count / visible_true_leaf_count (optional)
      - readiness_observed_date / transplant_date (optional ground truth)
    """
    import io

    init_db(db_path)

    # Resolve input stream
    if isinstance(csv_source, bytes):
        f = io.StringIO(csv_source.decode("utf-8-sig", errors="replace"))
        should_close = True
    elif isinstance(csv_source, io.StringIO):
        f = csv_source
        should_close = False
    elif isinstance(csv_source, (str, Path)):
        p = Path(csv_source)
        if p.exists() and p.is_file():
            f = open(p, "r", encoding="utf-8-sig")
            should_close = True
        elif isinstance(csv_source, str) and "\n" in csv_source:
            f = io.StringIO(csv_source)
            should_close = True
        else:
            raise FileNotFoundError(f"CSV file not found: {csv_source}")
    else:
        raise TypeError(f"Unsupported csv_source type: {type(csv_source)}")

    rows_by_plant: Dict[str, List[Dict[str, Any]]] = {}
    readiness_by_plant: Dict[str, date] = {}

    try:
        reader = csv.DictReader(f)
        for raw_r in reader:
            r = {k.strip(): v.strip() for k, v in raw_r.items() if k and v is not None}
            p_id = r.get("Plant_ID") or r.get("plant_id") or r.get("plant_ref") or r.get("PlantID")
            if not p_id:
                continue
            p_id = str(p_id).strip()

            d_str = r.get("Date") or r.get("date") or r.get("observation_date") or r.get("Observation_Date")
            if not d_str:
                continue
            obs_date = parse_date(d_str)

            age_str = r.get("Age") or r.get("age") or r.get("age_days") or ""
            age_days = None
            if age_str:
                clean_age = "".join(ch for ch in str(age_str) if ch.isdigit())
                if clean_age:
                    age_days = int(clean_age)

            h_str = r.get("Height_cm") or r.get("height_cm") or r.get("Height") or r.get("height")
            if h_str is None or h_str == "":
                continue
            height_cm = float(h_str)

            stem_str = r.get("Stem_Width_cm") or r.get("stem_width_cm") or r.get("stem_width")
            stem_width = float(stem_str) if stem_str else None

            leaves_str = r.get("Leaf_Count") or r.get("visible_true_leaf_count") or r.get("leaves") or r.get("leaf_count")
            leaves = int(leaves_str) if leaves_str else None

            # Check optional explicit readiness/transplant label in row
            ready_str = (
                r.get("readiness_observed_date")
                or r.get("transplant_date")
                or r.get("readiness_date")
                or r.get("ready_date")
            )
            if ready_str and p_id not in readiness_by_plant:
                try:
                    readiness_by_plant[p_id] = parse_date(ready_str)
                except Exception:
                    pass

            if p_id not in rows_by_plant:
                rows_by_plant[p_id] = []
            rows_by_plant[p_id].append({
                "date": obs_date,
                "age_days": age_days,
                "height_cm": height_cm,
                "stem_width_cm": stem_width,
                "leaves": leaves,
            })
    finally:
        if should_close:
            f.close()

    total_inserted = 0
    from datetime import timedelta

    for plant_id, obs_list in rows_by_plant.items():
        obs_list.sort(key=lambda x: x["date"])
        resolved_pid = register_plant(db_path, plant_id, species, variety, cohort="reference")

        for item in obs_list:
            record_observation(
                db_path,
                plant_id=resolved_pid,
                observation_date=item["date"],
                height_cm=item["height_cm"],
                stem_width_cm=item["stem_width_cm"],
                leaf_count=item["leaves"],
                age_days=item["age_days"],
                source="real",
                calibration_source="csv_import_no_calibration",
                calibration_verified=False,
            )
            total_inserted += 1

        # Use explicit readiness date if provided; otherwise final monitoring day + 1 day
        if plant_id in readiness_by_plant:
            event_d = readiness_by_plant[plant_id]
        else:
            final_obs = obs_list[-1]["date"]
            event_d = final_obs + timedelta(days=1)

        record_readiness_event(
            db_path,
            plant_id=resolved_pid,
            event_date=event_d,
            status="transplanted",
            notes=f"Readiness event for {species}",
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


def species_cohort_snapshot(
    db_path: Path | str = DEFAULT_DB_PATH,
    species: Optional[str] = None,
    observation_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Raw observations for a species on one date (latest if omitted).

    Returns a dict with 'observation_date', 'rows', 'row_count', 'plants_represented'.
    Missing data is preserved; no rows are invented.
    """
    with get_connection(db_path) as conn:
        if observation_date:
            target_date = observation_date
        else:
            if species:
                row = conn.execute(
                    "SELECT MAX(o.observation_date) AS d FROM observations o JOIN plants p ON o.plant_id=p.plant_id WHERE p.species=?",
                    (species,),
                ).fetchone()
            else:
                row = conn.execute("SELECT MAX(observation_date) AS d FROM observations").fetchone()
            target_date = row["d"] if row and row["d"] else None

        if not target_date:
            return {"species": species, "observation_date": None, "rows": [], "row_count": 0, "plants_represented": 0}

        query = """
            SELECT o.*, p.species, p.variety
            FROM observations o JOIN plants p ON o.plant_id = p.plant_id
            WHERE o.observation_date = ?
        """
        params: List[Any] = [target_date]
        if species:
            query += " AND p.species = ?"
            params.append(species)
        query += " ORDER BY o.plant_id, o.id"
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        return {
            "species": species,
            "observation_date": target_date,
            "rows": rows,
            "row_count": len(rows),
            "plants_represented": len({r["plant_id"] for r in rows}),
        }


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


def normalize_species(raw: str | None) -> str:
    """Species comparison key: case and whitespace insensitive."""
    return re.sub(r"\s+", "", str(raw or "")).lower()


def get_plant(db_path: Path | str = DEFAULT_DB_PATH, plant_id: str | None = None) -> Optional[Dict[str, Any]]:
    """Fetch one plant row, or None when it does not exist."""
    if not plant_id:
        return None
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM plants WHERE plant_id = ?", (str(plant_id).strip(),)
        ).fetchone()
        return dict(row) if row else None


def find_plants_by_ref(db_path: Path | str = DEFAULT_DB_PATH, ref: str | None = None) -> List[Dict[str, Any]]:
    """Find all plants matching a normalized plant/external reference.

    Ambiguity is preserved: callers must treat len(result) > 1 as quarantined.
    """
    norm = normalize_plant_ref(ref)
    if not norm:
        return []
    return [
        p for p in get_plants(db_path)
        if normalize_plant_ref(p.get("plant_id")) == norm
        or normalize_plant_ref(p.get("external_ref")) == norm
    ]


def get_latest_observation(
    db_path: Path | str = DEFAULT_DB_PATH,
    plant_id: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Latest observation for one plant (None when the plant has none)."""
    if not plant_id:
        return None
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT o.*, p.species, p.variety, p.planted_date
            FROM observations o
            JOIN plants p ON o.plant_id = p.plant_id
            WHERE o.plant_id = ?
            ORDER BY o.observation_date DESC, o.id DESC
            LIMIT 1
            """,
            (str(plant_id).strip(),),
        ).fetchone()
        return dict(row) if row else None


def get_observation(
    db_path: Path | str = DEFAULT_DB_PATH,
    plant_id: str | None = None,
    observation_date: date | str | None = None,
) -> Optional[Dict[str, Any]]:
    """One plant's observation on one date, if present."""
    if not plant_id or not observation_date:
        return None
    obs_date = parse_date(str(observation_date)) if isinstance(observation_date, str) else observation_date
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT o.*, p.species, p.variety, p.planted_date
            FROM observations o
            JOIN plants p ON o.plant_id = p.plant_id
            WHERE o.plant_id = ? AND o.observation_date = ?
            """,
            (str(plant_id).strip(), obs_date.isoformat()),
        ).fetchone()
        return dict(row) if row else None


def get_cohort_stats(
    db_path: Path | str = DEFAULT_DB_PATH,
    species: str | None = None,
    exclude_plant_id: str | None = None,
) -> Dict[str, Any]:
    """Counts describing the historical cohort a prediction would use.

    The live plant being predicted is excluded so its own measurements never
    become its reference. Missing data is reported as 0 counts, never invented.
    """
    key = normalize_species(species)
    plants = [p for p in get_plants(db_path) if not key or normalize_species(p.get("species")) == key]
    plant_ids = {str(p["plant_id"]) for p in plants}
    if exclude_plant_id:
        plant_ids.discard(str(exclude_plant_id))

    observations = [
        o for o in get_observations(db_path)
        if str(o["plant_id"]) in plant_ids
    ]
    ages = [o["age_days"] for o in observations if o.get("age_days") is not None]

    with get_connection(db_path) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(readiness_events)").fetchall()]
        date_col = "readiness_observed_date" if "readiness_observed_date" in cols else "event_date"
        event_rows = conn.execute(
            f"SELECT plant_id, {date_col} AS event_date FROM readiness_events"
        ).fetchall()
    events = [dict(r) for r in event_rows if str(r["plant_id"]) in plant_ids]

    latest = max((str(o["observation_date"]) for o in observations), default=None)
    return {
        "species": species,
        "n_plants": len(plant_ids),
        "n_observations": len(observations),
        "n_readiness_events": len(events),
        "n_readiness_plants": len({str(e["plant_id"]) for e in events}),
        "min_age_days": min(ages) if ages else None,
        "max_age_days": max(ages) if ages else None,
        "latest_observation_date": latest,
    }


def get_predictions(
    db_path: Path | str = DEFAULT_DB_PATH,
    plant_id: str | None = None,
    species: str | None = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Newest-first prediction history (append-only; never rewritten)."""
    query = "SELECT * FROM predictions WHERE 1=1"
    params: List[Any] = []
    if plant_id:
        query += " AND plant_id = ?"
        params.append(str(plant_id).strip())
    if species:
        query += " AND REPLACE(species, ' ', '') = ?"
        params.append(species.replace(" ", ""))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with get_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_feedback_for_prediction(
    db_path: Path | str = DEFAULT_DB_PATH,
    prediction_id: int | None = None,
) -> List[Dict[str, Any]]:
    if prediction_id is None:
        return []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE prediction_id = ? ORDER BY id DESC",
            (prediction_id,),
        ).fetchall()
        return [dict(r) for r in rows]

