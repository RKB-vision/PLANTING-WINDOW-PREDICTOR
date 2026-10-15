"""Shared display helpers used by every page.

Centralising banners and formatting here guarantees the honest-refusal
message and the ``SYNTHETIC DEMO`` label look and read the same everywhere
- they are rendered from :mod:`plantreadiness.config`, never re-typed.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import streamlit as st

from plantreadiness import config

SOURCE_LABELS = {"real": "REAL DATA", "synthetic": "SYNTHETIC DEMO"}


def source_badge(data_source: Optional[str]) -> str:
    """Coloured markdown badge for a row's data source."""
    if data_source == "synthetic":
        return f":orange[**{config.SYNTHETIC_BANNER}**]"
    if data_source == "real":
        return ":green[**REAL DATA**]"
    return ":gray[unknown source]"


def synthetic_banner() -> None:
    """Persistent SYNTHETIC DEMO banner (never optional for demo output)."""
    st.warning(
        f"**{config.SYNTHETIC_BANNER}** - this output comes from simulated "
        "data. It is a demonstration of the workflow, not a botanical "
        "finding, and must not be used as agricultural advice.")


def unavailable_notice(message: str, counts: Optional[dict] = None) -> None:
    """Render the honest-refusal state (Level 4) exactly as the CLI does."""
    st.info(message)
    if counts:
        variety = (f" / {counts['variety']}" if counts.get("variety") else "")
        st.caption(
            f"Usable **real** readiness events for "
            f"`{counts['species']}{variety}`: **{counts['readiness_events']}**"
            f" (from {counts['independent_plants']} independent plants). "
            f"Minimum required: {config.MIN_READINESS_EVENTS} events from "
            f"{config.MIN_INDEPENDENT_PLANTS} plants.")
    st.caption("Status: collecting data - **not** making a prediction.")


def prediction_result(result: dict) -> None:
    """Render a prediction result dict (available or refused)."""
    if result.get("synthetic"):
        synthetic_banner()

    if not result.get("available"):
        unavailable_notice(result.get("message", config.UNAVAILABLE_MESSAGE),
                           result.get("readiness_event_counts"))
        return

    col1, col2 = st.columns(2)
    pred_date = result.get("predicted_readiness_date")
    if pred_date:
        col1.metric("Estimated readiness date", pred_date)
        col2.metric("Days remaining", f"{result['days_remaining']:.1f} days")
    else:
        col1.metric("Days until transplant-ready",
                    f"{result['days_remaining']:.1f} days")
        if result.get("mae_days") is not None:
            col2.metric("Validation error (MAE)",
                        f"± {result['mae_days']:.1f} days")
    st.write(result.get("message", ""))
    if result.get("past_readiness"):
        st.warning(result.get("status_note") or "Readiness passed: seedling appears ready for transplanting already.")

    details = {
        "Fallback level": f"Level {result.get('level')}",
        "Prediction source": ("SYNTHETIC DEMO" if result.get("synthetic")
                              else result.get("data_source")),
        "Validated": "yes" if result.get("validated") else "no",
        "Trained on": (f"{result.get('n_independent_plants')} plants / "
                       f"{result.get('n_labelled_readiness_events')} "
                       "readiness events"),
        "Model": result.get("model_name"),
        "Observation used": result.get("observation_date"),
        "Estimated readiness date": result.get("predicted_readiness_date") or "(not computed)",
    }
    if result.get("baseline_mae_days") is not None:
        details["Mean-baseline MAE"] = \
            f"{result['baseline_mae_days']:.1f} days"
    if result.get("clamped"):
        details["Clamped"] = (f"raw output was "
                              f"{result['days_remaining_raw']} days")
    st.table(details)

    if result.get("limitations"):
        st.caption(f"Limitations: {result['limitations']}")
    if result.get("cross_source_warning"):
        st.error(result["cross_source_warning"])
    if result.get("synthetic_note"):
        st.warning(result["synthetic_note"])


def metric_row(metrics: list[tuple[str, object]]) -> None:
    """A row of metric cards."""
    cols = st.columns(len(metrics))
    for col, (label, value) in zip(cols, metrics):
        col.metric(label, value)


def status_icon(status: str) -> str:
    icons = {"pending": ":clock1:", "due": ":bell:",
             "overdue": ":red[:alarm_clock:]", "completed": ":white_check_mark:"}
    return icons.get(status, "")


def feedback_status(row: dict, as_of: Optional[date] = None) -> str:
    """Mirror of services.feedback_state() for display purposes."""
    from plantreadiness.services import feedback_state
    return feedback_state(row, as_of)["status"]


def error_box(exc: Exception) -> None:
    """Render a domain exception as a friendly, non-technical message."""
    from plantreadiness.db import (DuplicateRecordError, ValidationError)
    from plantreadiness.repository import ConnectionFailedError
    if isinstance(exc, DuplicateRecordError):
        st.warning(str(exc))
    elif isinstance(exc, ValidationError):
        st.error(str(exc))
    elif isinstance(exc, ConnectionFailedError):
        st.error(f"**Could not reach the database.** {exc}")
    else:
        st.error(f"**Unexpected error:** {type(exc).__name__}: {exc}")
