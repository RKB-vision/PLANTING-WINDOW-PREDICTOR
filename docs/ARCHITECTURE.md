# Plant Transplant Readiness System — Architecture Context

This document is the canonical architecture reference for the project. It is the
"core idea" every contributor (human or agent) should read **before** making
changes. `AGENTS.md` at the repository root summarises this and links here.

## 1. System Purpose

This project is a plant-monitoring system that uses camera-derived measurements
and historical growth data to estimate when a plant species is likely to be
ready for transplantation.

The primary prediction output is:

- Estimated days remaining until transplant readiness.
- Estimated readiness date.
- Supporting growth measurements and historical reference information.
- An indication of prediction confidence, evidence availability, and method used.

The system should become more useful over time by comparing its predictions with
human assessments, actual transplant dates, and subsequent plant outcomes.

## 2. Existing Technology and Components

The project has a Python-based processing and business-logic layer, a Flask
backend, a Next.js frontend, and a database/repository layer.

### Camera and measurement pipeline

- `plant_measurement.py` processes captured plant images using OpenCV.
- It extracts supported measurements, including plant height and stem width,
  using image segmentation and calibration.
- Measurements are written to `data/generated/plant_daily_measurements.csv` or another
  configured measurement output.
- Cloudinary is used in the camera-image workflow where configured.

### Historical data

- `data/super_orange_growth_data.csv` contains historical growth-monitoring records
  for 10 Super Orange plants.
- Its columns are `Date`, `Age`, `Plant_ID`, and `Height_cm`.
- The records describe plants that subsequently grew successfully and were
  transplanted.
- The supplied account of the data is that these plants were transplanted the
  day after their final monitoring day.
- These records provide a successful-plant reference cohort. They do not
  establish that transplantation on that date was the earliest or optimal
  decision.

### Python domain and data layer

- `plantreadiness/repository.py` provides database access.
- `plantreadiness/services.py` contains shared observation, growth-calculation,
  and domain logic.
- `plantreadiness/config.py` contains configuration.
- `prediction.py` contains prediction functions and model-selection behavior.
- `backend/app.py` exposes application functionality through Flask endpoints.

### Frontend

- The main interface is implemented in `nextjs/`.
- The frontend communicates with the backend through its API routes.
- The intended user workflow includes choosing a species, viewing available
  measurement information, obtaining a prediction, and providing feedback.

The precise responsibilities, schemas, and integration points of existing
modules must be understood from the repository itself. This description provides
architectural context; it is not a guarantee that every capability listed below
has already been implemented.

## 3. Main Data Flow

The system has the following conceptual flow:

Camera images → Image measurement pipeline → Plant observations → Database →
Species-level aggregation → Growth reference and prediction → User interface.

Historical growth CSV data enters the same data layer and contributes to the
species reference.

User feedback enters the data layer as a separate type of information. It is
later used to evaluate prediction quality and inform controlled improvements.

The image-measurement process, historical reference data, prediction inputs,
prediction outputs, and feedback should remain distinguishable rather than being
treated as interchangeable records.

## 4. Species-Level Aggregation

The intended prediction workflow is species-level, not necessarily individual-
plant tracking.

When a user selects a species and observation date, the system gathers the
available measurements for that species on that date and calculates the median
of each numerical measurement. The resulting values form a representative plant
profile.

For example, the representative profile may contain median height, median age,
and median growth rate when those measurements are available.

The median reduces the influence of unusually tall, short, or rapidly growing
plants. Each calculated median should be associated with the number of plants
contributing to it, so the system can indicate how much evidence supports the
reference.

Missing data must not be silently invented. The system must distinguish
unavailable measurements from actual zero values.

## 5. Successful-Plant Growth Reference

The Super Orange historical cohort supplies an initial reference for observed
growth.

For each plant, growth rate can be calculated between consecutive valid
measurements:

`growth rate = change in height / elapsed time in days`

Measurements must be placed in the correct temporal order. Missing observations
and irregular measurement intervals must be handled explicitly.

The system can then calculate median height and median growth rate across the
cohort at comparable ages or observation positions. These statistics describe
the typical trajectory of the successful plants in the dataset.

Growth rate is a candidate indicator of transplant readiness, not a definitive
readiness measurement. A maximum growth rate must not automatically be labelled
as the optimal transplant point.

The reference should initially be used to investigate whether growth patterns
near the observed transplant date can support useful predictions. The
successful-only dataset does not provide enough evidence by itself to determine
how much earlier a plant could have been transplanted successfully.

## 6. Prediction Layer

The prediction layer combines the current available observation information with
an appropriate species reference or trained prediction method.

Its goal is to estimate days remaining until likely transplant readiness and
derive the corresponding date.

The system must distinguish between:

- A prediction from a sufficiently validated model.
- A provisional estimate based on the historical reference.
- A synthetic demonstration prediction.
- An unavailable prediction caused by insufficient evidence.

Growth rate requires measurements at multiple times. In the initial days, when
only one valid measurement exists, the system must not invent a growth rate. An
age-based or reference-based estimate is acceptable only when its limitations are
made explicit.

A prediction must use only information available at the prediction time.
Historical evaluation must not leak later observations or eventual outcomes into
earlier predictions.

## 7. Feedback and Outcome Data

Feedback should be available to anyone involved in assessing readiness, carrying
out transplantation, or monitoring the plant afterward. The same person may
perform all these roles, or different people may contribute.

The feedback workflow has three stages.

**Readiness assessment:** Records whether the plant appears ready, not ready, or
uncertain, along with the assessment date and optional notes.

**Actual transplantation:** Records whether transplantation occurred, its actual
date, the plant's condition, and optional notes.

**Fourteen-day follow-up:** Records survival, plant condition, and available
growth measurements approximately 14 days after transplantation.

These stages are distinct. An assessment does not prove that transplantation
occurred, and a transplant record does not automatically establish a successful
outcome.

The system must preserve the original prediction, the measurements available at
the time, and the prediction method/version. Later feedback must be linked to
that prediction without rewriting its historical contents.

Missing feedback must remain missing or pending; it must not be interpreted as
failure or death.

## 8. Prediction Evaluation and Improvement

Feedback initially serves to evaluate the method, not automatically retrain the
model after every submission.

The system can compare predicted days remaining with the actual recorded
transplant timing, track prediction errors, examine 14-day outcomes, and
investigate which growth-rate patterns are associated with useful outcomes.

Prediction timing accuracy and transplant success are related but separate
questions. A prediction may accurately estimate when users in the historical
cohort transplanted their plants without proving that the timing was optimal.

As reliable evidence accumulates, alternative methods can be evaluated and
compared. A new model should only replace an existing method when evaluation
supports the change. The successful historical cohort, later feedback, and
synthetic demonstration data must remain identifiable as different evidence
sources.

## 9. Core Architectural Principles

1. Use the existing repository, database abstraction, shared services, Flask
   backend, and Next.js frontend rather than creating a parallel system.
2. Preserve the distinction between raw observations, species-level summaries,
   predictions, and feedback.
3. Keep the species-level median approach as the initial reference strategy.
4. Treat growth rate as a measurable candidate signal whose usefulness must be
   evaluated.
5. Preserve prediction history so outcomes can be compared with what the system
   actually predicted.
6. Keep historical observations, human judgements, actual outcomes, and
   synthetic data distinguishable.
7. Improve prediction logic using evidence and controlled evaluation rather than
   assuming that more feedback automatically makes a model better.
8. Avoid destructive database or legacy-application changes without explicit
   approval.

This architecture describes how the existing plant-monitoring system is intended
to function and how its prediction and feedback components fit together. The
actual implementation should be understood from the source code before deciding
whether any component requires modification.
