"""Configuration + repository factory for the web app.

Secrets come only from Streamlit's server-side secrets
(``.streamlit/secrets.toml`` locally, the Secrets UI on Streamlit Community
Cloud). They are never hard-coded, logged or committed - the committed
``secrets.toml.example`` contains placeholders only.

Two backends are supported and chosen *explicitly*:

* ``backend = "supabase"`` - production: Supabase Auth + PostgREST with
  Row Level Security,
* ``backend = "sqlite"``   - local development / tests against the same
  service layer.

If no backend is configured the app shows a setup message and stops. It
never silently falls back to an in-memory or synthetic database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from plantreadiness import config as prconfig
from plantreadiness import db as dbm
from plantreadiness.repository import (BaseRepository, ConfigurationError,
                                       SQLiteRepository,
                                       SupabaseRepository)

# Placeholder detection: the committed example must never work as-is.
_PLACEHOLDER_MARKERS = ("YOUR_", "CHANGEME", "xxxx", "replace-me",
                        "placeholder")

# Supabase privileged-key markers: never acceptable in this app.
_SERVICE_ROLE_MARKERS = ("service_role", "service-role")


def _jwt_role(key: str) -> Optional[str]:
    """Decode (unverified) the role claim of a Supabase JWT key.

    Both anon and service-role keys are JWTs; only the ``role`` claim tells
    them apart. Returns ``None`` when the key is not a parseable JWT.
    """
    import base64
    import json
    parts = key.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # fix missing base64 padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    role = claims.get("role")
    return str(role) if role else None


def privileged_key_reason(key: str) -> Optional[str]:
    """Return a reason string when ``key`` must not be used, else ``None``.

    The app only ever uses the **anon/public** key so Row Level Security is
    enforced. A service-role key (which bypasses RLS) is refused loudly.
    """
    low = key.lower()
    if any(marker in low for marker in _SERVICE_ROLE_MARKERS):
        return ("this looks like a SERVICE-ROLE key. The service-role key "
                "bypasses Row Level Security - never put it in the app. "
                "Use the anon/public key.")
    role = _jwt_role(key)
    if role and role not in ("anon", "authenticated", "public"):
        return (f"this key carries the JWT role '{role}'. Only the "
                "'anon' public key may be used here - a privileged key "
                "would bypass Row Level Security.")
    return None


@dataclass
class AppSettings:
    """Parsed, validated application settings."""

    backend: Optional[str] = None
    supabase_url: Optional[str] = None
    supabase_anon_key: Optional[str] = None
    sqlite_path: Path = prconfig.DEFAULT_DB_PATH
    models_dir: Path = prconfig.MODELS_DIR
    setup_errors: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return self.backend is not None and not self.setup_errors

    @property
    def is_supabase(self) -> bool:
        return self.backend == "supabase"

    @property
    def is_sqlite(self) -> bool:
        return self.backend == "sqlite"


def _looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    return any(marker.lower() in low for marker in _PLACEHOLDER_MARKERS)


def load_settings(secrets: Any) -> AppSettings:
    """Parse a mapping (normally ``st.secrets``) into :class:`AppSettings`.

    Never raises: problems are collected in ``setup_errors`` so the UI can
    render actionable setup help instead of a stack trace.
    """
    settings = AppSettings()
    try:
        data = dict(secrets)
    except Exception as exc:
        settings.setup_errors.append(
            f"Could not read secrets: {exc}. Copy "
            ".streamlit/secrets.toml.example to "
            ".streamlit/secrets.toml and fill it in.")
        return settings

    backend = data.get("backend")
    if backend is None:
        settings.setup_errors.append(
            "No backend configured. Set `backend = \"supabase\"` (production) "
            "or `backend = \"sqlite\"` (local development) in "
            ".streamlit/secrets.toml. The app will not guess a database.")
        return settings
    backend = str(backend).strip().lower()
    if backend not in ("supabase", "sqlite"):
        settings.setup_errors.append(
            f"Unknown backend '{backend}'. Use \"supabase\" or \"sqlite\".")
        return settings
    settings.backend = backend

    if backend == "sqlite":
        # Accept either a top-level sqlite_path or the [sqlite] section
        # shown in secrets.toml.example. NOTE: Streamlit wraps nested
        # secret sections in an AttrDict (a Mapping, not a dict), so the
        # check must use collections.abc.Mapping.
        sqlite_section = data.get("sqlite")
        db_path = data.get("sqlite_path")
        if db_path is None and isinstance(sqlite_section, Mapping):
            db_path = sqlite_section.get("path")
        if db_path:
            settings.sqlite_path = Path(str(db_path))
        models_dir = data.get("models_dir")
        if models_dir:
            settings.models_dir = Path(str(models_dir))
        return settings

    # -- supabase ---------------------------------------------------------- #
    section = data.get("supabase")
    if section is None:
        settings.setup_errors.append(
            "backend = \"supabase\" but no [supabase] section found. Add "
            "url and anon_key (the *anon/public* key - never the service-role "
            "key) to .streamlit/secrets.toml.")
        return settings
    try:
        section = dict(section)
    except Exception:
        settings.setup_errors.append(
            "[supabase] section could not be read as key/value pairs.")
        return settings

    url = str(section.get("url") or "").strip()
    anon_key = str(section.get("anon_key") or "").strip()
    if not url:
        settings.setup_errors.append(
            "[supabase] url is missing - your project URL looks like "
            "https://<project-ref>.supabase.co")
    if not anon_key:
        settings.setup_errors.append(
            "[supabase] anon_key is missing - copy the anon/public key from "
            "Supabase Project Settings > API. Do NOT use the service-role "
            "key here.")
    if anon_key:
        privileged = privileged_key_reason(anon_key)
        if privileged:
            settings.setup_errors.append(f"[supabase] anon_key rejected: "
                                         f"{privileged}")
    if anon_key and _looks_like_placeholder(anon_key):
        settings.setup_errors.append(
            "[supabase] anon_key still looks like the placeholder from "
            "secrets.toml.example - replace it with the real anon key.")
    if url and _looks_like_placeholder(url):
        settings.setup_errors.append(
            "[supabase] url still looks like a placeholder - replace it with "
            "your project URL.")

    settings.supabase_url = url or None
    settings.supabase_anon_key = anon_key or None
    models_dir = section.get("models_dir") or data.get("models_dir")
    if models_dir:
        settings.models_dir = Path(str(models_dir))
    return settings


# --------------------------------------------------------------------------- #
# Client + repository factories
# --------------------------------------------------------------------------- #


def create_supabase_client(settings: AppSettings) -> Any:
    """Create the Supabase client (anon key only)."""
    if not settings.configured or not settings.is_supabase:
        raise ConfigurationError(
            "Supabase backend is not configured - see the setup guide.")
    try:
        from supabase import create_client
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise ConfigurationError(
            "The 'supabase' package is not installed. Run "
            "`pip install -r requirements.txt`.") from exc
    try:
        return create_client(settings.supabase_url,
                             settings.supabase_anon_key)
    except Exception as exc:
        raise ConfigurationError(
            f"Could not create Supabase client: {exc}") from exc


def sqlite_repository(path: Path) -> SQLiteRepository:
    """Open (and initialise) the local development database."""
    conn = dbm.init_db(Path(path))
    return SQLiteRepository(conn)


def supabase_repository(client: Any, user_id: str) -> SupabaseRepository:
    """Scope a repository to the signed-in user's JWT (RLS identity)."""
    _apply_session_token(client, user_id)
    return SupabaseRepository(client, user_id)


def _apply_session_token(client: Any, token_or_user_id: str) -> None:
    """No-op placeholder kept for clarity: token application lives in auth.

    The auth module sets ``client.postgrest.auth(access_token)`` after
    sign-in; repositories only need the user id. This function exists so
    the factory's contract is explicit and testable.
    """
    return None


def setup_instructions(settings: AppSettings) -> str:
    """Human-readable setup help shown instead of running unconfigured."""
    lines = ["### Database not configured", ""]
    if settings.setup_errors:
        lines.append("**What needs fixing:**")
        lines.extend(f"- {e}" for e in settings.setup_errors)
        lines.append("")
    lines += [
        "The app will not fall back to a hidden in-memory or synthetic "
        "database. Configure one of the two supported backends:",
        "",
        "**Option A - Supabase (production)**",
        "1. Create a Supabase project.",
        "2. Run the migrations in `supabase/migrations/` (SQL editor or "
        "`supabase db push`).",
        "3. Enable Email auth and create two test accounts.",
        "4. Copy `.streamlit/secrets.toml.example` to "
        "`.streamlit/secrets.toml`, set `backend = \"supabase\"`, your "
        "project URL and the **anon/public** key.",
        "5. Verify isolation: `python scripts/verify_rls.py ...`",
        "",
        "**Option B - SQLite (local development)**",
        "1. Copy `.streamlit/secrets.toml.example` to "
        "`.streamlit/secrets.toml`.",
        "2. Set `backend = \"sqlite\"` (and optionally `sqlite_path`).",
        "",
        "`.streamlit/secrets.toml` is gitignored - secrets never enter the "
        "repository.",
    ]
    return "\n".join(lines)
