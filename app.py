"""
app.py — Streamlit UI for the plant transplant-readiness predictor.

One action per source:

    pull captures → measure (OpenCV) → save daily observation → train/validate
    → predict → persist an append-only prediction → show method + limitations

The measurement/prediction cycle lives in ``predictor.py``; this file only
collects settings, triggers the cycle, and renders the result honestly.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import cv2
import streamlit as st

import cloudinary_refresh as cr
import db
import ml
import predictor

st.set_page_config(
    page_title="Plant Readiness Predictor",
    layout="wide",
)

METHOD_LABELS = {
    "validated_ml_model": "Validated ML model",
    "provisional_historical_baseline": "Provisional historical baseline",
    "provisional_height_matched_baseline": "Provisional height-matched baseline",
    "insufficient_evidence": "Insufficient evidence",
}


def _fmt(value, digits: int = 2, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}{unit}"


def _leaf_method_note(method: str | None) -> str:
    if not method:
        return ""
    if method == "contour_concavity_estimate":
        return (
            "Leaf count is estimated from the outline's concavities; compound "
            "leaves (e.g. marigold) can still be under-counted."
        )
    if method == "no_plant_found":
        return "No foliage was segmented, so no leaf count exists."
    return f"Leaf count method: {method.replace('_', ' ')}."


def _render_images(cycle: predictor.CycleResult, idx_key: str) -> None:
    images = cycle.images or []
    if not images:
        return

    idx = st.session_state.get(idx_key, 0)
    idx = max(0, min(idx, len(images) - 1))
    st.session_state[idx_key] = idx
    image = images[idx]

    nav_prev, nav_mid, nav_next = st.columns([1, 2, 1])
    with nav_prev:
        if st.button("Previous", disabled=idx == 0, key=f"{idx_key}_prev", width="stretch"):
            st.session_state[idx_key] = idx - 1
            st.rerun()
    with nav_mid:
        st.caption(f"Capture {idx + 1} of {len(images)}")
    with nav_next:
        if st.button("Next", disabled=idx >= len(images) - 1, key=f"{idx_key}_next", width="stretch"):
            st.session_state[idx_key] = idx + 1
            st.rerun()

    annotated = None
    if idx < len(cycle.annotated_images):
        annotated = cycle.annotated_images[idx]
    elif image.get("annotated_path"):
        path = Path(image["annotated_path"])
        if path.exists():
            annotated = cv2.imread(str(path))

    if annotated is not None:
        st.image(
            cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB),
            caption=f"{image.get('public_id', 'capture')} · {image.get('date', '—')}",
            width="stretch",
        )
    elif image.get("url"):
        st.image(
            image["url"],
            caption=f"{image.get('public_id', 'capture')} · {image.get('date', '—')}",
            width="stretch",
        )

    m1, m2, m3 = st.columns(3)
    m1.metric("Height", _fmt(image.get("height_cm"), unit=" cm"))
    m2.metric("Stem width", _fmt(image.get("stem_width_cm"), 3, " cm"))
    m3.metric("Leaves (est.)", image.get("leaf_count") if image.get("leaf_count") is not None else "—")
    st.caption(
        f"Date {image.get('date', '—')} ({image.get('date_source', '—')}) · "
        f"scale {'calibrated' if image.get('calibrated') else 'fallback (unverified)'} · "
        f"{image.get('pixels_per_cm', '—')} px/cm · "
        f"{image.get('calibration_source', '—')}"
    )


def _render_prediction(cycle: predictor.CycleResult) -> None:
    pred = cycle.prediction
    if not pred:
        return

    method = pred.get("method", "insufficient_evidence")
    label = METHOD_LABELS.get(method, method.replace("_", " ").title())

    st.subheader("Readiness prediction")
    if pred.get("predicted_days_remaining") is not None:
        col1, col2, col3 = st.columns(3)
        col1.metric("Days until ready", f"{pred['predicted_days_remaining']:.1f} d")
        col2.metric("Ready around", pred.get("predicted_readiness_date") or "—")
        col3.metric(
            "Reference plants",
            pred.get("contributing_plants", 0),
        )

        if method == "validated_ml_model":
            st.success(f"Method: {label} · confidence: {pred.get('confidence')}")
        elif method.startswith("provisional"):
            st.info(f"Method: {label} · confidence: {pred.get('confidence')}")
        else:
            st.error(f"Method: {label} · confidence: {pred.get('confidence')}")
        st.write(pred.get("summary_message", ""))

        sufficiency = "Data sufficient for this method" if pred.get("data_sufficient") else "Data insufficient"
        st.caption(
            f"{sufficiency} · age source: {pred.get('age_source', 'unknown')} · "
            f"prediction #{cycle.prediction_id} saved (append-only)"
            if cycle.prediction_id
            else f"{sufficiency} · age source: {pred.get('age_source', 'unknown')}"
        )

        with st.expander("How this was calculated"):
            details = pred.get("details", {}) or {}
            cohort = details.get("cohort_stats") or {}
            st.markdown(
                f"- Method: **{label}** (`{method}`)\n"
                f"- Confidence: **{pred.get('confidence')}**\n"
                f"- Reference cohort: **{cohort.get('n_plants', 0)} plant(s)**, "
                f"**{cohort.get('n_observations', 0)} observation(s)**, "
                f"**{cohort.get('n_readiness_events', 0)} readiness event(s)**\n"
                f"- Training: "
                + (
                    f"**{cycle.training.get('best_model_name')}** validated, "
                    f"MAE **{cycle.training.get('best_mae')} d** vs baseline "
                    f"**{cycle.training.get('baseline_mae')} d**"
                    if cycle.training.get("validated")
                    else f"not validated ({cycle.training.get('reason', 'criteria not met')})"
                )
            )
            if details:
                st.json(details, expanded=False)
    else:
        st.error(f"Method: {label}")
        st.write(pred.get("summary_message", "No prediction was produced."))

    limitations = pred.get("limitations") or []
    if limitations:
        st.markdown("**Limitations**")
        for limitation in limitations:
            st.markdown(f"- {limitation}")


def _render_cycle(cycle: predictor.CycleResult, idx_key: str) -> None:
    for warning in cycle.warnings:
        st.warning(warning)

    with st.expander("Run log", expanded=False):
        for message in cycle.messages:
            st.markdown(f"- {message}")
        for error in (cycle.report or {}).get("errors", []):
            st.markdown(f"- error: {error}")

    observation = cycle.observation
    if observation:
        st.subheader("Latest saved observation")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Height", _fmt(observation.get("height_cm"), unit=" cm"))
        c2.metric("Stem width", _fmt(observation.get("stem_width_cm"), 3, " cm"))
        c3.metric(
            "Leaves (est.)",
            observation.get("visible_true_leaf_count")
            if observation.get("visible_true_leaf_count") is not None
            else "—",
        )
        c4.metric(
            "Age",
            f"{observation.get('age_days')} d"
            if observation.get("age_days") is not None
            else "unknown",
        )
        growth = observation.get("growth_rate_cm_per_day")
        st.caption(
            f"Date {observation.get('observation_date')} · "
            f"source {observation.get('source', '—')} · "
            f"calibration {observation.get('calibration_source', '—')} · "
            f"growth rate {_fmt(growth, 3, ' cm/day')} · "
            f"growth rate is NULL until a second dated observation exists"
        )
        method_note = _leaf_method_note(
            (cycle.images[0].get("leaf_count_method") if cycle.images else None)
        )
        if method_note:
            st.caption(method_note)

    _render_images(cycle, idx_key)
    _render_prediction(cycle)

    if cycle.stage == "complete":
        history = db.get_predictions(db_path=db.DEFAULT_DB_PATH, limit=5)
        if history:
            st.caption(
                "Recent predictions: "
                + ", ".join(
                    f"#{p['id']} {p['prediction_date']} ({p['method']})" for p in history
                )
            )


def _render_feedback(species: str) -> None:
    with st.expander("Prediction history and feedback"):
        history = db.get_predictions(db_path=db.DEFAULT_DB_PATH, species=species, limit=20)
        if not history:
            st.caption("No saved predictions for this species yet.")
            return

        st.dataframe(
            [
                {
                    "id": p["id"],
                    "as of": p["prediction_date"],
                    "days remaining": p["predicted_days_remaining"],
                    "ready date": p["predicted_readiness_date"],
                    "method": p["method"],
                    "confidence": p["confidence"],
                    "plant": p["plant_id"] or "—",
                }
                for p in history
            ],
            width="stretch",
            hide_index=True,
        )

        options = {
            f"#{p['id']} · {p['prediction_date']} · {p['method']}": p["id"] for p in history
        }
        choice = st.selectbox("Prediction receiving feedback", list(options), key="feedback_pred")
        actual = st.date_input("Actual transplant date", value=None, key="feedback_actual")
        outcome = st.selectbox(
            "Outcome",
            ["", "healthy", "needs_more_time", "failed"],
            key="feedback_outcome",
        )
        if st.button("Save feedback", key="feedback_save"):
            db.save_feedback(
                db_path=db.DEFAULT_DB_PATH,
                prediction_id=options[choice],
                stage="transplant_followup",
                assessment="transplanted" if actual else "pending",
                actual_transplant_date=actual,
                condition=outcome or None,
            )
            st.success(
                "Feedback saved. The original prediction is unchanged (append-only)."
            )


# --- Sidebar -----------------------------------------------------------------
species_list = db.get_species_list()
if not species_list:
    st.warning("No species found. Seed the database first: `python cli.py seed`")
    st.stop()

with st.sidebar:
    st.header("Settings")
    species = st.selectbox("Species / reference cohort", species_list)
    st.caption(
        "The species names both the live plant and the historical cohort it is "
        "compared against."
    )

    sources = cr.configured_sources()
    default_ref = sources[0][1] if sources else "camera"
    plant_ref = st.text_input("Camera plant reference", value=default_ref)

    configured_rod = cr.configured_rod_length()
    rod_cm = st.number_input(
        "Reference rod length (cm)",
        min_value=1.0,
        max_value=100.0,
        value=float(configured_rod),
        step=0.5,
    )
    verified_rod = cr._verified_rod_length()
    if verified_rod is not None and abs(verified_rod - rod_cm) < 1e-6:
        st.caption("Rod length is recorded as verified.")
    else:
        st.caption(
            "Rod length is unverified (set `CAMERA_VERIFIED_ROD_LENGTH_CM` after "
            "physically checking it). Camera observations stay quarantined from "
            "the cohort."
        )

    captures = st.slider("Captures per pull", min_value=1, max_value=8, value=4)
    reprocess = st.checkbox("Re-measure already processed captures", value=False)

    knows_planting = st.checkbox("I know the planting date")
    planted_date = None
    if knows_planting:
        planted_date = st.date_input("Planted on", value=date.today(), key="planted")
    st.caption(
        "Age is used only when a planting date is known; it is never guessed. "
        "Without it, prediction falls back to height matching."
    )

# --- Main --------------------------------------------------------------------
st.title("Plant Readiness Predictor")
st.caption("Pull images → measure → save to database → predict, with method and limits stated.")

camera_tab, upload_tab = st.tabs(["Camera (Cloudinary)", "Upload images"])

with camera_tab:
    if not cr.cloudinary_configured():
        st.warning(
            "Cloudinary is not configured. Set `CLOUDINARY_CLOUD_NAME`, "
            "`CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` and `CAMERA_SOURCES` "
            "in `.env`, or use the upload tab."
        )
    else:
        st.caption(
            "Configured sources: "
            + ", ".join(f"`{prefix}` → {ref}" for prefix, ref in sources)
        )
        if st.button("Pull images and predict", type="primary", key="camera_run"):
            with st.status("Running measurement cycle…", expanded=True) as status:
                st.write("Fetching captures from Cloudinary…")
                try:
                    cycle = predictor.run_camera_cycle(
                        db_path=db.DEFAULT_DB_PATH,
                        species=species,
                        captures=captures,
                        rod_length_cm=rod_cm,
                        planted_date=planted_date.isoformat() if planted_date else None,
                        external_ref=plant_ref or None,
                        force=True,
                        reprocess=reprocess,
                        auto_register=True,
                    )
                except Exception as exc:  # noqa: BLE001 - surface, don't crash
                    status.update(label="Cycle failed", state="error")
                    st.exception(exc)
                    cycle = None
                if cycle is not None:
                    st.write("Measuring images and saving observations…")
                    st.write("Training/validating the model and predicting…")
                    status.update(
                        label="Cycle complete" if cycle.ok else "Cycle stopped",
                        state="complete" if cycle.ok else "error",
                    )
                    st.session_state["camera_cycle"] = cycle
                    st.session_state["camera_img_idx"] = 0

    if st.session_state.get("camera_cycle") is not None:
        _render_cycle(st.session_state["camera_cycle"], "camera_img_idx")

with upload_tab:
    uploaded = st.file_uploader(
        "Plant photos (same plant and day)",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key="upload_files",
    )
    upload_col1, upload_col2 = st.columns(2)
    with upload_col1:
        upload_ref = st.text_input("Plant reference", value=plant_ref or "camera", key="upload_ref")
    with upload_col2:
        upload_date = st.date_input("Observation date", value=date.today(), key="upload_date")

    if st.button(
        "Measure and predict",
        type="primary",
        key="upload_run",
        disabled=not uploaded,
    ):
        with st.status("Running measurement cycle…", expanded=True) as status:
            st.write(f"Measuring {len(uploaded)} image(s)…")
            try:
                cycle = predictor.run_image_cycle(
                    db_path=db.DEFAULT_DB_PATH,
                    species=species,
                    images=[f.getvalue() for f in uploaded],
                    rod_length_cm=rod_cm,
                    plant_ref=upload_ref,
                    planted_date=planted_date.isoformat() if planted_date else None,
                    observation_date=upload_date,
                    auto_register=True,
                )
            except Exception as exc:  # noqa: BLE001
                status.update(label="Cycle failed", state="error")
                st.exception(exc)
                cycle = None
            if cycle is not None:
                st.write("Saving observation…")
                st.write("Training/validating the model and predicting…")
                status.update(
                    label="Cycle complete" if cycle.ok else "Cycle stopped",
                    state="complete" if cycle.ok else "error",
                )
                st.session_state["upload_cycle"] = cycle
                st.session_state["upload_img_idx"] = 0

    if st.session_state.get("upload_cycle") is not None:
        _render_cycle(st.session_state["upload_cycle"], "upload_img_idx")

st.divider()
_render_feedback(species)
