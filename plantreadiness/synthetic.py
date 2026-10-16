"""SYNTHETIC DEMONSTRATION DATA GENERATOR (Mode A).

.. warning::
   Everything produced by this module is **simulated**. The growth curves,
   readiness rules and survival probabilities below are *configurable
   assumptions chosen to exercise the software*, NOT discovered biological
   facts. Outputs must always be labelled ``SYNTHETIC DEMO``.

The generator is seeded and therefore reproducible. It creates, for several
species/varieties:

* plants and daily growth observations,
* readiness events (known in the simulation, so a countdown target exists),
* transplant dates and 14-day feedback outcomes,
* extra "future" plants that can be revealed later to retrain the demo model.

Readiness rule (assumption, documented):
    a simulated plant becomes "transplant ready" when it first satisfies
    ``height >= species.height_ready_cm`` AND ``age >= species.min_ready_age``
    on a daily assessment - this stands in for a human assessment protocol.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from . import db as dbm

DEMO_READINESS_DEFINITION = "synthetic-demo-readiness-v1"
DEMO_ASSESSMENT_SOURCE = "synthetic_demo_generator"


@dataclass
class SpeciesProfile:
    """Configurable, *assumed* growth/readiness parameters for one species."""
    name: str
    varieties: list[str]
    base_height_cm: float          # height at emergence
    daily_growth_cm: float         # mean linear growth per day
    growth_noise_cm: float         # daily measurement noise
    ready_height_cm: float         # assumed readiness height threshold
    min_ready_age_days: int        # assumed minimum age before readiness
    ready_age_jitter_days: int     # plant-to-plant variation
    logistic_cap_cm: Optional[float] = None   # optional saturating growth
    logistic_k: float = 0.12


@dataclass
class DemoConfig:
    """Master configuration for the synthetic demonstration."""
    seed: int = 42
    start_date: date = date(2083, 4, 24)
    plants_per_species_variety: int = 6
    extra_retrain_plants_per_species_variety: int = 3
    observe_days_after_readiness: int = 3
    transplant_delay_days: int = 2
    feedback_survival_prob: float = 0.9
    # probability that a very late transplant still survives (assumption)
    feedback_late_death_threshold_days: int = 10
    species: list[SpeciesProfile] = field(default_factory=lambda: [
        SpeciesProfile(
            name="Super Orange",
            varieties=["Early", "Late"],
            base_height_cm=0.3,
            daily_growth_cm=0.28,
            growth_noise_cm=0.06,
            ready_height_cm=6.5,
            min_ready_age_days=24,
            ready_age_jitter_days=4,
            logistic_cap_cm=9.0,
        ),
        SpeciesProfile(
            name="Demo Bean",
            varieties=["Climbing", "Bush"],
            base_height_cm=1.0,
            daily_growth_cm=0.9,
            growth_noise_cm=0.15,
            ready_height_cm=12.0,
            min_ready_age_days=12,
            ready_age_jitter_days=3,
        ),
    ])


# --------------------------------------------------------------------------- #
# Core simulation
# --------------------------------------------------------------------------- #


def _height_on_day(profile: SpeciesProfile, age_days: int,
                   rng: random.Random) -> float:
    """Assumed growth curve: logistic if a cap is set, else linear, + noise."""
    if profile.logistic_cap_cm:
        # logistic around half the cap
        raw = profile.logistic_cap_cm / (
            1.0 + math.exp(-profile.logistic_k * (age_days - 18))
        )
        base = max(profile.base_height_cm, raw)
    else:
        base = profile.base_height_cm + profile.daily_growth_cm * age_days
    noise = rng.gauss(0.0, profile.growth_noise_cm)
    return max(0.0, round(base + noise, 3))


def _stem_width_on_day(height_cm: float, rng: random.Random) -> float:
    """Simulated stem width - a rough fraction of height, plus noise.

    Simulated only. The real pipeline measures stem width from the camera
    (``observations.stem_width_cm``); this exists so the DEMO model has a
    stem-width signal to learn from.
    """
    return max(0.0, round(height_cm * 0.10 + rng.gauss(0.0, 0.02), 3))


def _find_readiness_age(profile: SpeciesProfile, rng: random.Random) -> int:
    """First daily assessment at which the assumed rule is satisfied."""
    jitter = rng.randint(-profile.ready_age_jitter_days,
                         profile.ready_age_jitter_days)
    target_age = max(profile.min_ready_age_days, profile.min_ready_age_days + jitter)
    age = max(1, target_age - 5)
    while age < 400:
        # simulated height with a fresh noise draw each assessment
        if _height_on_day(profile, age, rng) >= profile.ready_height_cm \
                and age >= profile.min_ready_age_days:
            return age
        age += 1
    return age


@dataclass
class SimPlant:
    species: str
    variety: str
    date_started: date
    readiness_age: int
    observations: list[dict]          # daily dicts up to readiness+extra
    readiness_date: date
    transplant_date: Optional[date]
    feedback: Optional[dict]          # outcome dict or None (not yet due)
    label: str                        # 'initial' | 'retrain'
    external_ref: str


def generate_demo_cohort(config: DemoConfig,
                         label: str = "initial",
                         ref_prefix: str = "SYN",
                         start_age: int = 8) -> list[SimPlant]:
    """Generate one reproducible cohort of simulated plants."""
    # Deterministic per-cohort seed: str.__hash__ is randomised per process,
    # so derive the seed explicitly instead of hashing a tuple.
    cohort_seed = f"{config.seed}|{label}|{start_age}".encode()
    rng = random.Random(int.from_bytes(
        hashlib.sha256(cohort_seed).digest()[:8], "big"))
    plants: list[SimPlant] = []
    counter = 0

    for profile in config.species:
        for variety in profile.varieties:
            for _ in range(config.plants_per_species_variety
                           if label == "initial"
                           else config.extra_retrain_plants_per_species_variety):
                counter += 1
                date_started = config.start_date - timedelta(days=start_age)
                readiness_age = _find_readiness_age(profile, rng)
                readiness_date = date_started + timedelta(days=readiness_age)

                # daily observations from start_age up to readiness (+ a few
                # days of post-readiness monitoring that must NOT be used as
                # prediction features after readiness)
                observations = []
                last_age = readiness_age + config.observe_days_after_readiness
                prev_height = None
                for age in range(start_age, last_age + 1):
                    h = _height_on_day(profile, age, rng)
                    day = date_started + timedelta(days=age)
                    rate = None
                    if prev_height is not None:
                        rate = round(h - prev_height, 4)
                    observations.append({
                        "observation_date": day.isoformat(),
                        "age_days": age,
                        "height_cm": h,
                        "stem_width_cm": _stem_width_on_day(h, rng),
                        "growth_rate_cm_per_day": rate,
                    })
                    prev_height = h

                # transplant occurs after readiness (delay days)
                transplant_date = readiness_date + timedelta(
                    days=config.transplant_delay_days)

                # ---- 14-day feedback outcome (assumption) -----------------
                late_days = (transplant_date - readiness_date).days
                survived = rng.random() < (
                    config.feedback_survival_prob if late_days <= 2
                    else config.feedback_survival_prob * 0.5
                    if late_days <= config.feedback_late_death_threshold_days
                    else 0.3
                )
                # height at transplant from the simulated curve
                h_transplant = _height_on_day(
                    profile, (transplant_date - date_started).days, rng)
                due = transplant_date + timedelta(days=14)
                if survived:
                    # 14-day growth assumption: continues at ~80% of rate
                    after = round(
                        h_transplant
                        + 14 * profile.daily_growth_cm * 0.8
                        + rng.gauss(0, profile.growth_noise_cm), 3)
                    after = max(after, 0.0)
                    condition = ("healthy"
                                 if after > h_transplant
                                 else "stressed_wilting")
                else:
                    after = None
                    condition = "dead"

                feedback = {
                    "transplant_date": transplant_date.isoformat(),
                    "feedback_due_date": due.isoformat(),
                    "submitted": True,
                    "submitted_date": due.isoformat(),   # submitted on due date
                    "survived": survived,
                    "height_at_transplant_cm": h_transplant,
                    "height_after_14d_cm": after,
                    "visible_condition": condition,
                    "notes": "SIMULATED 14-day outcome (synthetic demo).",
                }

                plants.append(SimPlant(
                    species=profile.name,
                    variety=variety,
                    date_started=date_started,
                    readiness_age=readiness_age,
                    observations=observations,
                    readiness_date=readiness_date,
                    transplant_date=transplant_date,
                    feedback=feedback,
                    label=label,
                    external_ref=f"{ref_prefix}-{label[:3].upper()}-{counter:03d}",
                ))
    return plants


# --------------------------------------------------------------------------- #
# Loading into the database
# --------------------------------------------------------------------------- #


def load_cohort_into_db(conn, plants: list[SimPlant],
                        with_feedback: bool = True) -> dict:
    """Persist a synthetic cohort with ``data_source='synthetic'``.

    Readiness events ARE recorded for demo plants (that is the point of the
    demo: simulated plants have *known* readiness so a countdown can be
    learned). Feedback rows are created but only marked submitted when
    ``with_feedback`` is True.
    """
    stats = {"plants": 0, "observations": 0, "readiness_events": 0,
             "feedback_records": 0, "feedback_submitted": 0}

    for sp in plants:
        try:
            pid = dbm.register_plant(
                conn,
                species=sp.species,
                variety=sp.variety,
                date_started=sp.date_started.isoformat(),
                data_source="synthetic",
                growing_conditions="simulated_demo_conditions",
                external_ref=sp.external_ref,
                cohort=sp.label,
            )
        except dbm.DuplicateRecordError:
            # already loaded (idempotent re-runs)
            row = conn.execute(
                """SELECT plant_id FROM plants
                   WHERE external_ref = ? AND data_source = 'synthetic'""",
                (sp.external_ref,)).fetchone()
            pid = int(row["plant_id"])
        stats["plants"] += 1

        for obs in sp.observations:
            try:
                dbm.add_observation(
                    conn, plant_id=pid,
                    observation_date=obs["observation_date"],
                    age_days=obs["age_days"],
                    height_cm=obs["height_cm"],
                    stem_width_cm=obs["stem_width_cm"],
                    growth_rate_cm_per_day=obs["growth_rate_cm_per_day"],
                    data_source="synthetic",
                )
                stats["observations"] += 1
            except dbm.DuplicateRecordError:
                pass

        try:
            dbm.record_readiness_event(
                conn, plant_id=pid,
                readiness_observed_date=sp.readiness_date.isoformat(),
                readiness_definition_version=DEMO_READINESS_DEFINITION,
                assessment_source=DEMO_ASSESSMENT_SOURCE,
                readiness_observed_age_days=sp.readiness_age,
                data_source="synthetic",
                notes="SIMULATED readiness event (synthetic demo).",
            )
            stats["readiness_events"] += 1
        except dbm.DuplicateRecordError:
            pass

        if with_feedback and sp.feedback:
            fb = sp.feedback
            try:
                rec = dbm.record_transplant(
                    conn, plant_id=pid,
                    transplant_date=fb["transplant_date"],
                    height_at_transplant_cm=fb["height_at_transplant_cm"],
                    data_source="synthetic",
                )
                stats["feedback_records"] += 1
                if fb["submitted"]:
                    dbm.submit_feedback(
                        conn, feedback_id=rec["feedback_id"],
                        feedback_submitted_date=fb["submitted_date"],
                        survived_after_14d=fb["survived"],
                        height_after_14d_cm=fb["height_after_14d_cm"],
                        visible_condition=fb["visible_condition"],
                        user_notes=fb["notes"],
                    )
                    stats["feedback_submitted"] += 1
            except (dbm.DuplicateRecordError, dbm.ValidationError):
                pass

    return stats
