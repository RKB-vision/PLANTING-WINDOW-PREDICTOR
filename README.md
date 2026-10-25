# Plant Transplant Readiness System (Streamlined Architecture)

> **Branch:** `simplified-architecture`  
> A lightweight, consolidated implementation of the Plant Transplant Readiness Prediction System in **6 core Python files**.

---

## 1. File Map (The 6 Core Files)

| File | Purpose | Lines |
|---|---|---|
| [`db.py`](file:///Users/visionary/Documents/CODING/EMBEDDED/db.py) | **SQLite Data Layer**: Schema, seeding, temporal growth rate derivation, training row extraction, predictions, and feedback tracking. | ~390 |
| [`vision.py`](file:///Users/visionary/Documents/CODING/EMBEDDED/vision.py) | **Computer Vision**: OpenCV calibration rod detection (px $\rightarrow$ cm), green foliage segmentation, height/stem width/leaf count measurement, visual HUD overlay. | ~230 |
| [`ml.py`](file:///Users/visionary/Documents/CODING/EMBEDDED/ml.py) | **Machine Learning & Baselines**: Historical median growth trajectory, GroupKFold CV regressor (`RandomForest`, `Ridge`, `Dummy`), and fallback prediction ladder. | ~280 |
| [`app.py`](file:///Users/visionary/Documents/CODING/EMBEDDED/app.py) | **Streamlit Web Application**: Interactive dashboard for countdown predictions, live OpenCV image processing, growth trajectories, and feedback recording. | ~260 |
| [`cli.py`](file:///Users/visionary/Documents/CODING/EMBEDDED/cli.py) | **Command Line Interface**: Thin CLI for headless automation (seed, measure, train, predict, evaluate). | ~110 |
| [`test_simplified.py`](file:///Users/visionary/Documents/CODING/EMBEDDED/test_simplified.py) | **Test Suite**: Automated tests verifying database integrity, vision calibration, GroupKFold ML, fallbacks, and feedback evaluation. | ~135 |

---

## 2. Non-Negotiable Architectural Principles Preserved

1. **Distinct Record Types**: Observations, species summaries, predictions, readiness events, and feedback are stored in separate relational tables.
2. **No Data Fabrication**: Missing values remain `None` (never 0). The initial observation for any plant has `growth_rate = None`.
3. **Honest Predictions**: Follows a strict fallback ladder:
   - **Level 1**: Validated ML model (only if beating DummyRegressor baseline and trained on $\ge 4$ independent plants and $\ge 6$ events).
   - **Level 2**: Historical cohort baseline (median trajectory).
   - **Level 3**: Insufficient evidence (explicitly states limitations).
4. **Leakage Prevention**: GroupKFold cross-validation groups samples by `plant_id` so the same plant never leaks across training and validation splits.
5. **Feedback Loop**: Historical predictions are append-only and linked to eventual transplant date and 14-day outcome records to evaluate model accuracy without rewriting history.

---

## 3. Quick Start

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Launch Interactive Web App
```bash
streamlit run app.py
```
Open your browser to `http://localhost:8501`.

### Run via Command Line (CLI)

```bash
# 1. Seed database from historical dataset
python cli.py seed --csv data/super_orange_growth_data.csv

# 2. Process an image with OpenCV
python cli.py measure Plant02_20261020_100000.jpg --rod-cm 12.0

# 3. Train & cross-validate ML countdown model
python cli.py train --species "Sunflower(SuperOrange)"

# 4. Predict transplant countdown
python cli.py predict --species "Sunflower(SuperOrange)" --height 4.2 --age 16

# 5. Evaluate historical predictions vs actual outcomes
python cli.py evaluate
```

### Run Automated Tests
```bash
pytest test_simplified.py -v
```
