#!/usr/bin/env python3
"""Staging integration test for dual-write mode.

Writes through DualWriteOperationalStore (D1 primary + PostgreSQL shadow),
then reads from each backend independently to verify data landed in both.

Usage:
    python scripts/cloudflare/d1_dual_write_test.py
    python scripts/cloudflare/d1_dual_write_test.py --no-cleanup

Environment:
    D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN  — D1 credentials
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD — PostgreSQL credentials
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv()

_PREFIX = "dw_test"
_TEST_EMAIL = f"{_PREFIX}_{secrets.token_hex(4)}@test.example.com"
_TEST_USER_NAME = f"{_PREFIX}_user"


class _Results:
    """Track pass/fail counts."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str) -> None:
        """Record a passing check."""
        self.passed += 1
        print(f"  [OK]   {name}")

    def fail(self, name: str, detail: str = "") -> None:
        """Record a failing check."""
        self.failed += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f" — {detail}"
        self.errors.append(msg)
        print(msg)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        """Record a check result."""
        if condition:
            self.ok(name)
        else:
            self.fail(name, detail)
        return condition

    def summary(self) -> int:
        """Print summary and return exit code."""
        total = self.passed + self.failed
        print(f"\n{'=' * 60}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("\nFailures:")
            for e in self.errors:
                print(f"  {e}")
        return 0 if self.failed == 0 else 1


async def _run(args: argparse.Namespace) -> int:
    """Run dual-write integration tests against real D1 + PostgreSQL."""
    import asyncpg

    from serving.config.settings import get_settings
    from serving.storage.d1_client import D1Client
    from serving.storage.d1_operational import D1OperationalStore
    from serving.storage.dual_write import DualWriteOperationalStore
    from serving.storage.postgres_operational import PostgresOperationalStore

    get_settings.cache_clear()
    settings = get_settings()
    r = _Results()

    print("\n=== Dual-Write Staging Test ===")
    print(f"  Test prefix: {_PREFIX}")
    print(f"  Test email:  {_TEST_EMAIL}\n")

    # -- Check credentials
    if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
        r.fail("d1_credentials", "D1_ACCOUNT_ID / D1_DATABASE_ID / D1_API_TOKEN not set")
        return r.summary()

    if not all([settings.db_host, settings.db_name, settings.db_user]):
        r.fail("pg_credentials", "DB_HOST / DB_NAME / DB_USER not set")
        return r.summary()

    # -- Initialize both backends
    d1_client = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )
    d1_store = D1OperationalStore(d1_client)

    pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=int(settings.db_port),
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_size=1,
        max_size=3,
    )
    pg_store = PostgresOperationalStore(pool)

    # Wrap in dual-write
    dual = DualWriteOperationalStore(d1_store, pg_store)

    # Track IDs for cleanup
    user_id = f"{_PREFIX}_{secrets.token_hex(6)}"
    key_hash = f"{_PREFIX}_keyhash_{secrets.token_hex(4)}"
    key_prefix = f"{_PREFIX}_pfx_{secrets.token_hex(3)}"
    session_id = f"{_PREFIX}_sess_{secrets.token_hex(4)}"
    refresh_hash = f"{_PREFIX}_rth_{secrets.token_hex(4)}"

    try:
        # -- Health check
        d1_ok = await d1_client.health_check()
        r.check("d1_health", d1_ok, "D1 unreachable")
        if not d1_ok:
            return r.summary()

        pg_ok = await pg_store.health_check()
        r.check("pg_health", pg_ok, "PostgreSQL unreachable")
        if not pg_ok:
            return r.summary()

        r.check("dual_write_shadow_healthy", dual.shadow_healthy)

        # -- Initialize schemas (idempotent CREATE TABLE IF NOT EXISTS)
        await d1_store.initialize()
        r.ok("d1_schema_init")
        await pg_store.initialize()
        r.ok("pg_schema_init")

        # ==============================================================
        # TEST 1: Create user
        # ==============================================================
        print("\n--- User CRUD ---")
        await dual.create_user(
            user_id=user_id,
            email=_TEST_EMAIL,
            password_hash="pbkdf2_test_hash",
            user_name=_TEST_USER_NAME,
        )

        d1_user = await d1_store.get_user_by_id(user_id)
        pg_user = await pg_store.get_user_by_id(user_id)

        r.check("create_user_d1", d1_user is not None, "Missing from D1")
        r.check("create_user_pg", pg_user is not None, "Missing from PostgreSQL")

        if d1_user and pg_user:
            r.check(
                "create_user_email_match",
                d1_user["email"] == pg_user["email"] == _TEST_EMAIL,
                f"D1={d1_user.get('email')} PG={pg_user.get('email')}",
            )
            r.check(
                "create_user_name_match",
                d1_user.get("user_name") == pg_user.get("user_name") == _TEST_USER_NAME,
            )

        # ==============================================================
        # TEST 2: Update user fields
        # ==============================================================
        await dual.update_user_fields(user_id, user_name="DW Updated", role="admin")

        d1_user = await d1_store.get_user_by_id(user_id)
        pg_user = await pg_store.get_user_by_id(user_id)

        r.check(
            "update_user_d1",
            d1_user and d1_user.get("user_name") == "DW Updated",
            f"D1 user_name={d1_user.get('user_name') if d1_user else 'None'}",
        )
        r.check(
            "update_user_pg",
            pg_user and pg_user.get("user_name") == "DW Updated",
            f"PG user_name={pg_user.get('user_name') if pg_user else 'None'}",
        )

        # ==============================================================
        # TEST 3: Create API key
        # ==============================================================
        print("\n--- API Keys ---")
        await dual.create_key(
            key_hash=key_hash,
            key_prefix=key_prefix,
            user_id=user_id,
            tier="pro",
            quota_daily_cost_usd=500.0,
            notes="dual-write test key",
        )

        d1_key = await d1_store.get_key_detail(user_id)
        pg_key = await pg_store.get_key_detail(user_id)

        r.check("create_key_d1", d1_key is not None, "Missing from D1")
        r.check("create_key_pg", pg_key is not None, "Missing from PostgreSQL")

        if d1_key and pg_key:
            r.check(
                "create_key_tier_match",
                d1_key.get("tier") == pg_key.get("tier") == "pro",
            )

        # ==============================================================
        # TEST 4: Revoke key
        # ==============================================================
        await dual.revoke_key(user_id)

        d1_key = await d1_store.get_key_detail(user_id)
        pg_key = await pg_store.get_key_detail(user_id)

        r.check(
            "revoke_key_d1",
            d1_key and d1_key.get("status") == "revoked",
            f"D1 status={d1_key.get('status') if d1_key else 'None'}",
        )
        r.check(
            "revoke_key_pg",
            pg_key and pg_key.get("status") == "revoked",
            f"PG status={pg_key.get('status') if pg_key else 'None'}",
        )

        # ==============================================================
        # TEST 5: Create session
        # ==============================================================
        print("\n--- Sessions ---")
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await dual.create_session(
            session_id=session_id,
            user_id=user_id,
            refresh_token_hash=refresh_hash,
            jti=f"{_PREFIX}_jti",
            sid=f"{_PREFIX}_sid",
            expires_at=expires,
        )

        d1_sess = await d1_store.get_session_by_token_hash(refresh_hash)
        pg_sess = await pg_store.get_session_by_token_hash(refresh_hash)

        r.check("create_session_d1", d1_sess is not None, "Missing from D1")
        r.check("create_session_pg", pg_sess is not None, "Missing from PostgreSQL")

        # ==============================================================
        # TEST 6: Revoke session
        # ==============================================================
        await dual.revoke_session(session_id)

        d1_sess = await d1_store.get_session_by_token_hash(refresh_hash)
        pg_sess = await pg_store.get_session_by_token_hash(refresh_hash)

        d1_revoked = d1_sess and (d1_sess.get("revoked") in (True, 1))
        pg_revoked = pg_sess and (pg_sess.get("revoked") in (True, 1))
        r.check("revoke_session_d1", d1_revoked)
        r.check("revoke_session_pg", pg_revoked)

        # ==============================================================
        # TEST 7: Cost counters
        # ==============================================================
        print("\n--- Cost Counters ---")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await dual.increment_user_cost(user_id, 1.50, day=today)
        await dual.increment_user_cost(user_id, 2.25, day=today)

        d1_cost = await d1_store.get_user_cost_today(user_id)
        pg_cost = await pg_store.get_user_cost_today(user_id)

        r.check(
            "cost_d1",
            abs(d1_cost - 3.75) < 0.01,
            f"Expected ~3.75, got {d1_cost}",
        )
        r.check(
            "cost_pg",
            abs(pg_cost - 3.75) < 0.01,
            f"Expected ~3.75, got {pg_cost}",
        )

        # ==============================================================
        # TEST 8: Audit log
        # ==============================================================
        print("\n--- Audit Log ---")
        await dual.log_admin_action(
            admin_ip="10.0.0.1",
            action=f"{_PREFIX}_test_action",
            target_user_id=user_id,
            details={"reason": "dual-write integration test"},
        )

        d1_count, _d1_rows = await d1_store.list_audit_log(action=f"{_PREFIX}_test_action")
        pg_count, _pg_rows = await pg_store.list_audit_log(action=f"{_PREFIX}_test_action")

        r.check("audit_d1", d1_count >= 1, f"Got {d1_count} rows")
        r.check("audit_pg", pg_count >= 1, f"Got {pg_count} rows")

        # ==============================================================
        # TEST 9: Shadow healthy property
        # ==============================================================
        print("\n--- Health ---")
        r.check("shadow_still_healthy", dual.shadow_healthy)

        overall_health = await dual.health_check()
        r.check("dual_health_check", overall_health)

    finally:
        # -- Cleanup
        if not args.no_cleanup:
            print("\n--- Cleanup ---")
            try:
                # Clean from D1
                await d1_client.query(
                    "DELETE FROM admin_audit_log WHERE target_user_id = ?", [user_id]
                )
                await d1_client.query(
                    "DELETE FROM admin_audit_log WHERE action = ?",
                    [f"{_PREFIX}_test_action"],
                )
                await d1_client.query("DELETE FROM user_daily_cost WHERE user_id = ?", [user_id])
                await d1_client.query("DELETE FROM auth_sessions WHERE user_id = ?", [user_id])
                await d1_client.query("DELETE FROM api_keys WHERE user_id = ?", [user_id])
                await d1_client.query("DELETE FROM users WHERE id = ?", [user_id])
                print("  D1 cleanup done")
            except Exception as e:
                print(f"  D1 cleanup error: {e}")

            try:
                # Clean from PostgreSQL
                async with pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id
                    )
                    await conn.execute("DELETE FROM user_daily_cost WHERE user_id = $1", user_id)
                    await conn.execute("DELETE FROM auth_sessions WHERE user_id = $1", user_id)
                    await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
                    await conn.execute("DELETE FROM users WHERE id = $1", user_id)
                print("  PostgreSQL cleanup done")
            except Exception as e:
                print(f"  PostgreSQL cleanup error: {e}")

        await d1_client.close()
        await pool.close()

    return r.summary()


def main() -> None:
    """Parse arguments and run the dual-write staging test."""
    parser = argparse.ArgumentParser(
        description="Dual-write integration test against real D1 + PostgreSQL."
    )
    parser.add_argument("--no-cleanup", action="store_true", help="Skip cleanup after tests")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
