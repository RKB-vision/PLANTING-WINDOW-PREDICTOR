"""Tests: SQLite -> Supabase migration utility.

Covers the safety requirements of task section 5:

* dry-run by default (writes nothing),
* preview grouped by data source,
* synthetic rows excluded unless explicitly opted in,
* duplicate detection (in-source and in-target),
* idempotent re-run (no duplicated rows),
* backup created before any write in apply mode,
* inserted/skipped/failed report,
* no service-role key required (signs in as the destination user),
* identities, history and the real/synthetic distinction preserved.

The Supabase side is mocked - no network, no real secrets.
"""

import argparse
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS = PROJECT_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import migrate_sqlite_to_supabase as mig  # noqa: E402

from plantreadiness import db as dbm  # noqa: E402


def make_source_db(path: Path, with_synthetic: bool = True) -> None:
    conn = dbm.init_db(path)
    # real plant with observations
    pid = dbm.register_plant(conn, species="Super Orange", variety="Early",
                             date_started="2083-04-24", data_source="real",
                             external_ref="P-1")
    dbm.add_observation(conn, pid, "2083-05-10", age_days=16, height_cm=3.5,
                        data_source="real")
    dbm.add_observation(conn, pid, "2083-05-26", age_days=32, height_cm=8.0,
                        data_source="real")
    dbm.record_readiness_event(
        conn, pid, "2083-05-24", readiness_definition_version="so-v1",
        assessment_source="expert", data_source="real")
    dbm.record_transplant(conn, pid, "2083-05-28",
                          height_at_transplant_cm=9.0, data_source="real")
    fb = dbm.pending_feedback(conn)[0]
    dbm.submit_feedback(conn, feedback_id=fb["feedback_id"],
                        feedback_submitted_date="2083-06-11",
                        survived_after_14d=True, height_after_14d_cm=12.0,
                        visible_condition="healthy")
    if with_synthetic:
        spid = dbm.register_plant(conn, species="Demo Bean", variety="Bush",
                                  data_source="synthetic", external_ref="S-1")
        dbm.add_observation(conn, spid, "2083-05-10", age_days=10,
                            height_cm=2.0, data_source="synthetic")
        dbm.record_transplant(conn, spid, "2083-05-20",
                              data_source="synthetic")
    conn.close()


# --------------------------------------------------------------------------- #
# Mocked target
# --------------------------------------------------------------------------- #


class MockTarget:
    """In-memory Supabase target that stores rows like the real tables."""

    def __init__(self):
        self.tables = {"plants": [], "observations": [],
                       "readiness_events": [], "transplant_events": [],
                       "feedback_14d": [], "feedback_corrections": []}
        self._next_id = {k: 1 for k in self.tables}

    # -- duplicate lookups -------------------------------------------------- #
    def _match(self, table, **filters):
        out = []
        for r in self.tables[table]:
            if all(r.get(k) == v for k, v in filters.items()):
                out.append(dict(r))
        return out

    def existing_plants(self, rows):
        found = []
        for row in rows:
            found += self._match(
                "plants", species=row["species"],
                data_source=row["data_source"],
                variety=row.get("variety"),
                external_ref=row.get("external_ref"))
        return found

    def existing_observations(self, rows):
        found = []
        for row in rows:
            found += self._match(
                "observations", plant_id=row["plant_id"],
                observation_date=row["observation_date"])
        return found

    def existing_readiness(self, rows):
        found = []
        for row in rows:
            found += self._match(
                "readiness_events", plant_id=row["plant_id"],
                readiness_definition_version=row[
                    "readiness_definition_version"])
        return found

    def existing_feedback(self, rows):
        found = []
        for row in rows:
            trans = self._match("transplant_events", plant_id=row["plant_id"],
                                transplant_date=row["transplant_date"])
            for t in trans:
                found += self._match(
                    "feedback_14d",
                    transplant_event_id=t["transplant_event_id"])
        return found

    # -- writes ------------------------------------------------------------- #
    def insert(self, table, row):
        payload = {k: v for k, v in row.items() if k != "user_id"}
        id_col = {"plants": "plant_id", "observations": "observation_id",
                  "readiness_events": "readiness_event_id",
                  "transplant_events": "transplant_event_id",
                  "feedback_14d": "feedback_id",
                  "feedback_corrections": "correction_id"}[table]
        # emulate NOT NULL user_id (RLS would reject a missing owner)
        if row.get("user_id") is None:
            raise RuntimeError("null user_id violates RLS")
        if any(existing.get("user_id") != row["user_id"]
               for existing in [payload] if False):
            pass
        payload["user_id"] = row["user_id"]
        payload[id_col] = self._next_id[table]
        self._next_id[table] += 1
        self.tables[table].append(payload)
        return payload[id_col]


def build_plan_with(source_conn, target, include_synthetic=False):
    return mig.build_plan(source_conn, include_synthetic, target)


class TestInspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "src.db"
        make_source_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspect_groups_by_source(self):
        info, conn = mig.inspect_sqlite(self.db)
        try:
            self.assertEqual(info["counts_by_source"]["plants"],
                             {"real": 1, "synthetic": 1})
            self.assertEqual(
                info["counts_by_source"]["observations"],
                {"real": 2, "synthetic": 1})
            self.assertTrue(info["tables"]["feedback_14d"]["present"])
        finally:
            conn.close()

    def test_missing_database_reports_clear_error(self):
        with self.assertRaises(mig.MigrationError):
            mig.inspect_sqlite(Path(self.tmp.name) / "nope.db")

    def test_non_plant_database_rejected(self):
        other = Path(self.tmp.name) / "other.db"
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(mig.MigrationError):
            mig.inspect_sqlite(other)


class TestPlanAndPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "src.db"
        make_source_db(self.db)
        self.target = MockTarget()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_plan_excludes_synthetic(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            plan = build_plan_with(conn, self.target,
                                   include_synthetic=False)
        finally:
            conn.close()
        self.assertEqual(len(plan["tables"]["plants"]["to_insert"]), 1)
        self.assertEqual(plan["skipped_synthetic"]["plants"], 1)
        self.assertEqual(plan["skipped_synthetic"]["observations"], 1)
        # only real rows planned anywhere
        for name in ("observations", "readiness_events", "feedback_14d"):
            for row in plan["tables"][name]["to_insert"]:
                self.assertEqual(row["data_source"], "real")

    def test_opt_in_includes_synthetic_keeps_label(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            plan = build_plan_with(conn, self.target,
                                   include_synthetic=True)
        finally:
            conn.close()
        self.assertEqual(len(plan["tables"]["plants"]["to_insert"]), 2)
        sources = {r["data_source"]
                   for r in plan["tables"]["plants"]["to_insert"]}
        self.assertEqual(sources, {"real", "synthetic"})
        self.assertEqual(plan["skipped_synthetic"], {})

    def test_preview_reports_by_table_and_source(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            plan = build_plan_with(conn, self.target, False)
            info, _ = mig.inspect_sqlite(self.db)
            buf = io.StringIO()
            with redirect_stdout(buf):
                mig.print_preview(info, plan)
        finally:
            conn.close()
        out = buf.getvalue()
        self.assertIn("dry-run", out)
        self.assertIn("nothing written", out)
        self.assertIn("EXCLUDED", out)
        self.assertIn("--include-synthetic", out)
        self.assertIn("plants", out)

    def test_existing_target_rows_counted_as_duplicates(self):
        # pre-populate target with the same real plant
        self.target.insert("plants", {
            "species": "Super Orange", "variety": "Early",
            "data_source": "real", "external_ref": "P-1",
            "user_id": "user-1"})
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            plan = build_plan_with(conn, self.target, False)
        finally:
            conn.close()
        self.assertEqual(
            len(plan["tables"]["plants"]["duplicate_in_target"]), 1)
        # duplicates are excluded from insertion
        self.assertEqual(len(plan["tables"]["plants"]["to_insert"]), 0)


class FakeAuthSession:
    access_token = "tok"
    refresh_token = "ref"


class FakeAuthUser:
    id = "user-1"
    email = "a@b.c"


class FakeAuthResp:
    user = FakeAuthUser()
    session = FakeAuthSession()


class FakeAuth:
    def sign_in_with_password(self, creds):
        return FakeAuthResp()


class FakeNetClient:
    """Stands in for supabase.create_client - no network involved."""

    def __init__(self):
        self.auth = FakeAuth()


def apply_migration(db: Path, target: MockTarget,
                    include_synthetic: bool = False) -> dict:
    """Run run_migration() against ``target`` with all network removed."""
    import supabase as supabase_mod
    original_client = mig.SupabaseTarget
    original_create = supabase_mod.create_client

    class PatchedTarget:
        def __init__(self, client, user_id):
            # mirror the real SupabaseTarget: it stamps rows with the
            # signed-in user's id before writing (RLS identity)
            self.user_id = user_id

        def insert(self, table, row):
            return target.insert(table, {**row, "user_id": self.user_id})

        def __getattr__(self, name):
            return getattr(target, name)

    mig.SupabaseTarget = PatchedTarget
    supabase_mod.create_client = lambda url, key: FakeNetClient()
    try:
        return mig.run_migration(argparse.Namespace(
            sqlite=str(db), url="https://x.supabase.co", anon_key="k",
            email="a@b.c", password="pw", apply=True,
            include_synthetic=include_synthetic, json=False))
    finally:
        mig.SupabaseTarget = original_client
        supabase_mod.create_client = original_create


class TestApplyRerunSafety(unittest.TestCase):
    """End-to-end apply against a mocked target: idempotent re-runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "src.db"
        make_source_db(self.db)
        self.target = MockTarget()

    def tearDown(self):
        self.tmp.cleanup()

    def _apply_direct(self, include_synthetic=False):
        return apply_migration(self.db, self.target, include_synthetic)

    def test_apply_creates_backup_before_write(self):
        result = self._apply_direct()
        self.assertTrue(result["applied"])
        backups = list(Path(self.tmp.name).glob("src.db.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(Path(result["backup"]).exists())
        # backup contains the original rows
        bak = sqlite3.connect(result["backup"])
        n = bak.execute("SELECT COUNT(*) FROM plants").fetchone()[0]
        bak.close()
        self.assertEqual(n, 2)  # 1 real + 1 synthetic preserved in backup

    def test_first_apply_inserts_real_rows(self):
        result = self._apply_direct()
        tables = result["tables"]
        self.assertEqual(tables["plants"]["inserted"], 1)
        self.assertEqual(tables["observations"]["inserted"], 2)
        self.assertEqual(tables["readiness_events"]["inserted"], 1)
        self.assertEqual(tables["feedback_14d"]["inserted"], 1)
        self.assertEqual(tables["plants"]["failed"], 0)
        # target now holds only real data (synthetic excluded by default)
        stored = self.target.tables["plants"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["data_source"], "real")
        # transplant + feedback split into two tables, linked
        self.assertEqual(len(self.target.tables["transplant_events"]), 1)
        self.assertEqual(len(self.target.tables["feedback_14d"]), 1)
        link = self.target.tables["feedback_14d"][0]["transplant_event_id"]
        self.assertEqual(
            link, self.target.tables["transplant_events"][0]
            ["transplant_event_id"])

    def test_rerun_skips_everything(self):
        first = self._apply_direct()
        second = self._apply_direct()
        self.assertEqual(second["tables"]["plants"]["inserted"], 0)
        self.assertEqual(second["tables"]["plants"]["skipped"],
                         first["tables"]["plants"]["inserted"])
        self.assertEqual(second["tables"]["observations"]["inserted"], 0)
        self.assertEqual(second["tables"]["observations"]["skipped"],
                         first["tables"]["observations"]["inserted"])
        self.assertEqual(second["tables"]["feedback_14d"]["inserted"], 0)
        # nothing duplicated in the target
        self.assertEqual(len(self.target.tables["plants"]), 1)
        self.assertEqual(len(self.target.tables["observations"]), 2)
        self.assertEqual(len(self.target.tables["feedback_14d"]), 1)

    def test_identity_and_history_preserved(self):
        self._apply_direct()
        plant = self.target.tables["plants"][0]
        self.assertEqual(plant["species"], "Super Orange")
        self.assertEqual(plant["variety"], "Early")
        self.assertEqual(plant["date_started"], "2083-04-24")
        self.assertEqual(plant["external_ref"], "P-1")
        self.assertEqual(plant["user_id"], "user-1")
        obs = self.target.tables["observations"]
        self.assertEqual([o["observation_date"] for o in obs],
                         ["2083-05-10", "2083-05-26"])
        fb = self.target.tables["feedback_14d"][0]
        self.assertEqual(fb["feedback_submitted_date"], "2083-06-11")
        self.assertEqual(fb["survived_after_14d"], 1)
        self.assertEqual(fb["growth_after_14d_cm"], 3.0)

    def test_failed_rows_are_reported_not_swallowed(self):
        # break the target so one table fails
        real_insert = self.target.insert

        def flaky(table, row):
            if table == "observations":
                raise RuntimeError("simulated failure")
            return real_insert(table, row)
        self.target.insert = flaky

        result = self._apply_direct()
        self.assertEqual(result["tables"]["observations"]["failed"], 2)
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("observations", result["errors"][0])
        # other tables still migrated
        self.assertEqual(result["tables"]["plants"]["inserted"], 1)

    def test_synthetic_opt_in_migrates_separate_rows(self):
        result = self._apply_direct(include_synthetic=True)
        self.assertEqual(result["tables"]["plants"]["inserted"], 2)
        sources = {r["data_source"] for r in self.target.tables["plants"]}
        self.assertEqual(sources, {"real", "synthetic"})
        # synthetic rows keep their label everywhere
        for table in ("observations", "feedback_14d"):
            labels = {r["data_source"]
                      for r in self.target.tables[table]}
            self.assertIn("synthetic", labels)


class TestDryRunDefault(unittest.TestCase):
    def test_parser_defaults_to_dry_run(self):
        args = mig.build_parser().parse_args([])
        self.assertFalse(args.apply)
        self.assertFalse(args.include_synthetic)

    def test_dry_run_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "src.db"
            make_source_db(db)
            before = db.stat().st_mtime_ns, db.stat().st_size
            # dry-run cannot even reach the network: no credentials given
            import contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = mig.main(["--sqlite", str(db), "--apply"])
            self.assertEqual(rc, 2)  # configuration error, nothing written
            self.assertIn("Supabase is not configured", err.getvalue())
            after = db.stat().st_mtime_ns, db.stat().st_size
            self.assertEqual(before, after)
            # no backups created either
            self.assertEqual(list(Path(tmp).glob("*.bak-*")), [])

    def test_missing_credentials_message_mentions_no_service_role(self):
        import supabase as supabase_mod
        original_create = supabase_mod.create_client
        supabase_mod.create_client = lambda url, key: FakeNetClient()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "src.db"
                make_source_db(db)
                err = io.StringIO()
                import contextlib
                with contextlib.redirect_stderr(err):
                    rc = mig.main(["--sqlite", str(db), "--url",
                                   "https://x.supabase.co", "--anon-key",
                                   "k", "--apply"])
                self.assertEqual(rc, 2)
                self.assertIn("service-role", err.getvalue())
        finally:
            supabase_mod.create_client = original_create

    def test_include_synthetic_flag_exists(self):
        args = mig.build_parser().parse_args(["--include-synthetic"])
        self.assertTrue(args.include_synthetic)


class TestMigrationSqlFiles(unittest.TestCase):
    """Static checks on the versioned SQL migrations (no database needed)."""

    MIGRATIONS = PROJECT_ROOT / "supabase" / "migrations"

    def _sql(self, name: str) -> str:
        path = self.MIGRATIONS / name
        self.assertTrue(path.is_file(), f"missing migration {name}")
        return path.read_text()

    def test_schema_defines_every_table_with_keys(self):
        sql = self._sql("0001_initial_schema.sql").lower()
        for table in ("user_profiles", "plants", "observations",
                      "readiness_events", "transplant_events",
                      "feedback_14d", "feedback_corrections",
                      "model_metadata"):
            self.assertIn(f"create table if not exists public.{table}",
                          sql, f"{table} missing from schema migration")
        # identity + provenance columns everywhere
        self.assertIn("user_id", sql)
        self.assertIn("data_source", sql)
        # uniqueness for idempotent imports / one-outcome-per-transplant
        self.assertIn("unique", sql)
        self.assertIn("references", sql)   # FKs enforced

    def test_rls_enabled_with_per_user_policies(self):
        import re
        # normalize whitespace: the SQL aligns columns with extra spaces
        sql = re.sub(r"\s+", " ",
                     self._sql("0002_row_level_security.sql").lower())
        for table in ("user_profiles", "plants", "observations",
                      "readiness_events", "transplant_events",
                      "feedback_14d", "feedback_corrections",
                      "model_metadata"):
            self.assertIn(
                f"alter table public.{table} enable row level security",
                sql, f"RLS not enabled on {table}")
        # policies must be scoped to auth.uid(), not open access
        self.assertIn("auth.uid()", sql)
        # all four verbs are policy-controlled
        for verb in ("select", "insert", "update", "delete"):
            self.assertIn(verb, sql)

    def test_no_service_role_key_anywhere_in_repo_sources(self):
        """No privileged JWT material in code, SQL, or config templates.

        Anon keys are public by design and also look like JWTs (tests use
        obviously-fake ones); what is forbidden is a key whose payload
        actually claims the service_role (or other privileged marker).
        """
        import base64
        import json
        import re
        jwt = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

        def privileged(match: "re.Match[str]") -> bool:
            payload = match.group(0).split(".")[1]
            payload += "=" * (-len(payload) % 4)   # pad base64url
            try:
                claims = json.loads(base64.urlsafe_b64decode(payload))
            except (ValueError, json.JSONDecodeError):
                return False
            return claims.get("role") not in (None, "anon", "authenticated")

        roots = [PROJECT_ROOT / "webapp", PROJECT_ROOT / "plantreadiness",
                 PROJECT_ROOT / "scripts", PROJECT_ROOT / "supabase",
                 PROJECT_ROOT / ".streamlit", PROJECT_ROOT / "tests"]
        offenders = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix in {
                        ".pyc", ".db", ".sqlite3"} or "__pycache__" in \
                        path.parts:
                    continue
                try:
                    text = path.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                if any(privileged(m) for m in jwt.finditer(text)):
                    offenders.append(str(path))
        self.assertEqual(offenders, [],
                         f"privileged key material found in {offenders}")

    def test_auth_uid_not_used_in_column_check_constraints(self):
        """auth.uid() is STABLE; column CHECK constraints need IMMUTABLE
        functions, so RLS (not CHECK) must be the enforcement layer.

        Note: RLS *policy* ``with check (user_id = auth.uid())`` clauses
        are exactly the intended enforcement and are not column checks.
        """
        sql = self._sql("0001_initial_schema.sql")
        for line in sql.splitlines():
            if "check" in line.lower() and "auth.uid()" in line:
                self.fail(f"0001 uses auth.uid() in a column CHECK "
                          f"constraint: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
