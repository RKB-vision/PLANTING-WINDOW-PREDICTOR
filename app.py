"""
app.py — Streamlit Web Application

Flow:
  1. Select species + date
  2. Click "Predict"
     - If measurements exist → show them, proceed to predict
     - If not → modal: "Plant details are not calibrated"
       → "Pull parameters" button → fetch from Cloudinary, measure, show image carousel
       → "Calibrate & Predict" button → generate prediction
  3. Show result: which model, days remaining, readiness date, kid-friendly explanation
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import streamlit as st
from PIL import Image
import requests
import io

import db
import ml
import cloudinary_refresh as cr

st.set_page_config(
    page_title="Plant Readiness Predictor",
    page_icon="🌱",
    layout="centered",
)

st.title("Plant Readiness Predictor")
st.caption("Select a species → pick a date → predict.")

# --- Species selector ---
species_list = db.get_species_list()
if not species_list:
    st.warning("No species found. Seed the database first: `python cli.py seed`")
    st.stop()

selected_species = st.selectbox("Species", species_list, key="species")

# --- Date selector ---
use_latest = st.checkbox("Use latest available date", value=True, key="use_latest")
if use_latest:
    obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
    if obs:
        obs_date = max(o["observation_date"] for o in obs)
    else:
        obs_date = date.today()
    st.caption(f"Using date: {obs_date}")
else:
    obs_date = st.date_input("Observation date", value=date.today(), key="obs_date")
    st.caption(f"Using date: {obs_date}")

# --- Check existing measurements ---
obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
date_obs = [o for o in obs if str(o["observation_date"]) == str(obs_date)] if obs else []
has_data = len(date_obs) > 0
recent = date_obs[-1] if date_obs else None

# Show existing measurements if available
if has_data and recent:
    st.success("Measurements already exist for this date.")
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Height", f"{recent['height_cm']:.2f} cm" if recent['height_cm'] is not None else "—")
    with m2:
        st.metric("Stem width", f"{recent['stem_width_cm']:.3f} cm" if recent['stem_width_cm'] is not None else "—")
    with m3:
        st.metric("Leaves", recent['visible_true_leaf_count'] if recent['visible_true_leaf_count'] is not None else "—")
    st.caption(f"Age: {recent['age_days']} days · Growth rate: {recent['growth_rate_cm_per_day']} cm/day" if recent['age_days'] else "")


# --- Modal for uncalibrated plants ---
@st.dialog("Plant details are not calibrated")
def uncalibrated_modal():
    st.write("No measurements found for this species and date. Pull parameters from Cloudinary?")

    if st.button("Pull parameters", type="primary", width="stretch"):
        with st.spinner("Pulling images from Cloudinary and measuring..."):
            report = cr.refresh(db.DEFAULT_DB_PATH, species=selected_species, captures=4)
        st.session_state["pull_report"] = report
        st.rerun()

    report = st.session_state.get("pull_report")
    if report:
        # Collect all images across sources
        all_images = []
        for src in report.get("sources", []):
            all_images.extend(src.get("images", []))

        if all_images:
            # Image carousel with left/right navigation
            if "img_idx" not in st.session_state:
                st.session_state["img_idx"] = 0

            idx = st.session_state["img_idx"]
            idx = max(0, min(idx, len(all_images) - 1))
            st.session_state["img_idx"] = idx

            img_data = all_images[idx]

            # Display image
            try:
                resp = requests.get(img_data["url"], timeout=10)
                pil_img = Image.open(io.BytesIO(resp.content))
                st.image(pil_img, caption=f"Image {idx + 1} of {len(all_images)}: {img_data.get('public_id', '')}", width="stretch")
            except Exception as e:
                st.warning(f"Could not load image: {e}")

            # Measurements for this image
            m1, m2, m3 = st.columns(3)
            with m1:
                st.metric("Height", f"{img_data['height_cm']:.2f} cm" if img_data.get('height_cm') is not None else "—")
            with m2:
                st.metric("Stem width", f"{img_data['stem_width_cm']:.3f} cm" if img_data.get('stem_width_cm') is not None else "—")
            with m3:
                st.metric("Leaves", img_data.get('leaf_count', "—"))

            st.caption(f"Date: {img_data.get('date', '—')} · Calibrated: {'Yes' if img_data.get('calibrated') else 'No'}")

            # Left/right navigation
            nav1, nav2, nav3 = st.columns([1, 2, 1])
            with nav1:
                if st.button("◀ Prev", disabled=(idx == 0), width="stretch"):
                    st.session_state["img_idx"] = idx - 1
                    st.rerun()
            with nav2:
                st.caption(f"Image {idx + 1} / {len(all_images)}")
            with nav3:
                if st.button("Next ▶", disabled=(idx == len(all_images) - 1), width="stretch"):
                    st.session_state["img_idx"] = idx + 1
                    st.rerun()

            # Aggregated daily measurement
            st.divider()
            st.markdown("**Aggregated daily measurement (median across captures):**")
            obs2 = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
            date_obs2 = [o for o in obs2 if str(o["observation_date"]) == str(obs_date)] if obs2 else []
            if date_obs2:
                r = date_obs2[-1]
                a1, a2, a3 = st.columns(3)
                with a1:
                    st.metric("Height", f"{r['height_cm']:.2f} cm" if r['height_cm'] is not None else "—")
                with a2:
                    st.metric("Stem width", f"{r['stem_width_cm']:.3f} cm" if r['stem_width_cm'] is not None else "—")
                with a3:
                    st.metric("Leaves", r['visible_true_leaf_count'] if r['visible_true_leaf_count'] is not None else "—")
                st.caption(f"Age: {r['age_days']} days · Growth rate: {r['growth_rate_cm_per_day']} cm/day" if r['age_days'] else "")

        elif report.get("reason"):
            st.warning(report["reason"])
        else:
            for src in report.get("sources", []):
                if src.get("status") not in ("imported",):
                    st.warning(f"{src.get('prefix')}: {src.get('status')} — {src.get('message', '')}")

        # Calibrate & Predict button
        st.divider()
        if st.button("Calibrate & Predict", type="primary", width="stretch"):
            st.session_state["calibrate_predict"] = True
            st.rerun()


# --- Predict button ---
st.divider()
predict_btn = st.button("Predict", type="primary", width="stretch")

if predict_btn:
    if not has_data and "pull_report" not in st.session_state:
        uncalibrated_modal()
    else:
        # Re-read observations
        obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
        date_obs = [o for o in obs if str(o["observation_date"]) == str(obs_date)] if obs else []
        recent = date_obs[-1] if date_obs else (obs[-1] if obs else None)

        height = recent["height_cm"] if recent else None
        stem = recent["stem_width_cm"] if recent else None
        leaves = recent["visible_true_leaf_count"] if recent else None
        age = recent["age_days"] if recent else None
        growth = recent["growth_rate_cm_per_day"] if recent else None

        with st.spinner("Predicting..."):
            train_res = ml.train_countdown_model(db_path=db.DEFAULT_DB_PATH, species=selected_species)
            pipe = train_res.get("pipeline") if train_res.get("validated") else None

            pred = ml.predict_species_readiness(
                db_path=db.DEFAULT_DB_PATH,
                species=selected_species,
                current_height_cm=height,
                current_age_days=age,
                current_growth_rate=growth,
                current_stem_width=stem,
                current_leaf_count=leaves,
                trained_model=pipe,
            )

        # --- Kid-friendly result ---
        st.divider()
        if pred.predicted_days_remaining is not None:
            st.markdown("### 🌱 Result")
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Days until ready", f"{pred.predicted_days_remaining} days")
            with col2:
                st.metric("Ready around", pred.predicted_readiness_date)

            if pred.method == "validated_ml_model":
                st.success(f"**How:** A trained AI model looked at {pred.contributing_plants} plants and learned the pattern. (Confidence: {pred.confidence})")
            elif pred.method == "provisional_historical_baseline":
                st.info(f"**How:** Compared to historical plants of the same age. (Confidence: {pred.confidence})")
            else:
                st.error(f"**How:** {pred.method.replace('_', ' ').title()} (Confidence: {pred.confidence})")

            st.caption(pred.summary_message)
        else:
            st.error(f"**Could not predict:** {pred.summary_message}")
            st.caption("Tip: Make sure the plant has measurements and the species has enough historical data.")

# Handle Calibrate & Predict from modal
if st.session_state.get("calibrate_predict"):
    del st.session_state["calibrate_predict"]
    # Re-read observations
    obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
    date_obs = [o for o in obs if str(o["observation_date"]) == str(obs_date)] if obs else []
    recent = date_obs[-1] if date_obs else (obs[-1] if obs else None)

    height = recent["height_cm"] if recent else None
    stem = recent["stem_width_cm"] if recent else None
    leaves = recent["visible_true_leaf_count"] if recent else None
    age = recent["age_days"] if recent else None
    growth = recent["growth_rate_cm_per_day"] if recent else None

    with st.spinner("Calibrating and predicting..."):
        train_res = ml.train_countdown_model(db_path=db.DEFAULT_DB_PATH, species=selected_species)
        pipe = train_res.get("pipeline") if train_res.get("validated") else None

        pred = ml.predict_species_readiness(
            db_path=db.DEFAULT_DB_PATH,
            species=selected_species,
            current_height_cm=height,
            current_age_days=age,
            current_growth_rate=growth,
            current_stem_width=stem,
            current_leaf_count=leaves,
            trained_model=pipe,
        )

    st.divider()
    if pred.predicted_days_remaining is not None:
        st.markdown("### 🌱 Result")
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Days until ready", f"{pred.predicted_days_remaining} days")
        with col2:
            st.metric("Ready around", pred.predicted_readiness_date)

        if pred.method == "validated_ml_model":
            st.success(f"**How:** A trained AI model looked at {pred.contributing_plants} plants and learned the pattern. (Confidence: {pred.confidence})")
        elif pred.method == "provisional_historical_baseline":
            st.info(f"**How:** Compared to historical plants of the same age. (Confidence: {pred.confidence})")
        else:
            st.error(f"**How:** {pred.method.replace('_', ' ').title()} (Confidence: {pred.confidence})")

        st.caption(pred.summary_message)
    else:
        st.error(f"**Could not predict:** {pred.summary_message}")