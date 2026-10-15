#!/usr/bin/env bash
# =============================================================================
# run.sh - one command to bring the whole Plant Readiness system up.
#
#   ./run.sh            (default) set up everything, then start the WEB APP
#   ./run.sh demo       train the labelled SYNTHETIC DEMO models, then start
#                       the web app - the Predictions page can then use them
#   ./run.sh test       run the full automated test suite (211 tests)
#   ./run.sh cli ...    use the CLI, e.g. ./run.sh cli list
#   ./run.sh help       show usage
#
# Knobs (environment variables):
#   PORT=8502     serve the web app on a different port (default 8501)
#   HEADLESS=1    do not auto-open the browser (servers / SSH sessions)
#
# What the script does (idempotent - safe to re-run at any time):
#   1. creates .venv if missing and installs requirements.txt whenever the
#      web stack is not yet importable (first run only),
#   2. creates .streamlit/secrets.toml with backend = "sqlite" ONLY when no
#      config exists yet - an existing (e.g. Supabase) config is never
#      overwritten,
#   3. initialises the SQLite prototype database (plant_readiness.db),
#   4. runs the command selected above.
#
# The CLI (prediction.py / plant_feedback.py) keeps working unchanged; the
# CLI and the web app call the same plantreadiness.services functions.
# For Supabase setup, migrations, auth, RLS verification and backups see
# DEPLOYMENT.md.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"

usage() {
    sed -n '2,29p' run.sh | sed 's/^# \{0,1\}//'
}

# -- 0. help (before any setup side effects) -------------------------------
case "${1:-web}" in
    -h|--help|help) usage; exit 0 ;;
esac

# -- 1. Python + virtualenv + dependencies ---------------------------------
if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY="$(command -v python3 || true)"
    if [ -z "$PY" ]; then
        echo "ERROR: python3 not found - install Python 3.10+ first." >&2
        exit 1
    fi
    echo "Creating virtual environment in .venv ..."
    "$PY" -m venv .venv
    PY=.venv/bin/python
fi

# Install requirements only when something is missing (first run).
if ! "$PY" -c "import streamlit, supabase, pandas, numpy, sklearn, joblib" \
        2>/dev/null; then
    echo "Installing dependencies from requirements.txt (first run only) ..."
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install -r requirements.txt
fi

# -- 2. Web app configuration (never clobber an existing file) -------------
if [ ! -f .streamlit/secrets.toml ]; then
    mkdir -p .streamlit
    cat > .streamlit/secrets.toml <<'TOML'
# Created automatically by run.sh for LOCAL development.
#   * backend "sqlite" = single-user, no auth, uses plant_readiness.db.
#   * To use Supabase (multi-user + Row Level Security), replace the content
#     with the [supabase] section from .streamlit/secrets.toml.example and
#     follow DEPLOYMENT.md. This file is gitignored - never commit it.
#   * The app refuses service-role keys by design; use the anon/public key.
backend = "sqlite"

[sqlite]
path = "./plant_readiness.db"
TOML
    echo "Created .streamlit/secrets.toml with backend = \"sqlite\" (local dev)."
    echo "  -> Edit it any time to switch to Supabase - see DEPLOYMENT.md."
fi

# -- 3. Initialise the SQLite prototype database ---------------------------
"$PY" - <<'PY'
from plantreadiness import config as prconfig
from plantreadiness import db as dbm

conn = dbm.init_db(str(prconfig.DEFAULT_DB_PATH))
conn.close()
print(f"SQLite database ready: {prconfig.DEFAULT_DB_PATH}")
PY

# -- 4. Dispatch -----------------------------------------------------------
start_web() {
    local port="${PORT:-8501}"
    local args=(run webapp/app.py --server.port "$port")
    if [ "${HEADLESS:-0}" = "1" ]; then
        args+=(--server.headless true)
    fi
    echo
    echo "Starting the Plant Readiness web app ..."
    echo "  URL:   http://localhost:$port"
    echo "  Pages: Dashboard - Register - Observe - Predict - Feedback - Data"
    echo "  Stop:  Ctrl+C   (or 'q' in the browser window)"
    echo
    exec "$PY" -m streamlit "${args[@]}"
}

case "${1:-web}" in
    web)
        start_web
        ;;
    demo)
        echo
        echo "Training SYNTHETIC DEMO models (labelled 'SYNTHETIC DEMO' in the UI) ..."
        "$PY" model_train.py --mode demo
        echo
        start_web
        ;;
    test)
        exec "$PY" -m unittest discover -s tests
        ;;
    cli)
        shift
        exec "$PY" plant_feedback.py "$@"
        ;;
    *)
        echo "Unknown command: $1" >&2
        echo >&2
        usage >&2
        exit 1
        ;;
esac
