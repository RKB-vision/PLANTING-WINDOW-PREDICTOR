"""Central configuration for the plant-readiness prototype."""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "plant_readiness.db"
MODELS_DIR = BASE_DIR / "models"
DEFAULT_REAL_CSV = BASE_DIR / "super_orange_growth_data_with_growth_rate.csv"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
FEEDBACK_WINDOW_DAYS = 14

VALID_SOURCES = ("real", "synthetic")
VISIBLE_CONDITIONS = ("healthy", "stressed_wilting", "dead")

DEFAULT_SPECIES = "Super Orange"

# Minimum configured data requirements before a model may be trained/validated.
MIN_INDEPENDENT_PLANTS = 4
MIN_READINESS_EVENTS = 6

# --------------------------------------------------------------------------- #
# Standard messages (kept verbatim so UI, tests and docs agree)
# --------------------------------------------------------------------------- #
SYNTHETIC_BANNER = (
    "*** SYNTHETIC DEMO: data and/or model below are SIMULATED - "
    "not validated botanical results ***"
)

UNAVAILABLE_MESSAGE = (
    "Readiness prediction unavailable: verified readiness-event data "
    "is not yet sufficient."
)

TARGET_DEFINITION = (
    "days_remaining = readiness_observed_date - observation_date "
    "(elapsed calendar days; only observations recorded at or before the "
    "readiness event are used as training rows)"
)

NO_SYNTHETIC_FALLBACK_NOTE = (
    "Synthetic models are never used for real plants unless the user "
    "explicitly enables provisional synthetic predictions."
)
