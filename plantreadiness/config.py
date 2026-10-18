"""Central configuration for the plant-readiness prototype."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

from .dotenv import load_dotenv

# Local secrets (Cloudinary keys, CAMERA_SOURCES, ...) live in a gitignored
# ``.env`` at the project root. Real environment variables always win.
load_dotenv()

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent


def normalize_plant_ref(value) -> str:
    """Canonical key for an external plant reference.

    Case-, whitespace- and punctuation-insensitive so that harmless variations
    such as ``Plant01``, ``plant01``, ``Plant-01`` and ``Plant_01`` all
    resolve to the same key. Numeric zero-padding is preserved, so
    ``Plant01`` and ``Plant1`` stay distinct. Species/variety are never
    inferred from this value.
    """
    if value is None:
        return ""
    return "".join(ch for ch in str(value).strip().lower() if ch.isalnum())
DEFAULT_DB_PATH = BASE_DIR / "plant_readiness.db"
MODELS_DIR = BASE_DIR / "models"
REFERENCE_DIR = MODELS_DIR / "reference"
# Committed input data lives under data/ so the repo root stays clean.
DATA_DIR = BASE_DIR / "data"
GENERATED_DIR = DATA_DIR / "generated"
DEFAULT_REAL_CSV = DATA_DIR / "super_orange_growth_data.csv"

# Synthetic demonstration runs must never write into the real database or the
# committed models/ directory, so they get their own scratch locations.
DEMO_DB_PATH = BASE_DIR / "plant_readiness_demo.db"
DEMO_MODELS_DIR = BASE_DIR / "models_demo"

# --------------------------------------------------------------------------- #
# Camera / cloud (ESP32-CAM -> Cloudinary -> OpenCV) refresh
# --------------------------------------------------------------------------- #
# Where the vision pipeline appends daily measurements that the refresh imports.
# Generated runtime output: kept out of the repo root and git-ignored.
CAMERA_CSV_PATH = GENERATED_DIR / "plant_daily_measurements.csv"

# --------------------------------------------------------------------------- #
# Reference-rod calibration (the ONE authoritative value)
# --------------------------------------------------------------------------- #
# ``CAMERA_ROD_LENGTH_CM`` is the single source of truth for the pixel -> cm
# calibration. The rod is a physical object in front of the camera, so the
# value is environment-specific and lives in the gitignored ``.env``. The
# constant below is only the documented fallback for a checkout with no ``.env``
# - nothing else in the codebase may hardcode 15 or 30.
#
# The configured value is *not* assumed to be physically correct. It stays
# UNVERIFIED until ``python -m plantreadiness.verify_calibration`` records the
# length that was actually measured; measurements taken under an unverified
# calibration are quarantined from the growth baseline. See
# :mod:`plantreadiness.calibration`.
DEFAULT_ROD_LENGTH_CM = 30.0


def positive_float_setting(name: str, default: float) -> float:
    """Read a strictly positive, finite float from the environment.

    Validation happens at import time so an invalid rod length is refused
    before any image is processed, rather than silently producing a wrong
    pixel -> cm scale.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a number of centimetres, got {raw!r}."
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} must be a positive, finite number of centimetres, "
            f"got {raw!r}."
        )
    return value


def positive_int_setting(name: str, default: int) -> int:
    """Read a strictly positive integer from the environment."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a whole number, got {raw!r}."
        ) from exc
    if value <= 0:
        raise ValueError(f"{name} must be at least 1, got {raw!r}.")
    return value


CAMERA_ROD_LENGTH_CM = positive_float_setting(
    "CAMERA_ROD_LENGTH_CM", DEFAULT_ROD_LENGTH_CM
)

CAMERA_ROD_HUE = int(os.getenv("CAMERA_ROD_HUE", "0"))
CAMERA_ROD_HUE_TOLERANCE = int(os.getenv("CAMERA_ROD_HUE_TOLERANCE", "12"))
CAMERA_CAPTURES_PER_REFRESH = int(os.getenv("CAMERA_CAPTURES_PER_REFRESH", "4"))

# Minimum seconds between automatic refreshes, so a page load or a predict
# cannot hammer Cloudinary. 0 disables throttling.
CAMERA_MIN_REFRESH_INTERVAL_SECONDS = float(
    os.getenv("CAMERA_MIN_REFRESH_INTERVAL_SECONDS", "60")
)

# --------------------------------------------------------------------------- #
# Calibration provenance + verification
# --------------------------------------------------------------------------- #
# Where a measurement's calibration actually came from. Stored on every
# observation and capture so historical heights stay traceable.
CALIBRATION_SOURCE_CAMERA = "camera_configured"        # camera + configured rod
CALIBRATION_SOURCE_CSV = "csv_import_no_calibration"   # human-recorded dataset
CALIBRATION_SOURCE_UNKNOWN = "unknown"                 # legacy, no provenance

# Calibration groups used when building a growth baseline. Measurements from
# different groups are NEVER mixed into one baseline.
CALIBRATION_GROUP_MANUAL = "manual_measurement"
CALIBRATION_GROUP_CAMERA_VERIFIED = "camera_verified"
CALIBRATION_GROUP_CAMERA_UNVERIFIED = "camera_unverified"
CALIBRATION_GROUP_UNKNOWN = "unknown"

ALL_CALIBRATION_GROUPS = (
    CALIBRATION_GROUP_MANUAL,
    CALIBRATION_GROUP_CAMERA_VERIFIED,
    CALIBRATION_GROUP_CAMERA_UNVERIFIED,
    CALIBRATION_GROUP_UNKNOWN,
)

# Groups that may contribute to the growth baseline. An unverified camera
# calibration (the rod length has not been measured) and rows with no recorded
# provenance are quarantined by design - they are still stored and shown, but
# never averaged into a reference baseline.
BASELINE_USABLE_CALIBRATION_GROUPS = (
    CALIBRATION_GROUP_MANUAL,
    CALIBRATION_GROUP_CAMERA_VERIFIED,
)

# --------------------------------------------------------------------------- #
# Measurement provenance (asserted vs verified)
# --------------------------------------------------------------------------- #
# Calibration status and measurement provenance are two independent axes. A
# human-recorded dataset has no rod calibration to verify, but that does NOT
# make its provenance trustworthy - it stays whatever the provider asserted.
PROVENANCE_PROVIDER_REPORTED = "provider_reported_unverified"
PROVENANCE_VERIFIED = "verified"

# --------------------------------------------------------------------------- #
# Four-capture daily aggregation
# --------------------------------------------------------------------------- #
# Minimum valid captures before a daily record is treated as confident. A
# single successful image is never presented as a four-image aggregate.
MIN_CAPTURES_FOR_CONFIDENCE = 3

# A capture is only counted as valid when its segmentation confidence exceeds
# this. The default (0.0) means "the pipeline reported a confidence at all" -
# no invented quality bar.
CAPTURE_MIN_CONFIDENCE = float(os.getenv("CAPTURE_MIN_CONFIDENCE", "0.0"))

# Cloudinary public IDs produced by the ESP32 look like Plant01_20261016_095000.
CAPTURE_NAME_RE = re.compile(
    r"^(?P<ref>[A-Za-z]+\d+)_(?P<date>\d{8})_(?P<time>\d{6})$"
)

# How many images are expected per plant per day. Used only to mark a day
# `incomplete`; a day is never assumed complete when this is unknown.
EXPECTED_CAPTURES_PER_DAY = positive_int_setting(
    "CAMERA_CAPTURES_PER_REFRESH", 4
)

# --------------------------------------------------------------------------- #
# Manual dataset upload guardrails
# --------------------------------------------------------------------------- #
MAX_UPLOAD_BYTES = positive_int_setting("MAX_UPLOAD_BYTES", 5_000_000)
MAX_UPLOAD_ROWS = positive_int_setting("MAX_UPLOAD_ROWS", 20_000)
ALLOWED_DATASET_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
GROWTH_RATE_ADJUSTMENT_MIN_FACTOR = 0.5
GROWTH_RATE_ADJUSTMENT_MAX_FACTOR = 2.0

FEEDBACK_WINDOW_DAYS = 14

VALID_SOURCES = ("real", "synthetic")
VISIBLE_CONDITIONS = ("healthy", "stressed_wilting", "dead")

# Readiness assessment outcomes (user's judgement, kept separate from the
# system prediction and from recorded readiness *events*).
READINESS_ASSESSMENT_STATUSES = ("ready", "not_ready", "uncertain")

# A predicted readiness date within this many days of the observed transplant
# date is treated as "matched the observed transplant timing".
EVALUATION_MATCH_TOLERANCE_DAYS = 3

# Species catalogue. ``DEFAULT_SPECIES`` is the one used when a caller does not
# name one; the legacy name ``Super Orange`` is kept only as an alias so old
# imports keep working, and is migrated to the new name by
# ``plantreadiness.migrate_species_rename``.
SPECIES_SUNFLOWER_SUPER_ORANGE = "Sunflower (SuperOrange)"
SPECIES_MARIGOLD_APSARA_YELLOW = "Marigold (Apsara Yellow)"
LEGACY_SPECIES_SUPER_ORANGE = "Super Orange"
DEFAULT_SPECIES = SPECIES_SUNFLOWER_SUPER_ORANGE
CONFIGURED_SPECIES = (SPECIES_SUNFLOWER_SUPER_ORANGE,
                      SPECIES_MARIGOLD_APSARA_YELLOW)

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
