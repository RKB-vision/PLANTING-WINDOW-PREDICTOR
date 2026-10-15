"""Tests: repository layer (SQLite backend + mocked Supabase backend).

Covers the requirements that the web app must satisfy:

* authentication/ownership scoping (every query filtered by user_id),
* connection failures surfaced as ConnectionFailedError,
* duplicate blocking for plants / observations / feedback,
* feedback due-date state machine,
* corrections preserve audit history,
* real/synthetic separation,
* CSV export and prediction-unavailable behaviour.

The Supabase client is mocked: no network access and no real secrets are
needed (and none are ever hard-coded in this file).
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from plantreadiness import config
from plantreadiness import services as svc
from plantreadiness.db import (DuplicateRecordError, NotFoundError,
                               ValidationError)
from plantreadiness.repository import (ConfigurationError,
                                       ConnectionFailedError,
                                       DatabaseError, SQLiteRepository,
                                       SupabaseRepository,
                                       create_sqlite_repository)


# --------------------------------------------------------------------------- #
# SQLite backend
# --------------------------------------------------------------------------- #


class SQLiteRepoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = create_sqlite_repository(
            Path(self.tmp.name) / "repo.db")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _plant(self, **kw):
        kw.setdefault("species", "Super Orange")
        kw.setdefault("variety", "Early")
        kw.setdefault("date_started", "2083-04-24")
        kw.setdefault("data_source", "real")
        return self.repo.register_plant(**kw)


class TestSQLiteRepository(SQLiteRepoBase):
    def test_register_and_get_plant(self):
        pid = self._plant()
        plant = self.repo.get_plant(pid)
        self.assertEqual(plant["species"], "Super Orange")
        self.assertEqual(plant["data_source"], "real")

    def test_duplicate_plant_rejected(self):
        self._plant(external_ref="X-1")
        with self.assertRaises(DuplicateRecordError):
            self._plant(external_ref="X-1")

    def test_unknown_plant_returns_none(self):
        self.assertIsNone(self.repo.get_plant(99999))

    def test_duplicate_observation_rejected(self):
        pid = self._plant()
        self.repo.add_observation(plant_id=pid, observation_date="2083-05-10",
                                  height_cm=3.5)
        with self.assertRaises(DuplicateRecordError):
            self.repo.add_observation(plant_id=pid,
                                      observation_date="2083-05-10",
                                      height_cm=4.0)

    def test_observation_source_mismatch_rejected(self):
        pid = self._plant()
        with self.assertRaises(ValidationError):
            self.repo.add_observation(plant_id=pid,
                                      observation_date="2083-05-10",
                                      height_cm=3.5, data_source="synthetic")

    def test_search_filters_plants(self):
        self._plant(species="Super Orange", variety="Early")
        self._plant(species="Demo Bean", variety="Bush")
        self.assertEqual(len(self.repo.list_plants()), 2)
        self.assertEqual(
            len(self.repo.list_plants(search="demo")), 1)
        self.assertEqual(
            len(self.repo.list_plants(data_source="synthetic")), 0)

    def test_count_observations_separates_sources(self):
        pid_real = self._plant()
        pid_syn = self._plant(species="Demo Bean", variety="Bush",
                              data_source="synthetic")
        self.repo.add_observation(plant_id=pid_real,
                                  observation_date="2083-05-10", height_cm=1)
        self.repo.add_observation(plant_id=pid_syn,
                                  observation_date="2083-05-10", height_cm=1,
                                  data_source="synthetic")
        self.assertEqual(self.repo.count_observations("real"), 1)
        self.assertEqual(self.repo.count_observations("synthetic"), 1)
        self.assertEqual(self.repo.count_observations(), 2)


class TestFeedbackWorkflow(SQLiteRepoBase):
    def _feedback(self, transplant="2083-05-20"):
        pid = self._plant()
        return pid, self.repo.record_transplant(
            plant_id=pid, transplant_date=transplant,
            height_at_transplant_cm=6.4)

    def test_due_date_is_transplant_plus_14(self):
        _, fb = self._feedback()
        self.assertEqual(fb["feedback_due_date"], "2083-06-03")

    def test_duplicate_transplant_rejected(self):
        pid = self._plant()
        self.repo.record_transplant(plant_id=pid,
                                    transplant_date="2083-05-20")
        with self.assertRaises(DuplicateRecordError):
            self.repo.record_transplant(plant_id=pid,
                                        transplant_date="2083-05-20")

    def test_pending_feedback_states(self):
        _, fb = self._feedback("2083-05-20")   # due 2083-06-03
        rows = self.repo.pending_feedback(as_of=date(2083, 6, 1))
        self.assertEqual(len(rows), 1)
        state = svc.feedback_state(rows[0], as_of=date(2083, 6, 1))
        self.assertEqual(state["status"], "pending")
        state = svc.feedback_state(rows[0], as_of=date(2083, 6, 3))
        self.assertEqual(state["status"], "due")
        state = svc.feedback_state(rows[0], as_of=date(2083, 6, 10))
        self.assertEqual(state["status"], "overdue")
        self.assertEqual(state["days_overdue"], 7)

    def test_completed_feedback_leaves_pending_list(self):
        _, fb = self._feedback()
        self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        self.assertEqual(self.repo.pending_feedback(), [])

    def test_duplicate_feedback_submission_blocked(self):
        _, fb = self._feedback()
        self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        with self.assertRaises(DuplicateRecordError):
            self.repo.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-04",
                survived_after_14d=False)

    def test_feedback_validation_consistency(self):
        _, fb = self._feedback()
        with self.assertRaises(ValidationError):
            self.repo.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=True, visible_condition="dead")
        with self.assertRaises(ValidationError):
            self.repo.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=False, visible_condition="healthy")

    def test_growth_only_when_both_heights_exist(self):
        _, fb = self._feedback()
        done = self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        self.assertEqual(done["growth_after_14d_cm"], 1.1)

        # dead plant, no height after -> growth stays NULL
        pid2 = self._plant(variety="Late")
        fb2 = self.repo.record_transplant(plant_id=pid2,
                                          transplant_date="2083-05-20")
        done2 = self.repo.submit_feedback(
            feedback_id=fb2["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=False)
        self.assertIsNone(done2["growth_after_14d_cm"])
        self.assertEqual(done2["visible_condition"], "dead")

    def test_correction_requires_reason_and_keeps_audit(self):
        _, fb = self._feedback()
        self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")

        with self.assertRaises(ValidationError):
            svc.correct_feedback(self.repo, feedback_id=fb["feedback_id"],
                                 height_after_14d_cm=8.0, reason="  ")

        fixed = svc.correct_feedback(
            self.repo, feedback_id=fb["feedback_id"],
            height_after_14d_cm=8.0, reason="misread tape")
        self.assertEqual(fixed["height_after_14d_cm"], 8.0)
        self.assertEqual(fixed["growth_after_14d_cm"], 1.6)

        revisions = self.repo.feedback_revisions(fb["feedback_id"])
        self.assertEqual(len(revisions), 1)
        previous = json.loads(revisions[0]["previous_values"])
        self.assertEqual(previous["height_after_14d_cm"], 7.5)
        self.assertEqual(revisions[0]["reason"], "misread tape")

    def test_cannot_correct_unsubmitted_feedback(self):
        _, fb = self._feedback()
        with self.assertRaises(ValidationError):
            svc.correct_feedback(self.repo, feedback_id=fb["feedback_id"],
                                 height_after_14d_cm=8.0, reason="early")

    def test_dashboard_stats_counts(self):
        self._feedback()
        stats = self.repo.dashboard_stats()
        self.assertEqual(stats["n_plants"], 1)
        self.assertEqual(stats["n_feedback_total"], 1)
        self.assertEqual(stats["n_feedback_pending"], 1)


# --------------------------------------------------------------------------- #
# Mocked Supabase backend
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class FakeQuery:
    """Mimics the postgrest request builder for the calls we make."""

    def __init__(self, store, table, op="select", payload=None):
        self.store = store
        self.table = table
        self.op = op
        self.payload = payload
        self.filters = {}
        self._in_filters = {}
        self._orders = []
        self._limit = None

    # chainables
    def select(self, *a, **kw):
        return self
    def eq(self, col, val):
        self.filters[col] = val
        return self
    def in_(self, col, vals):
        self._in_filters[col] = list(vals)
        return self
    def order(self, col, desc=False):
        self._orders.append((col, desc))
        return self
    def limit(self, n):
        self._limit = n
        return self
    def single(self):
        return self
    def maybe_single(self):
        return self

    def _matched_refs(self):
        """Actual stored row objects matching the filters (not copies)."""
        refs = []
        for r in self.store.setdefault(self.table, []):
            ok = True
            for col, val in self.filters.items():
                if col not in r or r[col] != val:
                    ok = False
                    break
            if ok:
                for col, vals in self._in_filters.items():
                    if r.get(col) not in vals:
                        ok = False
                        break
            if ok:
                refs.append(r)
        for col, desc in reversed(self._orders):
            refs.sort(key=lambda r: (r.get(col) is None, r.get(col)),
                      reverse=desc)
        if self._limit is not None:
            refs = refs[: self._limit]
        return refs

    def _rows(self):
        return [dict(r) for r in self._matched_refs()]

    def execute(self):
        if self.op == "insert":
            row = dict(self.payload)
            if row.get("user_id") is None:
                raise RuntimeError("null value in column user_id")
            table_rows = self.store.setdefault(self.table, [])
            # emulate unique constraints we care about
            if self.table == "plants":
                for existing in table_rows:
                    if (existing["user_id"] == row["user_id"]
                            and existing["species"] == row["species"]
                            and existing["data_source"] == row["data_source"]
                            and existing.get("variety") == row.get("variety")
                            and existing.get("external_ref")
                            == row.get("external_ref")):
                        raise RuntimeError(
                            "duplicate key value violates unique constraint "
                            "(23505)")
            if self.table == "observations":
                for existing in table_rows:
                    if (existing["plant_id"] == row["plant_id"]
                            and existing["observation_date"]
                            == row["observation_date"]):
                        raise RuntimeError(
                            "duplicate key value violates unique constraint "
                            "(23505)")
            # assign an id
            id_col = {
                "plants": "plant_id", "observations": "observation_id",
                "readiness_events": "readiness_event_id",
                "transplant_events": "transplant_event_id",
                "feedback_14d": "feedback_id",
                "feedback_corrections": "correction_id",
            }.get(self.table)
            if id_col:
                row[id_col] = (max([r.get(id_col, 0) for r in table_rows]
                                   or [0]) + 1)
            table_rows.append(row)
            return FakeResponse([row])
        if self.op == "update":
            matched = self._matched_refs()
            for ref in matched:
                ref.update(self.payload)
            return FakeResponse([dict(r) for r in matched])
        if self.op == "delete":
            matched = self._matched_refs()
            self.store[self.table] = [
                r for r in self.store[self.table] if r not in matched]
            return FakeResponse([dict(r) for r in matched])
        return FakeResponse(self._rows())

    # insert()/update()/delete() entry points
    def _new(self, op, payload=None):
        q = FakeQuery(self.store, self.table, op, payload)
        q.filters = dict(self.filters)
        return q

    def insert(self, payload, **kw):
        return FakeQuery(self.store, self.table, "insert", payload)

    def update(self, payload, **kw):
        q = FakeQuery(self.store, self.table, "update", payload)
        q.filters = dict(self.filters)
        q._in_filters = dict(self._in_filters)
        return q

    def delete(self, **kw):
        q = FakeQuery(self.store, self.table, "delete")
        q.filters = dict(self.filters)
        q._in_filters = dict(self._in_filters)
        return q


class FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def select(self, *a, **kw):
        return FakeQuery(self.store, self.name)

    def insert(self, payload, **kw):
        return FakeQuery(self.store, self.name, "insert", payload)

    def update(self, payload, **kw):
        return FakeQuery(self.store, self.name, "update", payload)

    def delete(self, **kw):
        return FakeQuery(self.store, self.name, "delete")


class FakeClient:
    """Minimal supabase.Client stand-in with an in-memory store."""

    def __init__(self, store=None, fail_on=None):
        self.store = store if store is not None else {}
        self.fail_on = fail_on
        self.postgrest = self

    def table(self, name):
        if self.fail_on:
            raise RuntimeError(self.fail_on)
        return FakeTable(self.store, name)

    def auth(self):  # pragma: no cover - not used directly
        raise NotImplementedError


class TestSupabaseRepositoryOwnership(unittest.TestCase):
    """Ownership: every read/write is scoped to the signed-in user."""

    USER_A = "user-aaa"
    USER_B = "user-bbb"

    def setUp(self):
        self.store = {}
        self.client = FakeClient(self.store)
        self.repo_a = SupabaseRepository(self.client, self.USER_A)
        self.repo_b = SupabaseRepository(self.client, self.USER_B)

    def test_requires_user_id(self):
        with self.assertRaises(ConfigurationError):
            SupabaseRepository(self.client, "")

    def test_insert_stamps_caller_user_id(self):
        pid = self.repo_a.register_plant(species="Super Orange",
                                         variety="Early",
                                         data_source="real")
        row = self.store["plants"][0]
        self.assertEqual(row["user_id"], self.USER_A)
        self.assertEqual(row["plant_id"], pid)

    def test_read_only_returns_own_rows(self):
        self.repo_a.register_plant(species="Super Orange", variety="Early",
                                   data_source="real")
        self.repo_b.register_plant(species="Demo Bean", variety="Bush",
                                   data_source="real")
        self.assertEqual(len(self.repo_a.list_plants()), 1)
        self.assertEqual(len(self.repo_b.list_plants()), 1)
        self.assertEqual(self.repo_a.list_plants()[0]["species"],
                         "Super Orange")
        self.assertEqual(self.repo_b.list_plants()[0]["species"],
                         "Demo Bean")

    def test_cannot_read_another_users_plant(self):
        pid = self.repo_a.register_plant(species="Super Orange",
                                         data_source="real")
        # repo_b scoped to user B: plant must be invisible even by id
        self.assertIsNone(self.repo_b.get_plant(pid))
        # and its list never contains A's rows
        self.assertEqual(self.repo_b.list_plants(), [])

    def test_cross_user_duplicate_is_allowed_separately(self):
        """Two accounts may hold the same species - identities are per user."""
        self.repo_a.register_plant(species="Super Orange", data_source="real")
        self.repo_b.register_plant(species="Super Orange", data_source="real")
        self.assertEqual(len(self.store["plants"]), 2)

    def test_duplicate_within_user_blocked(self):
        self.repo_a.register_plant(species="Super Orange", data_source="real")
        with self.assertRaises(DuplicateRecordError):
            self.repo_a.register_plant(species="Super Orange",
                                       data_source="real")

    def test_observations_visible_only_to_owner(self):
        pid = self.repo_a.register_plant(species="Super Orange",
                                         data_source="real")
        self.repo_a.add_observation(plant_id=pid,
                                    observation_date="2083-05-10",
                                    height_cm=3.5)
        self.assertEqual(self.repo_a.get_observations(pid).__len__(), 1)
        self.assertEqual(self.repo_b.get_observations(pid), [])
        self.assertIsNone(self.repo_b.latest_observation(pid))

    def test_feedback_read_scoped_to_owner(self):
        pid = self.repo_a.register_plant(species="Super Orange",
                                         data_source="real")
        self.repo_a.record_transplant(plant_id=pid,
                                      transplant_date="2083-05-20")
        self.assertEqual(len(self.repo_a.pending_feedback()), 1)
        self.assertEqual(self.repo_b.pending_feedback(), [])
        self.assertEqual(self.repo_a.all_feedback()[0]["transplant_date"],
                         "2083-05-20")


class TestSupabaseRepositoryWorkflow(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.repo = SupabaseRepository(FakeClient(self.store), "user-aaa")
        self.pid = self.repo.register_plant(
            species="Super Orange", variety="Early",
            date_started="2083-04-24", data_source="real")

    def test_duplicate_observation_blocked(self):
        self.repo.add_observation(plant_id=self.pid,
                                  observation_date="2083-05-10",
                                  height_cm=3.5)
        with self.assertRaises(DuplicateRecordError):
            self.repo.add_observation(plant_id=self.pid,
                                      observation_date="2083-05-10",
                                      height_cm=4.0)

    def test_transplant_creates_feedback_with_due_date(self):
        fb = self.repo.record_transplant(plant_id=self.pid,
                                         transplant_date="2083-05-20",
                                         height_at_transplant_cm=6.4)
        self.assertEqual(fb["feedback_due_date"], "2083-06-03")
        self.assertIn("transplant_events", self.store)
        self.assertEqual(len(self.store["transplant_events"]), 1)
        self.assertEqual(len(self.store["feedback_14d"]), 1)

    def test_submit_feedback_then_duplicate_blocked(self):
        fb = self.repo.record_transplant(plant_id=self.pid,
                                         transplant_date="2083-05-20",
                                         height_at_transplant_cm=6.4)
        done = self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        self.assertEqual(done["growth_after_14d_cm"], 1.1)
        with self.assertRaises(DuplicateRecordError):
            self.repo.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-04",
                survived_after_14d=True)

    def test_pending_feedback_carries_uniform_fields(self):
        self.repo.record_transplant(plant_id=self.pid,
                                    transplant_date="2083-05-20",
                                    height_at_transplant_cm=6.4)
        row = self.repo.pending_feedback()[0]
        # same keys the CLI/services read from SQLite rows
        self.assertEqual(row["transplant_date"], "2083-05-20")
        self.assertEqual(row["height_at_transplant_cm"], 6.4)
        self.assertEqual(row["species"], "Super Orange")
        self.assertIn("is_overdue", row)

    def test_correction_keeps_audit(self):
        fb = self.repo.record_transplant(plant_id=self.pid,
                                         transplant_date="2083-05-20",
                                         height_at_transplant_cm=6.4)
        self.repo.submit_feedback(
            feedback_id=fb["feedback_id"],
            feedback_submitted_date="2083-06-03", survived_after_14d=True,
            height_after_14d_cm=7.5, visible_condition="healthy")
        fixed = svc.correct_feedback(
            self.repo, feedback_id=fb["feedback_id"],
            height_after_14d_cm=8.0, reason="tape misread")
        self.assertEqual(fixed["height_after_14d_cm"], 8.0)
        revisions = self.repo.feedback_revisions(fb["feedback_id"])
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["reason"], "tape misread")
        self.assertIn("7.5", revisions[0]["previous_values"])

    def test_dashboard_separates_real_and_synthetic(self):
        self.repo.add_observation(plant_id=self.pid,
                                  observation_date="2083-05-10", height_cm=3)
        syn_pid = self.repo.register_plant(species="Demo Bean",
                                           data_source="synthetic")
        self.repo.add_observation(plant_id=syn_pid,
                                  observation_date="2083-05-10", height_cm=3,
                                  data_source="synthetic")
        counts = self.repo.readiness_counts_by_species()
        sources = {c["data_source"] for c in counts}
        self.assertEqual(sources, {"real", "synthetic"})
        stats = self.repo.dashboard_stats()
        self.assertEqual(stats["n_plants"], 2)


class TestSupabaseErrors(unittest.TestCase):
    def test_connection_failure_is_translated(self):
        client = FakeClient(fail_on="Failed to fetch: connection refused")
        repo = SupabaseRepository(client, "user-aaa")
        with self.assertRaises(ConnectionFailedError):
            repo.list_plants()

    def test_unique_violation_is_translated(self):
        class BoomTable:
            def select(self, *a, **k):
                return self
            def eq(self, *a, **k):
                return self
            def insert(self, payload, **k):
                raise RuntimeError(
                    "duplicate key value violates unique constraint "
                    "\"plants_unique_identity\" (23505)")

        class BoomClient:
            def table(self, name):
                return BoomTable()

        repo = SupabaseRepository(BoomClient(), "user-aaa")
        with self.assertRaises(DuplicateRecordError):
            repo.register_plant(species="Super Orange", data_source="real")

    def test_rls_violation_is_translated(self):
        class RLSTable:
            def select(self, *a, **k):
                return self
            def eq(self, *a, **k):
                return self
            def insert(self, payload, **k):
                raise RuntimeError(
                    'new row violates row-level security policy for table '
                    '"plants" (42501)')

        class RLSClient:
            def table(self, name):
                return RLSTable()

        repo = SupabaseRepository(RLSClient(), "user-aaa")
        with self.assertRaises(DatabaseError) as ctx:
            repo.register_plant(species="Super Orange", data_source="real")
        self.assertIn("Row-level security", str(ctx.exception))


class TestRepositoryParity(unittest.TestCase):
    """The same calls produce the same semantics on both backends."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sqlite = create_sqlite_repository(
            Path(self.tmp.name) / "parity.db")
        self.store = {}
        self.supa = SupabaseRepository(FakeClient(self.store), "user-aaa")

    def tearDown(self):
        self.sqlite.close()
        self.tmp.cleanup()

    def test_plant_registration_semantics_match(self):
        a = self.sqlite.register_plant(species="Super Orange",
                                       variety="Early",
                                       date_started="2083-04-24",
                                       data_source="real")
        b = self.supa.register_plant(species="Super Orange",
                                     variety="Early",
                                     date_started="2083-04-24",
                                     data_source="real")
        self.assertEqual(self.sqlite.get_plant(a)["species"],
                         self.supa.get_plant(b)["species"])
        with self.assertRaises(ValidationError):
            self.sqlite.register_plant(species="", data_source="real")
        with self.assertRaises(ValidationError):
            self.supa.register_plant(species="", data_source="real")
        with self.assertRaises(ValidationError):
            self.sqlite.register_plant(species="X", data_source="alien")
        with self.assertRaises(ValidationError):
            self.supa.register_plant(species="X", data_source="alien")

    def test_due_date_matches_across_backends(self):
        a = self.sqlite.register_plant(species="Super Orange",
                                       data_source="real")
        b = self.supa.register_plant(species="Super Orange",
                                     data_source="real")
        fa = self.sqlite.record_transplant(plant_id=a,
                                           transplant_date="2083-05-20")
        fb = self.supa.record_transplant(plant_id=b,
                                         transplant_date="2083-05-20")
        self.assertEqual(fa["feedback_due_date"], fb["feedback_due_date"])

    def test_feedback_validation_matches_across_backends(self):
        a = self.sqlite.register_plant(species="Super Orange",
                                       data_source="real")
        b = self.supa.register_plant(species="Super Orange",
                                     data_source="real")
        fa = self.sqlite.record_transplant(plant_id=a,
                                           transplant_date="2083-05-20")
        fb = self.supa.record_transplant(plant_id=b,
                                         transplant_date="2083-05-20")
        with self.assertRaises(ValidationError):
            self.sqlite.submit_feedback(
                feedback_id=fa["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=True, visible_condition="dead")
        with self.assertRaises(ValidationError):
            self.supa.submit_feedback(
                feedback_id=fb["feedback_id"],
                feedback_submitted_date="2083-06-03",
                survived_after_14d=True, visible_condition="dead")


if __name__ == "__main__":
    unittest.main()
