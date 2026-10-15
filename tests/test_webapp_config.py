"""Tests: web-app configuration, auth session handling, setup messages.

Covers the deployment-safety requirements:

* missing/incomplete secrets produce a *clear setup message* - never a
  silent fallback to a hidden in-memory or synthetic database,
* placeholder secrets from the committed example are rejected,
* only the anon key is used (service-role keys are refused),
* auth session state round-trips and is cleared on sign-out,
* connection failures during client creation are surfaced as
  ConfigurationError.
"""

import unittest
from types import SimpleNamespace

from plantreadiness.repository import ConfigurationError
from webapp import auth
from webapp import settings as app_settings

ANON_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.real-project-secret-"
            "abcdefghijklmnop")


def secrets(**kw):
    return dict(kw)


class TestSettingsUnconfigured(unittest.TestCase):
    def test_empty_secrets_is_not_configured(self):
        cfg = app_settings.load_settings(secrets())
        self.assertFalse(cfg.configured)
        self.assertTrue(cfg.setup_errors)
        self.assertIn("backend", cfg.setup_errors[0])

    def test_no_silent_sqlite_fallback_for_supabase_intent(self):
        """A half-configured Supabase setup must not fall back anywhere."""
        cfg = app_settings.load_settings(secrets(backend="supabase"))
        self.assertFalse(cfg.configured)
        self.assertFalse(cfg.is_sqlite)
        self.assertTrue(any("supabase" in e.lower()
                            for e in cfg.setup_errors))

    def test_unknown_backend_rejected(self):
        cfg = app_settings.load_settings(secrets(backend="postgres"))
        self.assertFalse(cfg.configured)
        self.assertTrue(any("Unknown backend" in e
                            for e in cfg.setup_errors))

    def test_setup_instructions_mention_both_options(self):
        cfg = app_settings.load_settings(secrets())
        text = app_settings.setup_instructions(cfg)
        self.assertIn("Database not configured", text)
        self.assertIn("supabase", text.lower())
        self.assertIn("sqlite", text.lower())
        self.assertIn("will not fall back", text)

    def test_unhashable_or_broken_secrets_object_is_caught(self):
        class Broken:
            def keys(self):
                raise RuntimeError("boom")
        cfg = app_settings.load_settings(Broken())
        self.assertFalse(cfg.configured)
        self.assertTrue(any("Could not read secrets" in e
                            for e in cfg.setup_errors))


class TestSettingsSupabase(unittest.TestCase):
    def test_complete_config_is_configured(self):
        cfg = app_settings.load_settings(secrets(
            backend="supabase",
            supabase={"url": "https://abc.supabase.co",
                      "anon_key": ANON_KEY}))
        self.assertTrue(cfg.configured)
        self.assertTrue(cfg.is_supabase)
        self.assertEqual(cfg.supabase_url, "https://abc.supabase.co")

    def test_missing_url_and_key_reported(self):
        cfg = app_settings.load_settings(secrets(
            backend="supabase", supabase={}))
        self.assertFalse(cfg.configured)
        joined = " ".join(cfg.setup_errors)
        self.assertIn("url", joined)
        self.assertIn("anon_key", joined)
        self.assertIn("service-role", joined)

    def test_placeholder_example_is_rejected(self):
        cfg = app_settings.load_settings(secrets(
            backend="supabase",
            supabase={"url": "https://YOUR_PROJECT_REF.supabase.co",
                      "anon_key": "YOUR_ANON_PUBLIC_KEY"}))
        self.assertFalse(cfg.configured)
        self.assertTrue(any("placeholder" in e.lower()
                            for e in cfg.setup_errors))

    def test_service_role_key_refused(self):
        """A privileged key pasted into secrets must be rejected loudly."""
        cfg = app_settings.load_settings(secrets(
            backend="supabase",
            supabase={"url": "https://abc.supabase.co",
                      "anon_key": "service_role_SECRET_key_value"}))
        self.assertFalse(cfg.configured)
        self.assertTrue(any("service-role" in e
                            for e in cfg.setup_errors))

    def test_missing_supabase_section(self):
        cfg = app_settings.load_settings(secrets(backend="supabase"))
        self.assertFalse(cfg.configured)
        self.assertTrue(any("[supabase] section" in e
                            for e in cfg.setup_errors))


class TestSettingsSqlite(unittest.TestCase):
    def test_sqlite_backend_configured(self):
        cfg = app_settings.load_settings(secrets(backend="sqlite"))
        self.assertTrue(cfg.configured)
        self.assertTrue(cfg.is_sqlite)

    def test_sqlite_path_from_section(self):
        cfg = app_settings.load_settings(secrets(
            backend="sqlite", sqlite={"path": "/tmp/x.db"}))
        self.assertEqual(str(cfg.sqlite_path), "/tmp/x.db")

    def test_sqlite_top_level_path(self):
        cfg = app_settings.load_settings(secrets(
            backend="sqlite", sqlite_path="/tmp/y.db"))
        self.assertEqual(str(cfg.sqlite_path), "/tmp/y.db")

    def test_supabase_client_factory_refuses_sqlite_config(self):
        cfg = app_settings.load_settings(secrets(backend="sqlite"))
        with self.assertRaises(ConfigurationError):
            app_settings.create_supabase_client(cfg)


class TestClientFactoryErrors(unittest.TestCase):
    def test_unconfigured_supabase_raises_configuration_error(self):
        cfg = app_settings.load_settings(secrets())
        with self.assertRaises(ConfigurationError):
            app_settings.create_supabase_client(cfg)

    def test_invalid_url_raises_configuration_error(self):
        cfg = app_settings.load_settings(secrets(
            backend="supabase",
            supabase={"url": "not-a-url", "anon_key": ANON_KEY}))
        # create_client validates the URL scheme and raises
        with self.assertRaises(ConfigurationError) as ctx:
            app_settings.create_supabase_client(cfg)
        self.assertIn("Could not create Supabase client", str(ctx.exception))


class FakeState(dict):
    """dict that mimics streamlit.session_state (pop with default works)."""


class FakeSession:
    def __init__(self, access="tok-a", refresh="ref-a"):
        self.access_token = access
        self.refresh_token = refresh


class FakeUser:
    def __init__(self, uid="user-1", email="a@example.com"):
        self.id = uid
        self.email = email


class FakeAuthResponse:
    def __init__(self, user=None, session=None):
        self.user = user
        self.session = session


class FakeAuthClient:
    def __init__(self, ok=True, fail_refresh=False):
        self.ok = ok
        self.fail_refresh = fail_refresh
        self.signed_out = False

    def sign_in_with_password(self, creds):
        if not self.ok:
            raise RuntimeError("Invalid login credentials")
        return FakeAuthResponse(FakeUser(), FakeSession())

    def sign_up(self, creds):
        return FakeAuthResponse(FakeUser(), FakeSession())

    def set_session(self, access, refresh):
        if self.fail_refresh:
            raise RuntimeError("refresh token invalid")
        return FakeAuthResponse(FakeUser(), FakeSession(access, refresh))

    def sign_out(self):
        self.signed_out = True


class FakePostgrest:
    def __init__(self):
        self.token = None

    def auth(self, token):
        self.token = token


class FakeClient:
    def __init__(self, auth_ok=True, fail_refresh=False):
        self.auth = FakeAuthClient(ok=auth_ok, fail_refresh=fail_refresh)
        self.postgrest = FakePostgrest()


class TestAuthSession(unittest.TestCase):
    def test_sign_in_persists_session_and_applies_token(self):
        client = FakeClient()
        state = FakeState()
        user = auth.sign_in(client, state, "a@example.com", "secret123")
        self.assertEqual(user.user_id, "user-1")
        self.assertEqual(state["sb_user_id"], "user-1")
        self.assertEqual(state["sb_email"], "a@example.com")
        self.assertEqual(client.postgrest.token, "tok-a")
        # password must never be stored
        self.assertNotIn("password", str(dict(state)))

    def test_sign_in_requires_credentials(self):
        with self.assertRaises(ValueError):
            auth.sign_in(FakeClient(), FakeState(), "  ", "x")
        with self.assertRaises(ValueError):
            auth.sign_in(FakeClient(), FakeState(), "a@example.com", "")

    def test_sign_in_failure_propagates(self):
        with self.assertRaises(RuntimeError):
            auth.sign_in(FakeClient(auth_ok=False), FakeState(),
                         "a@example.com", "wrong")

    def test_current_user_none_without_session(self):
        self.assertIsNone(auth.current_user(FakeState()))

    def test_restore_session_returns_user(self):
        client = FakeClient()
        state = FakeState()
        auth.sign_in(client, state, "a@example.com", "pw")
        restored = auth.restore_session(client, state)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.user_id, "user-1")
        self.assertEqual(client.postgrest.token, "tok-a")

    def test_restore_clears_state_when_refresh_fails(self):
        client = FakeClient(fail_refresh=True)
        state = FakeState()
        auth.sign_in(client, state, "a@example.com", "pw")
        restored = auth.restore_session(client, state)
        self.assertIsNone(restored)
        self.assertIsNone(auth.current_user(state))
        self.assertNotIn("sb_access_token", state)

    def test_sign_out_clears_every_key(self):
        client = FakeClient()
        state = FakeState()
        auth.sign_in(client, state, "a@example.com", "pw")
        auth.sign_out(client, state)
        self.assertTrue(client.auth.signed_out)
        self.assertIsNone(auth.current_user(state))
        for key in auth.SESSION_KEYS:
            self.assertNotIn(key, state)

    def test_sign_up_short_password_rejected(self):
        with self.assertRaises(ValueError):
            auth.sign_up(FakeClient(), FakeState(), "a@example.com", "short")

    def test_sign_up_without_session_means_confirm_first(self):
        client = FakeClient()

        def no_session(creds):
            return FakeAuthResponse(FakeUser(), None)
        client.auth.sign_up = no_session
        created = auth.sign_up(client, FakeState(), "a@example.com",
                               "longenough")
        self.assertEqual(created.access_token, "")
        # no session persisted -> user must confirm email first
        self.assertIsNone(auth.current_user(FakeState()))


if __name__ == "__main__":
    unittest.main()
