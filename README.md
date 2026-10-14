# Plant Readiness Countdown Prototype (species-aware, with 14-day feedback)

This project estimates **days remaining until a plant reaches its
transplant-ready stage**, using recorded **readiness events** as labels,
with a species-aware model fallback order and a 14-day post-transplant
feedback loop.

> **Honesty rules built into this code**
>
> - The real Super Orange CSV has **no verified readiness dates**, so no
>   readiness countdown model is trained from it. The real-data path shows
>   growth trends and prints
>   `Readiness prediction unavailable: verified readiness-event data is not yet sufficient.`
> - Synthetic data and synthetic-trained models always carry a persistent
>   **`SYNTHETIC DEMO`** indicator. Synthetic models are never used for a
>   real plant unless you explicitly pass `--allow-synthetic`.
> - The old workflow (Grok API criteria -> hardcoded rule labels ->
>   classifier "accuracy" against its own rules) has been removed.

## Files

| File | Purpose |
|---|---|
| `model_train.py` | Training entry point. Two explicit modes: `--mode demo` (synthetic) and `--mode real` (CSV import + quality/growth analysis + honest refusal). `--retrain` adds a synthetic cohort and compares old vs new models. |
| `prediction.py` | Prediction entry point: species-aware fallback (Level 1..4), `--allow-synthetic`, `--json`. |
| `plant_feedback.py` | CLI workflow: register, observe, predict, transplant, due reminders, submit feedback, history, species-status, list. |
| `demo_workflow.py` | Scripted end-to-end SYNTHETIC demonstration (steps 0-6, including retrain comparison). |
| `plantreadiness/db.py` | SQLite schema + validation + duplicate protection + date/growth arithmetic. |
| `plantreadiness/csv_import.py` | Real CSV import (append-only, idempotent) + data-quality checks + growth analysis. |
| `plantreadiness/synthetic.py` | Reproducible synthetic generator (documented assumptions, not botanical facts). |
| `plantreadiness/model.py` | Countdown training, grouped validation, artefacts + metadata, fallback selection, retrain gate. |
| `plantreadiness/config.py` | Constants: paths, minimum data requirements, standard messages. |
| `tests/` | Automated tests (dates, growth, duplicates, missing data, fallback, separation, integration). |
| `REPORT.md` | Limitations report: what is demonstrated only with synthetic data. |

The ESP32-CAM / Cloudinary / OpenCV measurement pipeline
(`plant_measurement.py`, `plant_daily_measurements.csv`) is **unchanged**.

## Setup

```bash
cd /Users/visionary/Documents/CODING/EMBEDDED
source .venv/bin/activate
pip install -r requirements.txt
```

## Quick start

### Mode A - synthetic demonstration (complete workflow)

```bash
# 1) train the demo models on generated synthetic cohorts
python model_train.py --mode demo

# 2) scripted end-to-end demo: register -> predict -> transplant ->
#    14-day feedback -> retrain -> compare on held-out synthetic plants
python demo_workflow.py

# 3) retrain later: new cohort, candidate vs deployed model on the SAME
#    held-out set (replaces only if strictly better)
python model_train.py --mode demo --retrain
```

### Mode B - real data (Super Orange CSV)

```bash
# imports the CSV, runs quality checks + growth analysis,
# then honestly reports that no readiness model can be trained yet
python model_train.py --mode real --csv super_orange_growth_data_with_growth_rate.csv
```

### 14-day feedback workflow

```bash
# register / list / measure
python plant_feedback.py register --species "Super Orange" --variety Early \
    --date-started 2083-04-24 --ref PLANT-11
python plant_feedback.py list
python plant_feedback.py observe --plant 1 --date 2083-05-20 \
    --age-days 26 --height 5.5 --growth-rate 0.3

# prediction (level 4 message until real readiness labels exist;
# add --allow-synthetic to explicitly see the labelled demo model)
python plant_feedback.py predict --plant 1
python plant_feedback.py predict --plant 1 --allow-synthetic

# transplant -> feedback becomes due 14 days later
python plant_feedback.py transplant --plant 1 --date 2083-05-25 --height 6.4

# pending/overdue reminders are also printed every time the tool opens
python plant_feedback.py due

# submit the 14-day outcome (dead plant: drop --height-after, growth stays NULL)
python plant_feedback.py submit --feedback 1 --submitted-date 2083-06-08 \
    --survived 1 --height-after 7.6 --condition healthy --notes "strong growth"

# inspect history / how many usable readiness events exist for a species
python plant_feedback.py history --plant 1
python plant_feedback.py species-status --species "Super Orange"
```

### Prediction directly

```bash
python prediction.py --plant 1                      # refuses honestly today
python prediction.py --plant 1 --allow-synthetic    # labelled demo model
python prediction.py --species "Super Orange" --variety Early \
    --age-days 20 --height-cm 4.2 --growth-rate 0.3 --json
```

## Model fallback order (species-aware)

1. **Validated species/variety model** (real labelled data),
2. **Broader real model** for the species (uncertainty + limitations shown),
3. **SYNTHETIC DEMO model** - only with explicit `--allow-synthetic`, always
   labelled, with a warning if applied to a real plant,
4. Otherwise: `Readiness prediction unavailable: verified readiness-event
   data is not yet sufficient.` (the system is collecting data).

A model is only eligible if it is marked `validated`: minimum
`4` independent plants and `6` labelled readiness events
(`plantreadiness/config.py`), trained with plant-grouped GroupKFold
validation and reported MAE in days against a mean-baseline.

## Database

SQLite (`plant_readiness.db`, created on first use) with tables
`plants`, `observations`, `readiness_events`, `feedback_14d`:
primary/foreign keys, CHECK constraints, duplicate protection
(one observation per plant per date, one feedback record per transplant,
feedback is immutable after submission), and `data_source` on every row to
keep `real` and `synthetic` separated. Historical observations and feedback
are never overwritten.

## Tests

```bash
python -m unittest discover -s tests -v
```

Covers: date arithmetic (readiness target, +14-day due dates), growth
calculation (including missing/deceased plants), duplicate feedback and
observation protection, species fallback levels, real/synthetic
data-source separation, CSV import, and the end-to-end demo workflow.

## Retraining policy

Feedback is **saved and validated first**; it is not automatically turned
into readiness labels. Retraining only happens when minimum data
requirements are met, the candidate is trained **excluding** the holdout
plants, and the candidate must score a strictly lower holdout MAE than the
deployed model on the **same held-out plants** - otherwise the current model
is kept.
# PLANTING-WINDOW-PREDICTOR
