#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Load local secrets (.env) if present, so both services see them.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# ------------------------------------------------------------
# Select and validate the backend interpreter. Prefer the project venv so
# `flask run` and lazy camera imports use the same installed dependencies.
# ------------------------------------------------------------
APP_PYTHON="${APP_PYTHON:-$ROOT_DIR/.venv/bin/python}"
if [ ! -x "$APP_PYTHON" ]; then
  APP_PYTHON="$(command -v python3 || true)"
fi
if [ -z "$APP_PYTHON" ] || ! "$APP_PYTHON" -c \
  'import cv2, flask, numpy, onnxruntime, rembg' >/dev/null 2>&1; then
  echo "Python backend dependencies are missing. Set up the project environment with:" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

# 2️⃣ Node/Next.js dependencies
if [ ! -d "nextjs/node_modules" ]; then
  echo "Installing Node dependencies for the Next.js front‑end…"
  (cd nextjs && npm install)
fi

# ------------------------------------------------------------
# Start the Python backend (Flask) in the background
# ------------------------------------------------------------
export FLASK_APP=backend/app.py
"$APP_PYTHON" -m flask run --host=0.0.0.0 --port=8000 &
PY_PID=$!

# Give the backend a moment to start
sleep 2

# ------------------------------------------------------------
# Start the Next.js development server (foreground)
# ------------------------------------------------------------
cd nextjs
npm run dev

# When the Next.js dev server exits, stop the backend
kill $PY_PID
