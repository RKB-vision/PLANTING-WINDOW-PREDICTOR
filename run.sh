#!/usr/bin/env bash
export PYTHONPATH=$(pwd)
set -e

# Load local secrets (.env) if present, so both services see them.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# ------------------------------------------------------------
# Ensure required dependencies are installed before starting.
# ------------------------------------------------------------
# 1️⃣ Python backend – Flask
if ! pip show flask >/dev/null 2>&1; then
  echo "Installing Flask in the current virtual‑env…"
  pip install flask
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
flask run --host=0.0.0.0 --port=8000 &
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
