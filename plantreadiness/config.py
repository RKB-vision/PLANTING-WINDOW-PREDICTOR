"""Central configuration for the plant-readiness prototype."""

from __future__ import annotations

import os
from pathlib import Path

from .dotenv import load_dotenv

# Local secrets (Cloudinary keys, CAMERA_SOURCES, ...) live in a gitignored
# ``.env`` at the project root. Real environment variables always win.
load_dotenv()

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "plant_readiness.db"
MODELS_DIR = BASE_DIR / "models"
REFERENCE_DIR = MODELS_DIR / "reference"
DEFAULT_REAL_CSV = BASE_DIR / "super_orange_growth_data.csv"

# Synthetic demonstration runs must never write into the real database or the
# committed models/ directory, so they get their own scratch locations.
DEMO_DB_PATH = BASE_DIR / "plant_readiness_demo.db"
DEMO_MODELS_DIR = BASE_DIR / "models_demo"

# --------------------------------------------------------------------------- #
# Camera / cloud (ESP32-CAM -> Cloudinary -> OpenCV) refresh
# --------------------------------------------------------------------------- #
# Where the vision pipeline appends daily measurements that the refresh imports.
CAMERA_CSV_PATH = BASE_DIR / "plant_daily_measurements.csv"

# Vision calibration / segmentation defaults used by the cloud refresh.
CAMERA_ROD_LENGTH_CM = float(os.getenv("CAMERA_ROD_LENGTH_CM", "30"))
CAMERA_ROD_HUE = int(os.getenv("CAMERA_ROD_HUE", "0"))
CAMERA_ROD_HUE_TOLERANCE = int(os.getenv("CAMERA_ROD_HUE_TOLERANCE", "12"))
CAMERA_CAPTURES_PER_REFRESH = int(os.getenv("CAMERA_CAPTURES_PER_REFRESH", "4"))

# Minimum seconds between automatic refreshes, so a page load or a predict
# cannot hammer Cloudinary. 0 disables throttling.
CAMERA_MIN_REFRESH_INTERVAL_SECONDS = float(
    os.getenv("CAMERA_MIN_REFRESH_INTERVAL_SECONDS", "60")
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
FEEDBACK_WINDOW_DAYS = 14

VALID_SOURCES = ("real", "synthetic")
VISIBLE_CONDITIONS = ("healthy", "stressed_wilting", "dead")

# Readiness assessment outcomes (user's judgement, kept separate from the
# system prediction and from recorded readiness *events*).
READINESS_ASSESSMENT_STATUSES = ("ready", "not_ready", "uncertain")

# A predicted readiness date within this many days of the observed transplant
# date is treated as "matched the observed transplant timing".
EVALUATION_MATCH_TOLERANCE_DAYS = 3

DEFAULT_SPECIES = "Super Orange"

# Minimum configured data requirements before a model may be trained/validated.
MIN_INDEPENDENT_PLANTS = 4
MIN_READINESS_EVENTS = 6

# Successful-cohort reference baseline (provisional, NOT a validated model).
# A provisional estimate is only offered when this many independent plants
# contribute to the reference group it is read from.
MIN_PROVISIONAL_PLANTS = 3
# How far the requested age / height may sit from a reference group.
PROVISIONAL_AGE_TOLERANCE_DAYS = 3
PROVISIONAL_HEIGHT_TOLERANCE_CM = 1.5

# How the historical transplant reference is derived when no verified
# transplant date is recorded for a plant.
TRANSPLANT_ASSUMPTION_RULE = (
    "transplant_date = day after the plant's final monitoring day "
    "(assumed for the successful cohort; not an independently verified "
    "transplant date)"
)

METHOD_VALIDATED_MODEL = "validated_model"
METHOD_SYNTHETIC_DEMO = "synthetic_demo"
METHOD_REFERENCE_BASELINE = "historical_reference_baseline"
METHOD_UNAVAILABLE = "insufficient_evidence"

PROVISIONAL_BASELINE_MESSAGE = (
    "Provisional estimate from the successful-cohort reference baseline. "
    "This is descriptive historical timing, NOT a validated readiness model, "
    "and the reference transplant date is assumed rather than verified."
)

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


# --------------------------------------------------------------------------- #
# Camera source helpers (explicit registration; never auto-create plants)
# --------------------------------------------------------------------------- #
def camera_credentials_present() -> bool:
    """True when Cloudinary admin credentials are configured server-side."""
    return bool(
        os.getenv("CLOUDINARY_CLOUD_NAME")
        and os.getenv("CLOUDINARY_API_KEY")
        and os.getenv("CLOUDINARY_API_SECRET")
    )


def camera_sources() -> list[tuple[str, str]]:
    """Configured Cloudinary prefix -> registered plant external_ref mapping.

    Format::

        CAMERA_SOURCES="Plant01_=Plant-01,Plant02_=Plant-02"

    Each referenced plant must already be registered (explicit registration).
    The refresh never auto-creates plants; unmatched prefixes are skipped.
    """
    raw = os.getenv("CAMERA_SOURCES", "").strip()
    if not raw:
        return []

    sources: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        prefix, ref = chunk.split("=", 1)
        prefix = prefix.strip()
        ref = ref.strip()
        if prefix and ref:
            sources.append((prefix, ref))
    return sources


def camera_refresh_enabled() -> bool:
    """Whether the backend may refresh camera observations before prediction.

    An explicit ``CAMERA_REFRESH_ENABLED`` always wins. Otherwise the refresh
    is enabled only when credentials and at least one registered source exist,
    so an unconfigured checkout stays a safe no-op.
    """
    flag = os.getenv("CAMERA_REFRESH_ENABLED")
    if flag is not None:
        return flag.strip().lower() in ("1", "true", "yes", "on")
    return bool(camera_credentials_present() and camera_sources())
