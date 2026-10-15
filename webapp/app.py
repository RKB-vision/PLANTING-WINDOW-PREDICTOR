"""Streamlit entry point.

    streamlit run webapp/app.py

Flow:
1. load settings from server-side secrets (missing -> setup message, stop),
2. Supabase backend -> require sign-in (Auth) and scope every query to the
   signed-in user; SQLite backend -> local development mode,
3. sidebar navigation across the six pages.

The UI never embeds SQL: pages call :mod:`plantreadiness.services` through
a repository, exactly like the CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Allow `streamlit run webapp/app.py` from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plantreadiness.repository import ConfigurationError  # noqa: E402
from webapp import auth  # noqa: E402
from webapp import components as ui  # noqa: E402
from webapp import settings as app_settings  # noqa: E402
# NOTE: page modules live in app_pages/, NOT pages/. A sibling pages/
# directory would be auto-discovered by Streamlit's legacy multipage
# system and shown as a second, broken navigation list in the sidebar
# (those files only define render(), so clicking them shows a blank page).
from webapp.app_pages import (dashboard, feedback, observation,  # noqa: E402
                              predictions, register, species_data)

st.set_page_config(page_title="Plant Readiness", page_icon="🌱",
                   layout="wide")

PAGES = {
    "Dashboard": dashboard,
    "Register plant": register,
    "Record observation": observation,
    "Predictions": predictions,
    "Transplant & feedback": feedback,
    "Species & data": species_data,
}


def _show_setup(cfg: app_settings.AppSettings) -> None:
    st.error("**App not started:** the database backend is not configured.")
    st.markdown(app_settings.setup_instructions(cfg))


def _sign_in_form(client) -> bool:
    """Render sign-in / sign-up; return True when a session exists."""
    user = auth.current_user(st.session_state)
    if user is not None:
        # make sure this rerun carries the JWT
        auth.restore_session(client, st.session_state)
        return auth.current_user(st.session_state) is not None

    st.title(":material/login: Sign in")
    st.caption("Each account only ever sees its own plants - enforced by "
               "Postgres Row Level Security, not by the UI.")
    mode = st.radio("Mode", ["Sign in", "Create account"], horizontal=True)
    email = st.text_input("Email")
    password = st.text_input("Password", type="password")
    if st.button(mode, type="primary"):
        try:
            if mode == "Sign in":
                auth.sign_in(client, st.session_state, email, password)
            else:
                created = auth.sign_up(client, st.session_state, email,
                                       password)
                if not created.access_token:
                    st.info("Account created. Confirm the email link, then "
                            "sign in.")
                    return False
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")
            return False
        st.rerun()
    return False


def main() -> None:
    cfg = app_settings.load_settings(st.secrets)
    if not cfg.configured:
        _show_setup(cfg)
        st.stop()

    repo = None
    if cfg.is_supabase:
        try:
            client = app_settings.create_supabase_client(cfg)
        except ConfigurationError as exc:
            st.error(f"**Supabase client could not be created:** {exc}")
            st.stop()
        if not _sign_in_form(client):
            st.stop()
        user = auth.current_user(st.session_state)
        if user is None:
            st.stop()
        try:
            repo = app_settings.supabase_repository(client, user.user_id)
        except ConfigurationError as exc:
            st.error(str(exc))
            st.stop()
        account_label = user.email or user.user_id
    else:
        try:
            repo = app_settings.sqlite_repository(cfg.sqlite_path)
        except Exception as exc:
            st.error(f"**Could not open the SQLite database:** {exc}")
            st.stop()
        client = None
        account_label = "local development"

    # ---- sidebar --------------------------------------------------------- #
    with st.sidebar:
        st.markdown("🌱 **Plant Readiness**")
        st.caption(f"Backend: `{repo.backend}` · {account_label}")
        if cfg.is_supabase:
            if st.button("Sign out", width="stretch"):
                auth.sign_out(client, st.session_state)
                st.rerun()
        else:
            st.caption(":orange[Local development mode - no auth. Use the "
                       "Supabase backend for multi-user isolation.]")
        st.divider()
        choice = st.radio("Navigation", list(PAGES.keys()),
                          label_visibility="collapsed")

    try:
        PAGES[choice].render(repo, cfg.models_dir)
    except Exception as exc:  # domain errors -> friendly box, not a trace
        ui.error_box(exc)
    finally:
        try:
            repo.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
