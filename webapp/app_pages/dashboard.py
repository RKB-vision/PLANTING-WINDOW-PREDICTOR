"""Dashboard page - real metrics only.

Never pads numbers with synthetic rows: plant/observation/feedback counts
and the readiness breakdown are grouped by ``data_source`` so a viewer can
see exactly how much evidence is real. Model status reflects what is
actually on disk (artefacts may be absent in a fresh deployment).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from plantreadiness import config
from plantreadiness import services as svc
from webapp import components as ui


def render(repo, models_dir) -> None:
    st.title(":material/dashboard: Dashboard")

    try:
        summary = svc.dashboard_summary(repo, models_dir)
    except Exception as exc:
        ui.error_box(exc)
        return

    ui.metric_row([
        ("Plants", summary["n_plants"]),
        ("Observations", summary["n_observations"]),
        ("Feedback pending", summary["n_feedback_pending"]),
        ("Feedback overdue", summary["n_feedback_overdue"]),
    ])

    # ---- feedback queue -------------------------------------------------- #
    st.subheader("14-day feedback queue")
    pending = summary["pending_feedback"]
    if not pending:
        st.success("No feedback is waiting - every transplant follow-up is "
                   "complete.")
    else:
        rows = []
        for r in pending:
            state = svc.feedback_state(r)
            rows.append({
                "Feedback #": r["feedback_id"],
                "Plant": f"#{r['plant_id']} {r['species']}"
                         f"{'/' + r['variety'] if r.get('variety') else ''}",
                "Transplant": r.get("transplant_date"),
                "Due": r["feedback_due_date"],
                "Status": state["status"],
                "Days overdue": state["days_overdue"] or "",
                "Source": r.get("data_source"),
            })
        st.dataframe(rows, hide_index=True, width="stretch")
        if summary["n_feedback_overdue"]:
            st.warning(f"{summary['n_feedback_overdue']} follow-up(s) are "
                       "**overdue** - see the Transplant & feedback page.")

    # ---- readiness evidence (real vs synthetic) -------------------------- #
    st.subheader("Readiness evidence by species")
    evidence = summary["readiness_by_species"]
    real_rows = [r for r in evidence if r["data_source"] == "real"]
    syn_rows = [r for r in evidence if r["data_source"] == "synthetic"]

    if real_rows:
        df = pd.DataFrame(real_rows).rename(columns={
            "species": "Species", "n_plants": "Plants",
            "n_plants_with_event": "Plants with readiness event",
            "n_events": "Real readiness events"})
        st.dataframe(df, hide_index=True, width="stretch")
        short = [r for r in real_rows
                 if r["n_events"] < config.MIN_READINESS_EVENTS]
        if short:
            names = ", ".join(f"`{r['species']}`" for r in short)
            st.caption(
                f"{names}: below the minimum of "
                f"{config.MIN_READINESS_EVENTS} verified real readiness "
                f"events from {config.MIN_INDEPENDENT_PLANTS} plants - "
                f"{config.UNAVAILABLE_MESSAGE}")
    else:
        st.info("No plants registered yet.")

    if syn_rows:
        with st.expander(
                f":orange[SYNTHETIC DEMO data present "
                f"({sum(r['n_plants'] for r in syn_rows)} simulated plants)]"):
            df = pd.DataFrame(syn_rows).rename(columns={
                "species": "Species", "n_plants": "Simulated plants",
                "n_events": "Synthetic readiness events"})
            st.dataframe(df, hide_index=True, width="stretch")
            st.caption("Synthetic rows are labelled and stored separately; "
                       "they never train a real-data model.")

    # ---- model status ---------------------------------------------------- #
    st.subheader("Model status")
    status = summary["model_status"]
    real_active = status["real"]["active"]
    syn_active = status["synthetic"]["active"]
    ui.metric_row([
        ("Real models (active)", real_active),
        ("Real models (validated)", status["real"]["validated"]),
        ("Synthetic demo models", syn_active),
    ])
    if not real_active:
        st.info("No active **real** model registered yet - predictions stay "
                f"in the honest-refusal state ({config.UNAVAILABLE_MESSAGE}) "
                "until enough verified readiness events are collected and "
                "trained.")
    else:
        rows = [{"Model": a["model_name"],
                 "Species": a["scope_species"],
                 "Variety": a["scope_variety"] or "any",
                 "Validated": "yes" if a["validated"] else "no",
                 "Plants": a["n_independent_plants"],
                 "Events": a["n_labelled_readiness_events"],
                 "MAE (days)": a["mae_days"]}
                for a in status["real"]["artifacts"]]
        st.dataframe(rows, hide_index=True, width="stretch")
    if syn_active:
        st.caption(f":orange[{syn_active} SYNTHETIC DEMO model(s) on disk - "
                   "used only with explicit opt-in and always labelled.]")
