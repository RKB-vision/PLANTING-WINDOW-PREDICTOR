"""Predictions page.

Applies the existing species-aware fallback order (Level 1 -> 4) through the
shared service. When no suitable model exists the exact honest-refusal
message is shown with the evidence counts - there is no silent fallback to
synthetic. Synthetic models require an explicit opt-in and their output is
always bannered.
"""

from __future__ import annotations

import streamlit as st

from plantreadiness import config
from plantreadiness import services as svc
from webapp import components as ui

FALLBACK_HELP = """
**Fallback order** (unchanged from the CLI):

1. **Level 1** - validated species/variety model trained on real labelled data
2. **Level 2** - broader real model for the species (uncertainty applies)
3. **Level 3** - `SYNTHETIC DEMO` model - **only** with explicit opt-in, always labelled
4. **Level 4** - insufficient evidence → *"Readiness prediction unavailable: ..."*
"""


def render(repo, models_dir) -> None:
    st.title(":material/clock: Days until transplant-ready")
    st.caption(FALLBACK_HELP)

    try:
        plants = repo.list_plants()
    except Exception as exc:
        ui.error_box(exc)
        return
    if not plants:
        st.info("No plants registered yet.")
        return

    labels = {f"#{p['plant_id']} {p['species']}"
              f"{' / ' + p['variety'] if p['variety'] else ''}"
              f" [{p['data_source']}]": p["plant_id"] for p in plants}
    chosen = st.selectbox("Plant", list(labels.keys()))
    plant_id = labels[chosen]
    plant = next(p for p in plants if p["plant_id"] == plant_id)

    allow_synthetic = st.checkbox(
        "Allow SYNTHETIC DEMO model (explicit opt-in)",
        value=False,
        disabled=(plant["data_source"] == "real"),
        help="Real plants never use synthetic models unless you tick this "
             "yourself - and even then the output is bannered and must not "
             "be treated as botanical advice.")
    if plant["data_source"] == "real" and not allow_synthetic:
        st.caption("This is a **real** plant: synthetic models stay off by "
                   "default.")

    # ---- which model would be used (before predicting) -------------------- #
    try:
        plan = svc.species_model_plan(
            repo, plant["species"], plant["variety"], models_dir,
            allow_synthetic=allow_synthetic)
    except Exception as exc:
        ui.error_box(exc)
        plan = None

    if plan is not None:
        with st.expander("Model selection for this species", expanded=True):
            st.write(f"**Would use:** Level {plan['level']} - {plan['message']}")
            counts = plan["real_readiness_event_counts"]
            st.write(
                f"Real readiness evidence for `{plant['species']}`"
                f"{' / ' + plant['variety'] if plant['variety'] else ''}: "
                f"**{counts['readiness_events']} events** from "
                f"**{counts['independent_plants']} plants** "
                f"(minimum: {plan['min_requirements']['readiness_events']} "
                f"events / "
                f"{plan['min_requirements']['independent_plants']} plants "
                f"→ {'met' if plan['meets_min_requirements'] else 'NOT met'})")
            if plan["model_name"]:
                st.caption(f"Artefact: `{plan['model_name']}`")

    if st.button("Predict", type="primary", width="stretch"):
        try:
            result = svc.predict_for_plant(
                repo, plant_id, models_dir,
                allow_synthetic=allow_synthetic)
        except Exception as exc:
            ui.error_box(exc)
        else:
            st.session_state["last_prediction"] = result

    result = st.session_state.get("last_prediction")
    if result and result.get("plant_id") == plant_id:
        st.divider()
        ui.prediction_result(result)
    elif result is None:
        st.caption("Press **Predict** to run the countdown estimate for this "
                   "plant's latest observation.")
