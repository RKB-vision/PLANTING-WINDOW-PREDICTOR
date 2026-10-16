# Plant-Readiness Next.js Front-end

A minimal **Next.js 14** front-end for the plant transplant-readiness system.
It talks to the Flask backend in `backend/app.py` through proxy API routes.

## Pages

- `/` — species-level dashboard: choose a species, view the latest cloud
  observation cohort, and get a readiness prediction.
- `/feedback` — readiness assessment, transplant record, 14-day follow-up,
  reference baseline, and prediction evaluation.

## Prerequisites

- Node >= 18
- The Flask backend running on `http://localhost:8000`

## Setup

```bash
cd nextjs
npm install
```

## Development

```bash
# terminal 1: backend
python backend/app.py

# terminal 2: front-end
npm run dev            # http://localhost:3000
```

## Production build

```bash
npm run build
npm start
```

## Environment variables

- `PYTHON_BACKEND_URL` — URL of the Flask backend (default `http://localhost:8000`).

## Styling

The design system lives in `styles/globals.css` (neutral surfaces, restrained
green accent, no emoji).
