"""
app.py — Streamlit Web Application (Simplified)

Core flow:
  1. Select species
  2. Pick observation date (or "Latest")
  3. Click "Fetch & Predict" → pulls image from Cloudinary, measures with OpenCV, predicts
  4. Shows: height, stem width, leaves, days remaining, readiness date, model used
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
import cloudinary_refresh as cr

st.set_page_config(
    page_title="Plant Readiness Predictor",
    page_icon="🌱",
    layout="centered",
)

# Cache species list
@st.cache_data(ttl=60)
def load_species():
    return db.get_species_list()


# Header
st.title("Plant Readiness Predictor")
st.caption("Select a species → pick a date → fetch from Cloudinary → measure → predict.")

species_list = load_species()

if not species_list:
    st.warning("No species found. Seed the database first: `python cli.py seed`")
    st.stop()

# --- Inputs ---
with st.container(border=True):
    col1, col2 = st.columns(2)
    with col1:
        selected_species = st.selectbox("Species", species_list, key="species")
    with col2:
        obs_date = st.date_input("Observation date", value=date.today(), key="obs_date")

    use_latest = st.checkbox("Use latest available date", value=True, key="use_latest")

    predict_btn = st.button("Fetch & Predict", type="primary", width="stretch")

# --- Prediction ---
if predict_btn:
    with st.spinner("Fetching image, measuring, predicting..."):
        # 1. Refresh from Cloudinary (pull + measure + import)
        report = cr.refresh(db.DEFAULT_DB_PATH, species=selected_species, captures=4)

        # 2. Get the observation date to use
        if use_latest:
            obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
            if obs:
                latest = max(o["observation_date"] for o in obs)
                obs_date = latest

        # 3. Get latest observation for this species
        obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
        recent = None
        if obs:
            # Filter to the selected date
            date_obs = [o for o in obs if str(o["observation_date"]) == str(obs_date)]
            if date_obs:
                recent = date_obs[-1]
            else:
                recent = obs[-1]  # fallback to last observation

        # 4. Extract measurements
        height = recent["height_cm"] if recent else None
        age = recent["age_days"] if recent else None
        growth = recent["growth_rate_cm_per_day"] if recent else None
        stem = recent["stem_width_cm"] if recent else None
        leaves = recent["visible_true_leaf_count"] if recent else None

        # 5. Train model (if possible)
        train_res = ml.train_countdown_model(db_path=db.DEFAULT_DB_PATH, species=selected_species)
        pipe = train_res.get("pipeline") if train_res.get("validated") else None

        # 6. Predict
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

    # --- Results ---
    st.divider()

    # Measurements
    st.subheader("Measurements")
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Height", f"{height:.2f} cm" if height is not None else "—")
    with m2:
        st.metric("Stem width", f"{stem:.3f} cm" if stem is not None else "—")
    with m3:
        st.metric("Leaves", leaves if leaves is not None else "—")

    st.caption(f"Age: {age} days · Growth rate: {growth} cm/day" if age else "")

    # Prediction
    st.subheader("Prediction")
    if pred.predicted_days_remaining is not None:
        p1, p2 = st.columns(2)
        with p1:
            st.metric("Days remaining", f"{pred.predicted_days_remaining} days")
        with p2:
            st.metric("Readiness date", pred.predicted_readiness_date)

        # Which model
        if pred.method == "validated_ml_model":
            st.success(f"Model: **{pred.method.replace('_', ' ').title()}** (Confidence: {pred.confidence.title()})")
        elif pred.method == "provisional_historical_baseline":
            st.info(f"Model: **{pred.method.replace('_', ' ').title()}** (Confidence: {pred.confidence.title()})")
        else:
            st.error(f"Model: **{pred.method.replace('_', ' ').title()}** (Confidence: {pred.confidence.title()})")

        st.caption(pred.summary_message)
        st.caption(f"Supporting cohort: {pred.contributing_plants} contributing plants.")
    else:
        st.error(pred.summary_message)

    # Camera refresh status
    with st.expander("Camera refresh details"):
        st.json(report)