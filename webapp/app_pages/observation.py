"""Record observation & readiness events page.

* Daily measurement: Auto-calculates plant age (from ``date_started``) and
  growth rate (from previous measurement) before submission.
* Readiness event: Records verified ground-truth transplant readiness dates
  associated with documented assessment criteria and observer identity.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from plantreadiness import config
from plantreadiness import services as svc
from webapp import components as ui


def render(repo, models_dir) -> None:
    st.title(":material/straighten: Record observation & readiness")

    try:
        plants = repo.list_plants()
    except Exception as exc:
        ui.error_box(exc)
        return
    if not plants:
        st.info("No plants registered yet - register one first.")
        return

    labels = {
        f"#{p['plant_id']} {p['species']}"
        f"{' / ' + p['variety'] if p['variety'] else ''}"
        f" [{p['data_source']}]": p["plant_id"]
        for p in plants
    }
    default_idx = 0
    if st.session_state.get("last_plant_id"):
        for i, pid in enumerate(labels.values()):
            if pid == st.session_state["last_plant_id"]:
                default_idx = i
    chosen = st.selectbox("Plant", list(labels.keys()), index=default_idx)
    plant_id = labels[chosen]
    plant = next(p for p in plants if p["plant_id"] == plant_id)

    tab_obs, tab_ready = st.tabs(["Daily measurement", "Transplant-readiness event"])

    with tab_obs:
        _render_observation_tab(repo, plant_id, plant)

    with tab_ready:
        _render_readiness_tab(repo, plant_id, plant)


def _render_observation_tab(repo, plant_id: int, plant: dict) -> None:
    obs_date = st.date_input("Observation date", value=date.today(), key="obs_date_input")

    prev = None
    try:
        prev = repo.latest_observation(plant_id)
    except Exception:
        prev = None

    col1, col2 = st.columns(2)
    height = col1.number_input(
        "Height (cm)",
        min_value=0.0,
        value=0.0,
        step=0.1,
        help="Leave at 0 and clear to store no height.",
        key="obs_height_input",
    )
    canopy = col1.number_input(
        "Canopy width (cm)",
        min_value=0.0,
        value=0.0,
        step=0.1,
        help="Optional canopy width.",
        key="obs_canopy_input",
    )
    leaves = col2.number_input(
        "Visible true leaves",
        min_value=0,
        value=0,
        step=1,
        help="Optional visible leaf count.",
        key="obs_leaves_input",
    )
    manual_age = col2.number_input(
        "Age (days) - manual override",
        min_value=0,
        value=0,
        step=1,
        help="0 = auto-calculate from the plant's start date.",
        key="obs_age_input",
    )

    height_val = float(height) if height > 0 else None
    derived_age = svc.auto_age_days(plant.get("date_started"), obs_date.isoformat())
    rate_info = svc.auto_growth_rate(
        prev.get("height_cm") if prev else None,
        prev.get("observation_date") if prev else None,
        height_val,
        obs_date.isoformat(),
    )

    with st.expander("What will be stored (auto-calculated)", expanded=True):
        age_stored = int(manual_age) if manual_age > 0 else derived_age
        if age_stored is None:
            if plant.get("date_started") and obs_date.isoformat() < plant["date_started"]:
                st.caption(
                    "Age: **not measurable** - the observation date is "
                    "before the plant's start date. Left empty (never invented)."
                )
            else:
                st.caption(
                    "Age: **not measurable** - the plant has no start date "
                    "and no manual age was given. Left empty."
                )
        else:
            src = "manual value" if manual_age > 0 else "derived from `date_started`"
            st.caption(f"Age: **{age_stored} days** ({src})")
        if rate_info["value"] is None:
            st.caption(
                f"Growth rate: **not measurable** - {rate_info['reason']}."
                " Left empty (never invented)."
            )
        else:
            st.caption(
                f"Growth rate: **{rate_info['value']} cm/day** "
                f"({rate_info['reason']})"
            )
        if prev and height_val is not None:
            st.caption(
                f"Previous measurement: {prev['observation_date']} at "
                f"{prev['height_cm']} cm"
            )
        elif height_val is None:
            st.caption(
                "Height is empty - a row with no height will still be "
                "recorded (age-only observation)."
            )

    if st.button("Record observation", type="primary", key="btn_record_obs"):
        try:
            result = svc.record_observation(
                repo,
                plant_id=plant_id,
                observation_date=obs_date.isoformat(),
                height_cm=height_val,
                age_days=int(manual_age) if manual_age > 0 else None,
                canopy_width_cm=float(canopy) if canopy > 0 else None,
                visible_true_leaf_count=int(leaves) if leaves > 0 else None,
            )
        except Exception as exc:
            ui.error_box(exc)
        else:
            st.success(
                f"Observation **#{result['observation_id']}** recorded "
                f"for plant #{plant_id}."
            )
            for field, how in result["derived"].items():
                st.caption(f"auto: `{field}` {how}")
            st.session_state["last_plant_id"] = plant_id

    _growth_chart(repo, plant_id)


def _render_readiness_tab(repo, plant_id: int, plant: dict) -> None:
    st.subheader("Record Verified Transplant Readiness Event")
    st.caption(
        "Readiness events supply **ground truth training labels** for countdown models. "
        "Observations recorded on or before this date are paired with: "
        "`days_remaining = readiness_date - observation_date`."
    )

    with st.form("form_record_readiness", clear_on_submit=True):
        r_date = st.date_input("Readiness observed date", value=date.today())
        col_v, col_s = st.columns(2)
        version = col_v.text_input(
            "Definition version *",
            value="v1-nursery-visual",
            help="Unique version tag identifying the botanical/grower criteria applied.",
        )
        source = col_s.text_input(
            "Assessment source / observer *",
            value="grower-inspection",
            help="Identity of person or sensor verifying readiness.",
        )
        notes = st.text_area(
            "Observation notes",
            placeholder="e.g., 4 true leaves present, root plug fully knit, stem diameter >= 3mm.",
            help="Document visual criteria observed.",
        )
        st.caption(
            "Rule: Each plant may have one readiness event per definition version. "
            "To correct an event, use a revised version tag (e.g. `v1.1-corrected`)."
        )
        ok = st.form_submit_button("Record readiness event", type="primary")

    if ok:
        if not version.strip() or not source.strip():
            st.error("Both definition version and assessment source are required.")
            return
        try:
            res = svc.record_readiness_event(
                repo,
                plant_id=plant_id,
                readiness_observed_date=r_date.isoformat(),
                readiness_definition_version=version.strip(),
                assessment_source=source.strip(),
                notes=notes.strip() if notes else None,
            )
        except Exception as exc:
            ui.error_box(exc)
            return
        st.success(
            f"Readiness event **#{res['readiness_event_id']}** recorded on "
            f"**{res['readiness_observed_date']}** for plant #{plant_id}."
        )

    # Display history of readiness events for this plant
    try:
        events = repo.list_readiness_events(plant_id=plant_id)
    except Exception:
        events = []

    st.subheader(f"Recorded Readiness Events (Plant #{plant_id})")
    if not events:
        st.info("No readiness events recorded for this plant yet.")
    else:
        table_rows = [
            {
                "Event #": e["readiness_event_id"],
                "Date": e["readiness_observed_date"],
                "Age (days)": e.get("readiness_observed_age_days"),
                "Definition version": e["readiness_definition_version"],
                "Observer": e["assessment_source"],
                "Notes": e.get("notes") or "",
                "Recorded at": e.get("recorded_at"),
            }
            for e in events
        ]
        st.dataframe(table_rows, hide_index=True, width="stretch")


def _growth_chart(repo, plant_id: int) -> None:
    st.subheader("Growth history")
    try:
        obs = repo.get_observations(plant_id)
    except Exception as exc:
        ui.error_box(exc)
        return
    if not obs:
        st.caption("No observations yet for this plant.")
        return
    rows = [
        {
            "Date": o["observation_date"],
            "Age (days)": o["age_days"],
            "Height (cm)": o["height_cm"],
            "Growth rate (cm/day)": o["growth_rate_cm_per_day"],
            "Source": o["data_source"],
        }
        for o in obs
    ]
    df = pd.DataFrame(rows)
    heights = df.dropna(subset=["Height (cm)"])
    if len(heights) >= 2:
        st.line_chart(heights.set_index("Date")[["Height (cm)"]])
    elif len(heights) == 1:
        st.caption(
            "One height measurement so far - a trend line needs at least two."
        )
    st.dataframe(rows, hide_index=True, width="stretch")
    if any(r["Source"] == "synthetic" for r in rows):
        st.markdown(
            f":orange[**{config.SYNTHETIC_BANNER}** rows present in this history.]"
        )
