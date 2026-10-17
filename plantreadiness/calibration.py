"""Calibration provenance: what the rod length was, whether it was verified,
and which measurements are allowed to feed a growth baseline.

The physical reference-rod length is *configuration*, not code - see
:data:`plantreadiness.config.CAMERA_ROD_LENGTH_CM`. This module answers the
two questions the rest of the system must not guess at:

1. **Was the calibration used for this measurement verified?** The configured
   rod length is unverified until someone measures the real rod and records it
   with :mod:`plantreadiness.verify_calibration`. A verification is tied to the
   length that was measured: verifying 15 cm does not verify a later
   configuration of 10 cm.

2. **May this measurement enter the growth baseline?** Measurements from
   incompatible or unverified calibrations are never averaged together. They are
   kept, shown and counted, but quarantined out of the baseline.

Calibration status and measurement provenance are deliberately separate axes.
A human-recorded dataset has no rod calibration to verify - which is *not* the
same as being trustworthy. Its provenance stays whatever the provider asserted
(:data:`config.PROVENANCE_PROVIDER_REPORTED`) and it is never described as a
validated model.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from . import config

# Human-readable labels for the UI / reports.
CALIBRATION_GROUP_LABELS = {
    config.CALIBRATION_GROUP_MANUAL:
        "Manual measurement (no camera calibration)",
    config.CALIBRATION_GROUP_CAMERA_VERIFIED:
        "Camera measurement, verified rod length",
    config.CALIBRATION_GROUP_CAMERA_UNVERIFIED:
        "Camera measurement, UNVERIFIED rod length",
    config.CALIBRATION_GROUP_UNKNOWN:
        "Unknown provenance",
}

_VERIFICATION_TOLERANCE_CM = 1e-6


# --------------------------------------------------------------------------- #
# Classifying one measurement
# --------------------------------------------------------------------------- #


def calibration_group(*, calibration_source: Optional[str],
                      rod_length_cm: Optional[float] = None,
                      calibration_verified: Optional[bool] = None) -> str:
    """Classify one stored measurement into a calibration group.

    ``calibration_source`` mirrors ``observations.calibration_source`` /
    ``camera_captures.calibration_source``. A camera measurement is only
    "verified" when the rod length used was positively verified at the time.
    """
    source = (calibration_source or "").strip() or config.CALIBRATION_SOURCE_UNKNOWN

    if source == config.CALIBRATION_SOURCE_CAMERA:
        if calibration_verified and rod_length_cm is not None:
            return config.CALIBRATION_GROUP_CAMERA_VERIFIED
        return config.CALIBRATION_GROUP_CAMERA_UNVERIFIED

    if source == config.CALIBRATION_SOURCE_CSV:
        return config.CALIBRATION_GROUP_MANUAL

    return config.CALIBRATION_GROUP_UNKNOWN


def group_for_row(row: dict[str, Any]) -> str:
    """Calibration group for a stored observation/capture row."""
    return calibration_group(
        calibration_source=row.get("calibration_source"),
        rod_length_cm=row.get("rod_length_cm"),
        calibration_verified=row.get("calibration_verified"),
    )


def is_baseline_usable(group: str) -> bool:
    """Whether a calibration group may contribute to the growth baseline."""
    return group in config.BASELINE_USABLE_CALIBRATION_GROUPS


def group_label(group: str) -> str:
    return CALIBRATION_GROUP_LABELS.get(group, group)


# --------------------------------------------------------------------------- #
# Verification state
# --------------------------------------------------------------------------- #


def verification_applies(state: Optional[dict[str, Any]],
                         configured_rod_length_cm: Optional[float] = None
                         ) -> bool:
    """Whether a recorded verification covers the rod length in use *now*.

    The verification is tied to the specific length that was measured. When the
    configuration changes, the previous verification no longer applies and the
    new value starts out unverified again.
    """
    if not state or not state.get("verified"):
        return False
    recorded = state.get("rod_length_cm")
    if recorded is None:
        return False
    configured = (config.CAMERA_ROD_LENGTH_CM
                  if configured_rod_length_cm is None
                  else configured_rod_length_cm)
    return math.isclose(float(recorded), float(configured),
                        abs_tol=_VERIFICATION_TOLERANCE_CM)


def describe(state: Optional[dict[str, Any]],
             configured_rod_length_cm: Optional[float] = None) -> dict[str, Any]:
    """A plain-language summary of the current calibration state."""
    configured = (config.CAMERA_ROD_LENGTH_CM
                  if configured_rod_length_cm is None
                  else configured_rod_length_cm)
    verified = verification_applies(state, configured)
    out: dict[str, Any] = {
        "configured_rod_length_cm": configured,
        "verified": verified,
        "verified_rod_length_cm": (state or {}).get("rod_length_cm"),
        "verified_at": (state or {}).get("verified_at"),
        "verified_by": (state or {}).get("verified_by"),
        "note": (state or {}).get("note"),
        "baseline_usable_group": config.CALIBRATION_GROUP_CAMERA_VERIFIED
        if verified else None,
    }
    if verified:
        out["message"] = (
            f"Rod length {configured:g} cm is verified; camera measurements "
            "share the verified calibration group."
        )
    elif state and state.get("rod_length_cm") is not None:
        out["message"] = (
            f"The rod was verified at {float(state['rod_length_cm']):g} cm but "
            f"{configured:g} cm is configured now, so the configured value is "
            "unverified again. Camera measurements stay quarantined from the "
            "growth baseline until the current length is measured."
        )
    else:
        out["message"] = (
            f"Rod length {configured:g} cm is configured but UNVERIFIED. "
            "Camera measurements are collected and stored, but excluded from "
            "the growth baseline until the physical rod is measured and "
            "recorded."
        )
    return out


# --------------------------------------------------------------------------- #
# Reporting the quarantine
# --------------------------------------------------------------------------- #


def summarise_groups(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count rows per calibration group and what is excluded from a baseline."""
    counts: dict[str, int] = {g: 0 for g in config.ALL_CALIBRATION_GROUPS}
    for row in rows:
        counts[group_for_row(row)] += 1

    usable = sum(counts[g] for g in config.BASELINE_USABLE_CALIBRATION_GROUPS)
    quarantined = sum(
        counts[g] for g in config.ALL_CALIBRATION_GROUPS
        if g not in config.BASELINE_USABLE_CALIBRATION_GROUPS)
    return {
        "counts_by_group": counts,
        "usable_rows": usable,
        "quarantined_rows": quarantined,
        "baseline_usable_groups": list(config.BASELINE_USABLE_CALIBRATION_GROUPS),
    }
