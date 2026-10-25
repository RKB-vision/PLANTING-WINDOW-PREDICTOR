"""
app.py — Streamlit Interactive Plant Readiness Dashboard

Unified web application replacing Flask + Next.js with a single interactive Python interface.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pandas as pd
import streamlit as st

import db
import ml
import vision


st.set_page_config(
    page_title="Plant Transplant Readiness Predictor",
    page_icon="🌱",
    layout="wide",
)


@st.cache_data(ttl=60)
def load_species():
    return db.get_species_list()


@st.cache_data(ttl=60)
def load_all_observations(species: Optional[str] = None):
    return db.get_observations(species=species)


# Header
st.title("Plant transplant readiness predictor")
st.caption(
    "Camera-derived computer vision measurements and historical growth cohorts "
    "to estimate when plant species are ready for transplantation."
)

species_list = load_species()
all_obs = load_all_observations()

# High-level overview metrics
with st.container(border=True):
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Monitored species", len(species_list))
    with col2:
        total_plants = len(db.get_plants())
        st.metric("Total plants", total_plants)
    with col3:
        st.metric("Historical observations", len(all_obs))
    with col4:
        eval_res = ml.evaluate_predictions()
        st.metric("Evaluated outcomes", eval_res.get("evaluations_count", 0))

# Main Tabs
tab_pred, tab_upload, tab_vision, tab_trends, tab_feedback, tab_train = st.tabs([
    ":material/psychology: Predict readiness",
    ":material/upload_file: Upload CSV dataset",
    ":material/photo_camera: Vision & measurement",
    ":material/show_chart: Growth trajectories",
    ":material/fact_check: Feedback & outcomes",
    ":material/model_training: Model training & evaluation",
])


# ---------------------------------------------------------
# TAB 1: PREDICT READINESS
# ---------------------------------------------------------
with tab_pred:
    st.subheader("Transplant countdown estimation")
    st.write(
        "Combines current plant measurements with validated ML models "
        "or historical median trajectories using an honest fallback ladder."
    )

    if not species_list:
        st.warning("No plant data in database yet. Head to the 'Data & ML models' tab to seed data.")
    else:
        left, right = st.columns([1, 1])

        with left:
            with st.container(border=True):
                st.markdown("##### Input observation")
                selected_species = st.selectbox("Species", species_list, key="pred_species")

                mode = st.radio(
                    "Measurement source",
                    ["Use database most recent", "Enter custom measurements"],
                    horizontal=True,
                )

                custom_height = None
                custom_age = None
                custom_growth = None

                if mode == "Enter custom measurements":
                    c_a, c_h = st.columns(2)
                    with c_a:
                        custom_age = st.number_input("Plant age (days)", min_value=1, max_value=120, value=12)
                    with c_h:
                        custom_height = st.number_input("Height (cm)", min_value=0.1, max_value=50.0, value=3.8, step=0.1)
                    custom_growth = st.number_input("Growth rate (cm/day)", min_value=0.0, max_value=5.0, value=0.45, step=0.05)

                predict_clicked = st.button("Predict readiness", type="primary", width="stretch")

        with right:
            if predict_clicked:
                with st.spinner("Calculating countdown..."):
                    # Check if a validated ML model is available for species
                    train_res = ml.train_countdown_model(species=selected_species)
                    pipe = train_res.get("pipeline") if train_res.get("validated") else None

                    pred = ml.predict_species_readiness(
                        species=selected_species,
                        current_height_cm=custom_height,
                        current_age_days=custom_age,
                        current_growth_rate=custom_growth,
                        trained_model=pipe,
                    )

                with st.container(border=True):
                    st.markdown("##### Prediction result")
                    if pred.predicted_days_remaining is not None:
                        m1, m2 = st.columns(2)
                        with m1:
                            st.metric("Estimated days remaining", f"{pred.predicted_days_remaining} days")
                        with m2:
                            st.metric("Estimated readiness date", pred.predicted_readiness_date)

                        if pred.method == "validated_ml_model":
                            st.success(
                                pred.summary_message,
                                title=f"Method: {pred.method.replace('_', ' ').title()} (Confidence: {pred.confidence.title()})",
                            )
                        else:
                            st.info(
                                pred.summary_message,
                                title=f"Method: {pred.method.replace('_', ' ').title()} (Confidence: {pred.confidence.title()})",
                            )

                        st.caption(f"Supporting cohort: {pred.contributing_plants} contributing plants.")
                    else:
                        st.error(pred.summary_message, title="Prediction unavailable")


# ---------------------------------------------------------
# TAB 2: UPLOAD CSV DATASET
# ---------------------------------------------------------
with tab_upload:
    st.subheader("Upload plant growth CSV dataset")
    st.write(
        "Upload a CSV containing daily plant growth observations. "
        "The system will compute temporal growth rates and register the cohort for predictions."
    )

    u_col1, u_col2 = st.columns([1, 1])

    with u_col1:
        with st.container(border=True):
            st.markdown("##### Select and upload file")
            uploaded_csv = st.file_uploader(
                "Choose CSV file",
                type=["csv"],
                help="Columns: Date, Plant_ID, Height_cm, Age, etc.",
                key="csv_file_uploader",
            )

            c_spec, c_var = st.columns(2)
            with c_spec:
                input_species = st.text_input("Species name", value="Sunflower (SuperOrange)")
            with c_var:
                input_variety = st.text_input("Variety name", value="SuperOrange")

            if uploaded_csv is not None:
                if st.button("Import & seed database", type="primary", width="stretch"):
                    with st.spinner("Processing and seeding observations..."):
                        uploaded_csv.seek(0)
                        count = db.seed_from_csv(
                            db.DEFAULT_DB_PATH,
                            uploaded_csv.read(),
                            species=input_species,
                            variety=input_variety,
                        )
                    st.success(f"Successfully imported {count} observations for '{input_species}'!")
                    st.cache_data.clear()
            else:
                st.divider()
                st.caption("Or load the default historical cohort from disk:")
                if st.button("Load default super_orange_growth_data.csv", width="stretch"):
                    with st.spinner("Importing default cohort..."):
                        count = db.seed_from_csv(db.DEFAULT_DB_PATH, "data/super_orange_growth_data.csv")
                    st.success(f"Seeded {count} observations into the database.")
                    st.cache_data.clear()

    with u_col2:
        with st.container(border=True):
            st.markdown("##### CSV preview & format reference")
            if uploaded_csv is not None:
                try:
                    uploaded_csv.seek(0)
                    df_preview = pd.read_csv(uploaded_csv)
                    st.caption(f"Found {len(df_preview)} rows and {len(df_preview.columns)} columns in uploaded file:")
                    st.dataframe(df_preview.head(5), width="stretch")
                except Exception as e:
                    st.error(f"Error parsing CSV: {e}")
            else:
                st.info("Upload a CSV file on the left to see an instant preview of its rows and columns.")
                st.markdown("""
                **Supported columns:**
                - `Plant_ID` / `plant_id` (e.g. `1`, `Plant02`)
                - `Date` / `date` (e.g. `02-09-2026` or `2026-09-02`)
                - `Height_cm` / `height_cm` (e.g. `1.5`)
                - `Age` / `age_days` (optional, e.g. `8 days` or `8`)
                - `Stem_Width_cm` (optional)
                - `Leaf_Count` (optional)
                - `readiness_observed_date` (optional ground truth label)
                """)


# ---------------------------------------------------------
# TAB 3: VISION & CAMERA MEASUREMENT
# ---------------------------------------------------------
with tab_vision:
    st.subheader("OpenCV camera measurement pipeline")
    st.write(
        "Detects vertical calibration rod to establish scale, segments foliage green contours, "
        "and extracts height, stem thickness, and leaf count."
    )

    v_col1, v_col2 = st.columns([1, 1])

    with v_col1:
        with st.container(border=True):
            st.markdown("##### Image source")
            sample_images = [f.name for f in Path(".").glob("*.jpg") if not f.name.startswith(".")]
            img_choice = st.radio("Select input", ["Sample repository image", "Upload photo"])

            image_bytes = None
            if img_choice == "Sample repository image" and sample_images:
                chosen_sample = st.selectbox("Sample image", sample_images)
                image_bytes = Path(chosen_sample).read_bytes()
            else:
                uploaded = st.file_uploader("Upload plant image", type=["jpg", "jpeg", "png"])
                if uploaded:
                    image_bytes = uploaded.read()

            rod_cm = st.slider("Reference rod length (cm)", min_value=5.0, max_value=25.0, value=12.0, step=0.5)
            measure_clicked = st.button("Process image", type="primary", width="stretch")

    with v_col2:
        if image_bytes and measure_clicked:
            with st.spinner("Processing computer vision pipeline..."):
                res = vision.measure_image(image_bytes, rod_length_cm=rod_cm)

            with st.container(border=True):
                st.markdown("##### OpenCV detection overlay")
                rgb_annotated = cv2.cvtColor(res.annotated_image, cv2.COLOR_BGR2RGB)
                st.image(rgb_annotated, caption="Annotated detection", alt="Plant measurement with bounding box and rod")

                m_col1, m_col2, m_col3 = st.columns(3)
                with m_col1:
                    st.metric("Height", f"{res.height_cm:.2f} cm")
                with m_col2:
                    st.metric("Stem width", f"{res.stem_width_cm or 0:.3f} cm")
                with m_col3:
                    st.metric("Leaf count", res.leaf_count)

                st.caption(f"Calibration scale: {res.pixels_per_cm:.2f} px/cm ({'Calibrated' if res.calibrated else 'Fallback'})")


# ---------------------------------------------------------
# TAB 3: GROWTH TRAJECTORIES & BASELINE
# ---------------------------------------------------------
with tab_trends:
    st.subheader("Historical growth trajectories")
    if species_list:
        spec_choice = st.selectbox("Select species", species_list, key="trend_species")
        obs_for_spec = db.get_observations(species=spec_choice)

        if obs_for_spec:
            df_spec = pd.DataFrame(obs_for_spec)
            if "age_days" in df_spec.columns and not df_spec["age_days"].dropna().empty:
                med_trajectory = df_spec.groupby("age_days").agg(
                    median_height=("height_cm", "median"),
                    median_growth_rate=("growth_rate_cm_per_day", "median"),
                    plants=("plant_id", "nunique"),
                ).reset_index()

                st.line_chart(
                    med_trajectory.set_index("age_days")[["median_height"]],
                    y_label="Height (cm)",
                    x_label="Age (days)",
                )

                with st.expander("View trajectory data table"):
                    st.dataframe(med_trajectory, width="stretch")
            else:
                st.dataframe(df_spec[["observation_date", "plant_id", "height_cm"]], width="stretch")
        else:
            st.info("No observations recorded for this species.")


# ---------------------------------------------------------
# TAB 4: FEEDBACK & OUTCOMES
# ---------------------------------------------------------
with tab_feedback:
    st.subheader("Record feedback & transplant outcomes")
    st.write(
        "Preserves the distinction between readiness assessments, actual transplantation, "
        "and 14-day survival monitoring. Missing feedback remains pending without rewriting history."
    )

    with st.container(border=True):
        st.markdown("##### Submit observation feedback")
        fb_stage = st.selectbox("Stage", ["readiness_assessment", "transplantation", "fourteen_day_followup"])

        f_col1, f_col2 = st.columns(2)
        with f_col1:
            fb_plant = st.text_input("Plant ID", value="Plant01")
            fb_date = st.date_input("Event date", value=date.today())
        with f_col2:
            fb_status = st.selectbox("Assessment / Status", ["ready", "not_ready", "uncertain", "transplanted", "survived", "stunted"])
            fb_notes = st.text_input("Notes", "")

        if st.button("Save feedback", width="stretch"):
            db.save_feedback(
                db.DEFAULT_DB_PATH,
                prediction_id=None,
                plant_id=fb_plant,
                stage=fb_stage,
                assessment=fb_status,
                actual_transplant_date=fb_date if fb_stage in ("transplantation", "fourteen_day_followup") else None,
                notes=fb_notes,
            )
            st.success("Feedback recorded successfully!")


# ---------------------------------------------------------
# TAB 5: DATA & ML MODELS
# ---------------------------------------------------------
with tab_train:
    st.subheader("Machine learning models & dataset management")
    st.write(
        "Controlled evaluation: models are cross-validated using GroupKFold (grouped by plant_id). "
        "A model is only marked validated when it beats the dummy baseline on MAE."
    )

    m_col_left, m_col_right = st.columns([1, 1])

    with m_col_left:
        with st.container(border=True):
            st.markdown("##### Model training & validation")
            train_spec = st.selectbox("Species to train", species_list if species_list else ["Sunflower(SuperOrange)"])

            if st.button("Run cross-validation & train", type="primary", width="stretch"):
                with st.spinner("Training candidate models (RandomForest, Ridge, Dummy)..."):
                    res = ml.train_countdown_model(species=train_spec)

                if res["validated"]:
                    st.success(
                        f"Model validated! Winning algorithm: **{res['best_model_name']}** with CV MAE of **{res['best_mae']} days** "
                        f"(beating baseline MAE {res['baseline_mae']} days).",
                        title="Validation passed",
                    )
                else:
                    st.warning(f"Model not validated: {res.get('reason')}", title="Validation gated")

                if "cv_scores" in res:
                    st.json(res["cv_scores"])

    with m_col_right:
        with st.container(border=True):
            st.markdown("##### Species dataset summary")
            if species_list:
                summary_data = []
                for sp in species_list:
                    obs_count = len(db.get_observations(species=sp))
                    t_rows = len(db.get_training_rows(species=sp))
                    summary_data.append({
                        "Species": sp,
                        "Observations": obs_count,
                        "Training samples (with readiness)": t_rows,
                    })
                st.dataframe(pd.DataFrame(summary_data), width="stretch")
            else:
                st.info("No species data in database.")

            st.caption("To upload additional plant observation data, visit the 'Upload CSV dataset' tab.")
