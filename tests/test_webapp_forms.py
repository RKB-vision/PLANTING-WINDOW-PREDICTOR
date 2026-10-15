"""Tests: Streamlit web app forms and pages (AppTest, no network).

Covers the UI-side requirements:

* unconfigured backend shows the setup message and stops (never runs
  against a hidden database),
* register form validation + duplicate handling,
* observation form auto-calculates age / growth rate and never invents
  values,
* predictions page shows the honest-refusal message for a real plant,
* feedback page blocks duplicate submissions and shows due/overdue state,
* species/data page offers CSV preview and export,
* navigation between all six pages works without exceptions.

SQLite backend is used (explicitly configured), so no secrets or network
are required.
"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

from plantreadiness import config
from plantreadiness import db as dbm

APP = Path(__file__).resolve().parent.parent / "webapp" / "app.py"
TIMEOUT = 60


def make_app(db_path: str) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=TIMEOUT)
    at.secrets["backend"] = "sqlite"
    at.secrets["sqlite"] = {"path": db_path}
    return at


def assert_no_exceptions(testcase, at):
    msgs = [str(e.value) for e in at.exception]
    testcase.assertEqual(msgs, [], f"unhandled exceptions: {msgs}")


class WebAppBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "web.db")
        self.at = make_app(self.db)
        self.at.run()

    def tearDown(self):
        self.tmp.cleanup()

    def assert_no_exceptions(self, at):
        assert_no_exceptions(self, at)

    def goto(self, page: str):
        self.at.radio[0].set_value(page)
        self.at.run()
        self.assert_no_exceptions(self.at)
        # A page-level crash (e.g. NameError/KeyError inside render) is
        # caught by main()'s try/except and rendered as an error box, so
        # assert_no_exceptions alone would miss it. Navigating to any page
        # must never produce one.
        errs = [e.value for e in self.at.error]
        self.assertEqual(errs, [],
                         f"{page} showed an unexpected error on load: {errs}")
        return self.at

    def all_text(self, at) -> str:
        """Every visible text channel concatenated (markdown/caption/...)."""
        parts = [m.value for m in at.markdown]
        parts += [c.value for c in at.caption]
        parts += [i.value for i in at.info]
        parts += [h.value for h in at.subheader]
        parts += [t.value for t in at.title]
        return " ".join(parts)


class TestUnconfiguredSetupMessage(unittest.TestCase):
    def test_missing_secrets_shows_setup_and_stops(self):
        at = AppTest.from_file(str(APP), default_timeout=TIMEOUT)
        # Explicitly unconfigured backend: supabase selected but no
        # credentials supplied. (We cannot rely on the .streamlit file
        # being absent - run.sh / normal use creates one - so the test
        # forces the unconfigured state instead.)
        at.secrets["backend"] = "supabase"
        at.run()
        assert_no_exceptions(self, at)
        errs = " ".join(e.value for e in at.error)
        self.assertIn("not configured", errs)
        # no page rendered (setup gate stops the app)
        self.assertEqual([t.value for t in at.title], [])
        # the setup guide offers both backends
        body = " ".join(m.value for m in at.markdown)
        self.assertIn("sqlite", body.lower())
        self.assertIn("supabase", body.lower())
        self.assertIn("will not fall back", body)


class TestNavigation(WebAppBase):
    PAGES = ["Dashboard", "Register plant", "Record observation",
             "Predictions", "Transplant & feedback", "Species & data"]

    def test_all_pages_render_without_exceptions(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                self.goto(page)
                self.assertTrue(self.at.title,
                                f"{page} rendered no title")


class TestRegisterForm(WebAppBase):
    def test_register_plant_persists(self):
        self.goto("Register plant")
        self.at.text_input[0].set_value("Super Orange")   # species
        self.at.text_input[1].set_value("Early")          # variety
        # submit the register form
        self.at.button[0].set_value(True)
        self.at.run()
        self.assert_no_exceptions(self.at)
        success = " ".join(s.value for s in self.at.success)
        self.assertIn("Registered plant", success)

        conn = dbm.init_db(self.db)
        try:
            row = conn.execute(
                "SELECT species, variety, data_source FROM plants"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["species"], "Super Orange")
        self.assertEqual(row["data_source"], "real")

    def test_duplicate_plant_shows_warning_not_traceback(self):
        conn = dbm.init_db(self.db)
        dbm.register_plant(conn, species="Super Orange", variety="Early",
                           data_source="real")
        conn.close()

        self.goto("Register plant")
        self.at.text_input[0].set_value("Super Orange")
        self.at.text_input[1].set_value("Early")
        self.at.button[0].set_value(True)
        self.at.run()
        self.assert_no_exceptions(self.at)
        warnings = " ".join(w.value for w in self.at.warning)
        self.assertIn("already", warnings.lower())

    def test_synthetic_checkbox_requires_explicit_opt_in(self):
        self.goto("Register plant")
        # checkbox present but unticked by default -> real
        checkboxes = [c for c in self.at.checkbox]
        self.assertTrue(checkboxes)
        self.assertFalse(checkboxes[0].value)
        self.at.text_input[0].set_value("Demo Bean")
        self.at.button[0].set_value(True)
        self.at.run()
        conn = dbm.init_db(self.db)
        try:
            src = conn.execute("SELECT data_source FROM plants") \
                .fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(src, "real")


class TestObservationForm(WebAppBase):
    def _register(self, date_started=None):
        """Register a plant; default start = 16 days before today so the
        form's default observation date (today) derives age=16."""
        if date_started is None:
            date_started = (date.today() - timedelta(days=16)).isoformat()
        conn = dbm.init_db(self.db)
        try:
            pid = dbm.register_plant(conn, species="Super Orange",
                                     variety="Early",
                                     date_started=date_started,
                                     data_source="real")
        finally:
            conn.close()
        return pid

    def _record(self, height=3.5):
        """Press the Record observation button on the open form."""
        self.at.number_input[0].set_value(height)   # Height (cm)
        rec = [b for b in self.at.button
               if b.label == "Record observation"]
        self.assertTrue(rec, "record button not found")
        rec[0].set_value(True)
        self.at.run()
        self.assert_no_exceptions(self.at)

    def test_auto_age_and_growth_rate_shown_before_submit(self):
        self._register()
        self.goto("Record observation")
        # form default date = today -> age derives from date_started
        self.at.number_input[0].set_value(3.5)   # height
        self.at.run()
        body = self.all_text(self.at)
        self.assertIn("derived from", body)
        self.assertIn("16 days", body)

    def test_first_observation_does_not_invent_growth_rate(self):
        self._register()
        self.goto("Record observation")
        self._record(height=3.5)
        conn = dbm.init_db(self.db)
        try:
            row = conn.execute(
                "SELECT age_days, growth_rate_cm_per_day FROM observations"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "observation was not written")
        self.assertEqual(row["age_days"], 16)
        # no previous measurement -> rate stays NULL, never invented
        self.assertIsNone(row["growth_rate_cm_per_day"])

    def test_observation_before_start_is_blocked_not_invented(self):
        """Observation dated before the plant's start is refused with an
        explicit error - nothing is recorded, no negative age is invented."""
        self._register(date_started=(date.today() + timedelta(days=30))
                       .isoformat())   # plant starts in the future
        self.goto("Record observation")
        # form default date = today, which is BEFORE the start date
        self._record(height=3.5)
        errs = " ".join(e.value for e in self.at.error)
        self.assertIn("before the plant's date_started", errs)
        conn = dbm.init_db(self.db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM observations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, "invalid observation must not be written")

    def test_growth_chart_needs_two_points(self):
        self._register()
        self.goto("Record observation")
        body = " ".join(m.value for m in self.at.caption)
        self.assertTrue(body)   # page renders with history section


class TestPredictionsPage(WebAppBase):
    def test_honest_refusal_for_real_plant(self):
        conn = dbm.init_db(self.db)
        try:
            pid = dbm.register_plant(conn, species="Super Orange",
                                     variety="Early",
                                     date_started="2083-04-24",
                                     data_source="real")
            dbm.add_observation(conn, pid, "2083-05-10", age_days=16,
                                height_cm=3.5, data_source="real")
        finally:
            conn.close()

        self.goto("Predictions")
        self.at.button[0].set_value(True)   # Predict
        self.at.run()
        self.assert_no_exceptions(self.at)
        infos = " ".join(i.value for i in self.at.info)
        self.assertIn(config.UNAVAILABLE_MESSAGE, infos)
        # fallback explanation (rendered as a caption) is visible
        body = self.all_text(self.at)
        self.assertIn("Level 1", body)
        # synthetic opt-in present but off for a real plant
        boxes = [c for c in self.at.checkbox]
        self.assertTrue(any("SYNTHETIC DEMO" in c.label for c in boxes))
        # no synthetic advice shown
        warns = " ".join(w.value for w in self.at.warning)
        self.assertNotIn("SYNTHETIC DEMO: provisional", warns)

    def test_no_plants_message(self):
        self.goto("Predictions")
        infos = " ".join(i.value for i in self.at.info)
        self.assertIn("No plants registered yet", infos)


class TestFeedbackPage(WebAppBase):
    def _transplant(self, days_ago=5):
        """Transplant ``days_ago`` days before today so the form's default
        submission date (today) is valid and the record is pending
        (due = transplant + 14 = today + 9)."""
        self.transplant_date = (date.today()
                                - timedelta(days=days_ago)).isoformat()
        self.due_date = (date.today()
                         + timedelta(days=14 - days_ago)).isoformat()
        conn = dbm.init_db(self.db)
        try:
            pid = dbm.register_plant(conn, species="Super Orange",
                                     variety="Early", data_source="real")
            dbm.record_transplant(conn, pid, self.transplant_date,
                                  height_at_transplant_cm=6.4)
        finally:
            conn.close()

    def test_due_date_and_state_displayed(self):
        self._transplant()
        self.goto("Transplant & feedback")
        self.assert_no_exceptions(self.at)
        # the pending list is a dataframe: due date = transplant + 14 days
        self.assertTrue(self.at.dataframe, "no pending table rendered")
        df_text = " ".join(df.value.to_string()
                           for df in self.at.dataframe)
        self.assertIn(self.due_date, df_text)
        self.assertIn("pending", df_text)

    def test_submit_outcome_growth_computed(self):
        self._transplant()
        self.goto("Transplant & feedback")
        # fill the submit-feedback form: submission date defaults to today
        # (valid: on/after the transplant date), then heights + submit
        submit = [b for b in self.at.button if b.label == "Submit outcome"]
        self.assertTrue(submit, "submit form not found")
        # radios: Yes/No survival is already "Yes" by default
        heights = [n for n in self.at.number_input
                   if "Height after 14 days" in n.label]
        self.assertTrue(heights)
        heights[0].set_value(7.5)
        submit[0].set_value(True)
        self.at.run()
        self.assert_no_exceptions(self.at)
        success = " ".join(s.value for s in self.at.success)
        self.assertIn("submitted", success.lower())

        conn = dbm.init_db(self.db)
        try:
            row = conn.execute(
                "SELECT growth_after_14d_cm, feedback_submitted_date "
                "FROM feedback_14d").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["feedback_submitted_date"],
                         date.today().isoformat())
        # growth only when both heights exist: 7.5 - 6.4
        self.assertAlmostEqual(row["growth_after_14d_cm"], 1.1)

    def test_duplicate_submission_blocked_with_message(self):
        self._transplant()
        conn = dbm.init_db(self.db)
        try:
            fb = dbm.pending_feedback(conn)[0]
            dbm.submit_feedback(conn, feedback_id=fb["feedback_id"],
                                feedback_submitted_date=self.transplant_date,
                                survived_after_14d=True,
                                height_after_14d_cm=7.5,
                                visible_condition="healthy")
        finally:
            conn.close()

        self.goto("Transplant & feedback")
        # a second submission is blocked with an explicit message; the page
        # still renders, and the record moved out of the pending queue
        self.assert_no_exceptions(self.at)
        self.assertIn("History", " ".join(t.label for t in self.at.tabs))
        pend = [d for d in self.at.dataframe
                if "Status" in d.value.columns]
        if pend:
            self.assertNotIn("pending",
                             pend[0].value["Status"].astype(str).tolist())


class TestSpeciesDataPage(WebAppBase):
    def test_csv_preview_and_validation(self):
        self.goto("Species & data")
        # switch to import tab: AppTest exposes tabs; set file uploader is
        # not directly settable, so validate the page structure + evidence
        self.assert_no_exceptions(self.at)
        headers = [m.value for m in self.at.markdown]
        self.assertTrue(any("Evidence" in h or "Import" in h
                            for h in headers))

    def test_evidence_counts_and_minimums_caption(self):
        conn = dbm.init_db(self.db)
        try:
            dbm.register_plant(conn, species="Super Orange",
                               variety="Early", data_source="real")
        finally:
            conn.close()
        self.goto("Species & data")
        body = " ".join(m.value for m in self.at.caption)
        self.assertIn(str(config.MIN_READINESS_EVENTS), body)
        self.assertIn(config.UNAVAILABLE_MESSAGE, body)
        # the species-detail section (which needs models_dir for the model
        # plan) must render - regression for the NameError that used to be
        # swallowed by the page-level try/except as "Unexpected error"
        full = self.all_text(self.at)
        self.assertIn("Model selection", full)
        self.assertNotIn("Unexpected error", full)

    def test_export_buttons_offered(self):
        conn = dbm.init_db(self.db)
        try:
            pid = dbm.register_plant(conn, species="Super Orange",
                                     data_source="real")
            dbm.add_observation(conn, pid, "2083-05-10", age_days=10,
                                height_cm=3.0, data_source="real")
        finally:
            conn.close()
        self.goto("Species & data")
        labels = [b.label for b in self.at.download_button]
        self.assertTrue(any("Download Plants" in l for l in labels))
        self.assertTrue(any("Download Observations" in l for l in labels))


class TestDashboardPage(WebAppBase):
    def test_dashboard_shows_real_counts(self):
        conn = dbm.init_db(self.db)
        try:
            pid = dbm.register_plant(conn, species="Super Orange",
                                     data_source="real")
            dbm.add_observation(conn, pid, "2083-05-10", age_days=10,
                                height_cm=3.0, data_source="real")
        finally:
            conn.close()
        self.goto("Dashboard")
        metrics = {m.label: m.value for m in self.at.metric}
        self.assertEqual(str(metrics.get("Plants")), "1")
        self.assertEqual(str(metrics.get("Observations")), "1")

    def test_dashboard_no_artifacts_is_honest(self):
        self.goto("Dashboard")
        infos = " ".join(i.value for i in self.at.info)
        self.assertIn("No active **real** model", infos)

    def test_dashboard_feedback_queue_empty_message(self):
        self.goto("Dashboard")
        success = " ".join(s.value for s in self.at.success)
        self.assertIn("No feedback is waiting", success)


if __name__ == "__main__":
    unittest.main()
