"""
app.py — Streamlit Web Application (Simplified)

Core flow:
  1. Select species + date
  2. If measurements already exist → show them, skip fetch
  3. If no measurements → "Fetch from Cloudinary" button → fetch + measure + show images
  4. "Calibrate & Predict" button → runs prediction, shows kid-friendly result
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import streamlit as st
import pandas as pd

import db
import ml
import vision
import cloudinary_refresh as cr

st.set_page_config(
    page_title="Plant Readiness Predictor",
    page_icon="🌱",
    layout="centered",
)

st.title("Plant Readiness Predictor")
st.caption("Select a species → pick a date → fetch or use existing → predict.")

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
        latest = max(o["observation_date"] for o in obs)
        obs_date = latest
    else:
        obs_date = date.today()
    st.caption(f"Using date: {obs_date}")
else:
    obs_date = st.date_input("Observation date", value=date.today(), key="obs_date")

# --- Check existing measurements ---
obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
date_obs = [o for o in obs if str(o["observation_date"]) == str(obs_date)] if obs else []

has_data = len(date_obs) > 0
recent = date_obs[-1] if date_obs else None

# --- Show existing or fetch ---
if has_data:
    st.success("Measurements already exist for this date.")
    height = recent["height_cm"]
    stem = recent["stem_width_cm"]
    leaves = recent["visible_true_leaf_count"]
    age = recent["age_days"]
    growth = recent["growth_rate_cm_per_day"]

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Height", f"{height:.2f} cm" if height is not None else "—")
    with m2:
        st.metric("Stem width", f"{stem:.3f} cm" if stem is not None else "—")
    with m3:
        st.metric("Leaves", leaves if leaves is not None else "—")
    st.caption(f"Age: {age} days · Growth rate: {growth} cm/day" if age else "")

    fetch_btn = False
else:
    st.info("No measurements for this date. Fetch from Cloudinary?")
    fetch_btn = st.button("Fetch from Cloudinary", type="primary", width="stretch")

# --- Fetch from Cloudinary ---
if fetch_btn:
    with st.spinner("Pulling images from Cloudinary and measuring..."):
        report = cr.refresh(db.DEFAULT_DB_PATH, species=selected_species, captures=4)

    if report.get("observations_inserted", 0) > 0:
        st.success(f"Imported {report['observations_inserted']} new observation(s).")
        # Re-fetch observations
        obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
        date_obs = [o for o in obs if str(o["observation_date"]) == str(obs_date)] if obs else []
        if date_obs:
            recent = date_obs[-1]
            height = recent["height_cm"]
            stem = recent["stem_width_cm"]
            leaves = recent["visible_true_leaf_count"]
            age = recent["age_days"]
            growth = recent["growth_rate_cm_per_day"]

            m1, m2, m3 = st.columns(3)
            with m1:
                st.metric("Height", f"{height:.2f} cm" if height is not None else "—")
            with m2:
                st.metric("Stem width", f"{stem:.3f} cm" if stem is not None else "—")
            with m3:
                st.metric("Leaves", leaves if leaves is not None else "—")
            st.caption(f"Age: {age} days · Growth rate: {growth} cm/day" if age else "")
    elif report.get("reason"):
        st.warning(report["reason"])
    else:
        for src in report.get("sources", []):
            if src.get("status") not in ("imported",):
                st.warning(f"{src.get('prefix')}: {src.get('status')} — {src.get('message', '')}")

    with st.expander("Camera refresh details"):
        st.json(report)

# --- Re-read measurements (in case fetch just happened) ---
obs = db.get_observations(db_path=db.DEFAULT_DB_PATH, species=selected_species)
date_obs = [o for o in obs if str(o["observation_date"]) == str(obs_date)] if obs else []
recent = date_obs[-1] if date_obs else (obs[-1] if obs else None)

height = recent["height_cm"] if recent else None
stem = recent["stem_width_cm"] if recent else None
leaves = recent["visible_true_leaf_count"] if recent else None
age = recent["age_days"] if recent else None
growth = recent["growth_rate_cm_per_day"] if recent else None

# --- Calibrate & Predict ---
st.divider()
st.subheader("Calibrate & Predict")

if st.button("Calibrate & Predict", type="primary", width="stretch"):
    with st.spinner("Calibrating and predicting..."):
        # Train model if possible
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
        st.markdown(f"### 🌱 Result")
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Days until ready", f"{pred.predicted_days_remaining} days")
        with col2:
            st.metric("Ready around", pred.predicted_readiness_date)

        # Which model
        if pred.method == "validated_ml_model":
            st.success(f"**How:** A trained AI model (Random Forest) looked at {pred.contributing_plants} plants and learned the pattern. (Confidence: {pred.confidence})")
        elif pred.method == "provisional_historical_baseline":
            st.info(f"**How:** Compared to historical plants of the same age. (Confidence: {pred.confidence})")
        else:
            st.error(f"**How:** {pred.method.replace('_', ' ').title()} (Confidence: {pred.confidence})")

        st.caption(pred.summary_message)
    else:
        st.error(f"**Could not predict:** {pred.summary_message}")
        st.caption("Tip: Make sure the plant has measurements and the species has enough historical data.")