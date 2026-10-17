# AGENTS.md

> **Read this first.** Before changing anything in this repository, read the
> canonical architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
> It describes the system's purpose, data flow, and the non-negotiable
> principles below. Ideas in this file are a summary; the linked document is the
> source of truth.

## What this project is

A plant-monitoring system that uses camera-derived measurements and historical
growth data to estimate when a plant species is likely to be ready for
transplantation.

**Core workflow**

```
ESP32-CAM → Cloudinary (cloud images)
          → measurement pipeline (OpenCV: height, stem width, true leaves)
          → plant observations (CSV → database)
          → species-level aggregation (medians + contributing plant counts)
          → growth reference / prediction
          → Next.js UI (select species → predict readiness date)
          → feedback (assessment / transplant record / 14-day follow-up)
          → evaluation (feedback-driven, controlled improvement)
```

## Non-negotiable rules

1. **Use the existing system.** Build on `plantreadiness/`, `backend/`,
   `nextjs/`, and the existing database abstraction — do not create a parallel
   system.
2. **Keep record types distinct.** Raw observations, species-level summaries,
   predictions, and feedback are different things and must stay distinguishable.
3. **Never invent data.** Missing values stay missing; they are not zero and not
   fabricated. When there is a single measurement, growth rate is `NULL`.
4. **Be honest in predictions.** Always state the method
   (validated model / provisional historical baseline / synthetic demo /
   insufficient evidence), the supporting measurements, uncertainty, and
   limitations. Never fabricate growth.
5. **Synthetic ≠ real.** Synthetic data and synthetic-trained models always carry
   a persistent `SYNTHETIC DEMO` indicator and are never used for a real plant
   unless explicitly enabled.
6. **Preserve history.** Predictions and feedback are append-only; later feedback
   links to an earlier prediction without rewriting it. Missing feedback is
   *pending*, never failure/death.
7. **Improve with evidence, not assumptions.** Do not auto-retrain after every
   submission. A new method replaces an old one only when evaluation supports it.
8. **No destructive changes without approval.** Do not reset or drop the
   database or delete real/historical records.

## Where things live

| Path | Purpose |
|---|---|
| `plantreadiness/` | Domain + data layer (config, repository, services, reference, imports) |
| `backend/app.py` | Flask API endpoints |
| `nextjs/` | Next.js frontend |
| `plant_measurement.py` | ESP32-CAM → Cloudinary → OpenCV measurement pipeline |
| `prediction.py` | Prediction / model-selection entry point |
| `plant_feedback.py` | CLI for observations, feedback, camera import |
| `data/` | Committed inputs (`super_orange_growth_data.csv` = historical successful-plant reference cohort) |
| `data/generated/` | Generated pipeline output (git-ignored, e.g. `plant_daily_measurements.csv`) |
| `docs/` | `ARCHITECTURE.md` (canonical, read first), `DEPLOYMENT.md`, `REPORT.md`, `plan.md`, `audit/` |
| `models/` | Trained models + reference baseline |

## Conventions

- Python: `plantreadiness` is the domain package; keep CLI entry points thin.
- Frontend: shared design system lives in `nextjs/styles/globals.css`; keep the
  UI simple and professional (neutral surfaces, restrained green accent, no
  emoji).
- DB changes go through the repository abstraction and forward migrations.
- Run the Python test suite before and after changes.
