#!/usr/bin/env bash
# run.sh — Launch the Plant Readiness Streamlit dashboard
set -e

# Load local secrets (.env) if present
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

export PYTHONPATH=$(pwd)

# Install dependencies if needed
if ! python -c "import streamlit" >/dev/null 2>&1; then
  echo "Installing dependencies..."
  pip install -r requirements.txt
fi

# Create data/generated if missing
mkdir -p data/generated

# Start Streamlit
echo "Starting Plant Readiness dashboard on http://localhost:8501"
streamlit run app.py --server.port 8502