# Running & Deploying (local storage)

The system runs on a single device and stores all data in a **local SQLite
database** (`plant_readiness.db`). There is no cloud database dependency.

## Architecture at a glance

| Layer | Technology | Notes |
|---|---|---|
| Measurement pipeline | Python + OpenCV | ESP32-CAM images from Cloudinary -> `data/generated/plant_daily_measurements.csv` |
| Domain / data layer | `plantreadiness/` | Repository (`SQLiteRepository`) + services |
| API | Flask (`backend/app.py`) | Port `8000` |
| Front-end | Next.js (`nextjs/`) | Port `3000`, proxies to the Flask API |
| Storage | Local SQLite | `plant_readiness.db`, created on first use |

## Prerequisites

- Python 3.11+ with `pip`
- Node.js 18+ with `npm`
- (Optional) Cloudinary credentials, only if you want the automatic camera
  refresh.

## 1. Python dependencies

```bash
cd /path/to/EMBEDDED
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Run

One command (installs missing deps, starts Flask + Next.js):

```bash
./run.sh
```

Or start each service yourself:

```bash
python backend/app.py          # Flask API on :8000
cd nextjs && npm install && npm run dev   # UI on :3000
```

Open <http://localhost:3000>.

## 3. Camera refresh (optional)

Prediction reads the database, so measurements must be imported. Register each
camera plant once, then put the credentials in a `.env` at the project root:

```bash
python plant_feedback.py register --species "Super Orange" --ref Plant-01

cp .env.example .env      # then edit .env
#   CLOUDINARY_CLOUD_NAME=...
#   CLOUDINARY_API_KEY=...
#   CLOUDINARY_API_SECRET=...
#   CAMERA_SOURCES=Plant01_=Plant-01
#   CAMERA_ROD_LENGTH_CM=30
```

`.env` is gitignored and loaded automatically (real environment variables
always win). The backend then pulls/measures/imports new images before a
species prediction. Without these variables the refresh is a safe no-op. See
the README for the full variable list.

## 4. Storage

- Everything lives in `plant_readiness.db` (SQLite). It is gitignored; back it
  up by copying the file.
- The schema is created on first use and evolved by a lightweight
  forward-migration in `plantreadiness/db.py` — an existing database keeps
  working without a reset.
- `real` and `synthetic` rows are always tagged via a `data_source` column.

## 5. Tests

```bash
python -m pytest -q          # or: python -m unittest discover -s tests
```

## 6. Backups & export

- **Backup:** copy `plant_readiness.db` while the backend is stopped.
- **Export:** the CLI can export tables to CSV (`plant_feedback.py`,
  `plantreadiness.services.export_table_csv`).
