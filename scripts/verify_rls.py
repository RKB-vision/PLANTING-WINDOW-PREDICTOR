#!/usr/bin/env python
"""Verify Supabase Row Level Security with two real accounts.

Authentication alone must never be mistaken for data isolation - this
script *proves* the policies work by writing as one account and then
checking that a second account can neither read nor overwrite those rows.

It uses only the ANON key plus two ordinary user sessions. No service-role
key is read, printed or required.

Usage
-----
    python scripts/verify_rls.py \
        --url https://xxxx.supabase.co --anon-key eyJ... \
        --email-a alice@example.com --password-a '...' \
        --email-b bob@example.com --password-b '...'

Exit code 0 = isolation verified; 1 = a policy is missing or broken.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class RLSVerificationError(Exception):
    pass


def _sign_in(client, email: str, password: str):
    resp = client.auth.sign_in_with_password(
        {"email": email, "password": password})
    if resp.user is None:
        raise RLSVerificationError(f"Sign-in returned no user for {email}.")
    return str(resp.user.id), resp.session.access_token


def _set_token(client, token: str) -> None:
    """Scope PostgREST queries to this session's JWT (RLS identity)."""
    try:
        client.postgrest.auth(token)
    except AttributeError:  # pragma: no cover - older client layout
        pass


def verify(url: str, anon_key: str, creds_a: tuple, creds_b: tuple) -> list:
    """Run the two-account isolation checks. Returns a list of results."""
    try:
        from supabase import create_client
    except ImportError as exc:  # pragma: no cover
        raise RLSVerificationError(
            "The 'supabase' package is not installed.") from exc

    results: list[dict] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        results.append({"check": name, "passed": bool(passed),
                        "detail": detail})
        marker = "PASS" if passed else "FAIL"
        print(f"  [{marker}] {name}" + (f" - {detail}" if detail else ""))

    # Two independent clients so sessions never taint each other.
    client_a = create_client(url, anon_key)
    client_b = create_client(url, anon_key)

    user_a, token_a = _sign_in(client_a, *creds_a)
    user_b, token_b = _sign_in(client_b, *creds_b)
    _set_token(client_a, token_a)
    _set_token(client_b, token_b)
    print(f"Signed in: A={user_a} B={user_b}")

    marker = f"rls-check-{uuid.uuid4().hex[:8]}"

    # --- 1. A writes a plant; A can read it back -------------------------- #
    plant_row = {
        "species": "RLS Probe",
        "variety": marker,
        "data_source": "synthetic",
        "external_ref": marker,
    }
    resp = client_a.table("plants").insert(
        {**plant_row, "user_id": user_a}).execute()
    inserted = list(resp.data or [])
    check("A can insert its own row", len(inserted) == 1,
          f"rows={len(inserted)}")
    plant_id = inserted[0]["plant_id"] if inserted else None

    back = list(client_a.table("plants").select("*").eq(
        "user_id", user_a).eq("variety", marker).execute().data or [])
    check("A can read its own row", len(back) == 1, f"rows={len(back)}")

    # --- 2. B cannot see A's row ------------------------------------------ #
    leak = list(client_b.table("plants").select("*").eq(
        "variety", marker).execute().data or [])
    check("B cannot see A's row", len(leak) == 0,
          f"leaked rows={len(leak)}")
    leak_by_user = list(client_b.table("plants").select("*").eq(
        "user_id", user_a).execute().data or [])
    check("B cannot select by A's user_id", len(leak_by_user) == 0,
          f"rows={len(leak_by_user)}")

    # --- 3. B cannot overwrite A's row ------------------------------------ #
    upd = list(client_b.table("plants").update({"species": "HACKED"}).eq(
        "plant_id", plant_id).execute().data or [])
    check("B cannot update A's row", len(upd) == 0, f"rows={len(upd)}")
    after = list(client_a.table("plants").select("species").eq(
        "plant_id", plant_id).execute().data or [])
    check("A's row unchanged after B's update attempt",
          bool(after) and after[0]["species"] == "RLS Probe",
          f"species={after[0]['species'] if after else 'missing'}")

    # --- 4. B cannot delete A's row --------------------------------------- #
    if plant_id is not None:
        try:
            client_b.table("plants").delete().eq(
                "plant_id", plant_id).execute()
        except Exception as exc:  # policy may raise - that is a pass
            check("B's delete of A's row rejected by policy", True,
                  type(exc).__name__)
        surviving = list(client_a.table("plants").select("*").eq(
            "plant_id", plant_id).execute().data or [])
        check("A's row survives B's delete attempt", len(surviving) == 1,
              f"rows={len(surviving)}")

    # --- 5. B cannot insert a row claiming A's user_id -------------------- #
    rejected = False
    detail = ""
    try:
        forged = list(client_b.table("plants").insert({
            "species": "FORGED", "variety": marker,
            "data_source": "synthetic",
            "user_id": user_a,
        }).execute().data or [])
        rejected = len(forged) == 0
        detail = f"inserted={len(forged)}"
    except Exception as exc:
        rejected = True
        detail = f"policy rejected ({type(exc).__name__})"
    check("B cannot insert a row with A's user_id", rejected, detail)

    # --- cleanup (A removes its own probe rows) --------------------------- #
    if plant_id is not None:
        try:
            client_a.table("plants").delete().eq(
                "plant_id", plant_id).execute()
        except Exception:
            pass
    try:
        client_a.auth.sign_out()
        client_b.auth.sign_out()
    except Exception:
        pass
    return results


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Two-account Row Level Security verification "
                    "(anon key only, no service-role key).")
    p.add_argument("--url", default=os.environ.get("SUPABASE_URL"))
    p.add_argument("--anon-key", default=os.environ.get("SUPABASE_ANON_KEY"))
    p.add_argument("--email-a", default=os.environ.get("RLS_EMAIL_A"))
    p.add_argument("--password-a", default=os.environ.get("RLS_PASSWORD_A"))
    p.add_argument("--email-b", default=os.environ.get("RLS_EMAIL_B"))
    p.add_argument("--password-b", default=os.environ.get("RLS_PASSWORD_B"))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    missing = [name for name, val in (
        ("--url", args.url), ("--anon-key", args.anon_key),
        ("--email-a", args.email_a), ("--password-a", args.password_a),
        ("--email-b", args.email_b), ("--password-b", args.password_b))
        if not val]
    if missing:
        print("Missing required values: " + ", ".join(missing), file=sys.stderr)
        print("Set SUPABASE_URL / SUPABASE_ANON_KEY or pass flags. "
              "No service-role key is used by this script.", file=sys.stderr)
        return 2
    print("Verifying Row Level Security with two accounts...")
    try:
        results = verify(args.url, args.anon_key,
                         (args.email_a, args.password_a),
                         (args.email_b, args.password_b))
    except RLSVerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        print("RLS IS NOT CORRECTLY CONFIGURED - do not deploy.", file=sys.stderr)
        return 1
    print("Row Level Security verified: accounts are isolated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
