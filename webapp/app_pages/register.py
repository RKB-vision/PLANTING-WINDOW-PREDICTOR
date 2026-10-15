"""Register plant page.

Real data is the default. Registering a *simulated* plant requires an
explicit opt-in and is labelled SYNTHETIC DEMO everywhere it appears.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from plantreadiness import config
from plantreadiness import services as svc
from webapp import components as ui


def render(repo, models_dir) -> None:
    st.title(":material/eco: Register plant")

    with st.form("register_plant", clear_on_submit=True):
        col1, col2 = st.columns(2)
        species = col1.text_input("Species *", value=config.DEFAULT_SPECIES,
                                  help="Plant species, e.g. 'Super Orange'")
        variety = col1.text_input("Variety",
                                  help="Leave blank if unknown")
        date_started = col2.date_input("Date started (sowing/germination)",
                                       value=date.today(),
                                       help="Used to auto-calculate plant age "
                                            "for every later observation.")
        external_ref = col2.text_input("External reference",
                                       help="Your own ID (e.g. CSV Plant_ID)")
        growing_conditions = st.text_area(
            "Growing conditions",
            help="Free text: substrate, light, pot size... (never used as a "
                 "model feature - kept as a note only)")
        cohort = st.text_input("Cohort",
                               help="e.g. 'initial', 'holdout', 'retrain'. "
                                    "Holdout cohorts never train models.")

        st.divider()
        synthetic = st.checkbox(
            "Register as SYNTHETIC DEMO data",
            help="Only tick this for simulated plants. Real plants must "
                 "never be registered as synthetic or vice versa.")
        source = "synthetic" if synthetic else "real"
        if synthetic:
            st.warning(f"**{config.SYNTHETIC_BANNER}** - this plant will be "
                       "labelled simulated data and kept separate from real "
                       "evidence.")

        submitted = st.form_submit_button("Register plant",
                                          type="primary",
                                          width="stretch")

    if not submitted:
        _list_existing(repo)
        return

    try:
        pid = svc.register_plant(
            repo, species=species, variety=variety or None,
            date_started=date_started.isoformat() if date_started else None,
            data_source=source,
            growing_conditions=growing_conditions or None,
            external_ref=external_ref or None,
            cohort=cohort or None)
    except Exception as exc:
        ui.error_box(exc)
        _list_existing(repo)
        return

    st.success(f"Registered plant **#{pid}**: {species}"
               f"{' / ' + variety if variety else ''} "
               f"(`data_source={source}`)")
    st.session_state["last_plant_id"] = pid
    st.caption("Next: record an observation on the **Record observation** "
               "page.")
    _list_existing(repo)


def _list_existing(repo) -> None:
    st.subheader("Registered plants")
    search = st.text_input("Search", placeholder="species / variety / ref",
                           key="plant_search")
    try:
        plants = repo.list_plants(search=search or None)
    except Exception as exc:
        ui.error_box(exc)
        return
    if not plants:
        st.caption("No plants registered yet.")
        return
    rows = [{"#": p["plant_id"], "Species": p["species"],
             "Variety": p["variety"], "Started": p["date_started"],
             "Ref": p["external_ref"], "Cohort": p["cohort"],
             "Source": p["data_source"]} for p in plants]
    st.dataframe(rows, hide_index=True, width="stretch")
    st.markdown(" ".join(
        f"`[{p['data_source']}]`" if p["data_source"] == "synthetic" else ""
        for p in plants if p["data_source"] == "synthetic"))
