"""Supabase Auth integration for the Streamlit app.

Session state holds only what is needed to restore the user's JWT between
reruns - never the password. The access token is applied to the PostgREST
client so every query carries the user's identity and RLS scopes it.

No service-role key is used anywhere: the app always operates as a signed-in
user, exactly like the browser client would.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from plantreadiness.repository import ConfigurationError

SESSION_KEYS = ("sb_user_id", "sb_email", "sb_access_token",
                "sb_refresh_token")


@dataclass
class AuthUser:
    user_id: str
    email: str
    access_token: str
    refresh_token: str


def current_user(state: Any) -> Optional[AuthUser]:
    """Return the signed-in user from Streamlit session state, if any."""
    user_id = state.get("sb_user_id")
    token = state.get("sb_access_token")
    if not user_id or not token:
        return None
    return AuthUser(user_id=str(user_id),
                    email=str(state.get("sb_email") or ""),
                    access_token=str(token),
                    refresh_token=str(state.get("sb_refresh_token") or ""))


def sign_in(client: Any, state: Any, email: str, password: str) -> AuthUser:
    """Sign in and persist the session in ``state``.

    Raises whatever the Supabase client raises (bad credentials, network
    failure) - the UI turns it into a user-facing message.
    """
    if not email.strip() or not password:
        raise ValueError("Email and password are required.")
    resp = client.auth.sign_in_with_password(
        {"email": email.strip(), "password": password})
    user = getattr(resp, "user", None)
    session = getattr(resp, "session", None)
    if user is None or session is None:
        raise ConfigurationError(
            "Sign-in returned no session - check email confirmation rules "
            "in Supabase (Authentication > Providers > Email).")
    _persist(state, str(user.id), getattr(user, "email", email),
             session.access_token, session.refresh_token)
    _apply_token(client, session.access_token)
    return AuthUser(user_id=str(user.id),
                    email=getattr(user, "email", email),
                    access_token=session.access_token,
                    refresh_token=session.refresh_token)


def sign_up(client: Any, state: Any, email: str, password: str) -> AuthUser:
    """Create an account. If email confirmation is on, the returned session
    may be None - the caller then asks the user to confirm first."""
    if not email.strip() or len(password) < 8:
        raise ValueError("Email and a password of at least 8 characters "
                         "are required.")
    resp = client.auth.sign_up({"email": email.strip(), "password": password})
    user = getattr(resp, "user", None)
    session = getattr(resp, "session", None)
    if user is None:
        raise ConfigurationError(
            "Sign-up returned no user - the project may have sign-ups "
            "disabled.")
    if session is None:
        # Email confirmation required: do NOT create a session.
        return AuthUser(user_id=str(user.id),
                        email=getattr(user, "email", email),
                        access_token="", refresh_token="")
    _persist(state, str(user.id), getattr(user, "email", email),
             session.access_token, session.refresh_token)
    _apply_token(client, session.access_token)
    return AuthUser(user_id=str(user.id),
                    email=getattr(user, "email", email),
                    access_token=session.access_token,
                    refresh_token=session.refresh_token)


def restore_session(client: Any, state: Any) -> Optional[AuthUser]:
    """Restore the previous session on rerun; refresh if expired.

    Returns ``None`` when there is nothing to restore (or the refresh
    fails), in which case the UI shows the sign-in form.
    """
    user = current_user(state)
    if user is None:
        return None
    if user.access_token:
        _apply_token(client, user.access_token)
    if user.refresh_token:
        try:
            resp = client.auth.set_session(user.access_token,
                                           user.refresh_token)
            session = getattr(resp, "session", None)
            if session is not None:
                _persist(state, user.user_id, user.email,
                         session.access_token, session.refresh_token)
                _apply_token(client, session.access_token)
                return AuthUser(user_id=user.user_id, email=user.email,
                                access_token=session.access_token,
                                refresh_token=session.refresh_token)
        except Exception:
            # Refresh failed (revoked/expired): force a fresh sign-in.
            sign_out(client, state)
            return None
    return user


def sign_out(client: Any, state: Any) -> None:
    """Sign out and clear every session key."""
    try:
        client.auth.sign_out()
    except Exception:
        pass
    for key in SESSION_KEYS:
        state.pop(key, None)


def _persist(state: Any, user_id: str, email: str, access: str,
             refresh: str) -> None:
    state["sb_user_id"] = user_id
    state["sb_email"] = email
    state["sb_access_token"] = access
    state["sb_refresh_token"] = refresh


def _apply_token(client: Any, token: str) -> None:
    """Attach the JWT to PostgREST so RLS sees this user's identity."""
    if not token:
        return
    postgrest = getattr(client, "postgrest", None)
    if postgrest is not None and hasattr(postgrest, "auth"):
        try:
            postgrest.auth(token)
        except Exception:  # pragma: no cover - client-version differences
            pass
