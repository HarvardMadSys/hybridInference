#!/usr/bin/env python3
"""Staging integration test for the D1 backend.

Exercises every operational table against a real D1 instance, then
optionally runs a full auth flow against the staging HTTP server.

Usage:
    # Part 1: Direct store tests (D1 credentials required)
    python scripts/d1_staging_test.py --store-only

    # Part 2: Full HTTP flow (staging server must be running with DB_BACKEND=d1)
    python scripts/d1_staging_test.py --http-only --base-url https://staging.freeinference.org

    # Both
    python scripts/d1_staging_test.py --base-url https://staging.freeinference.org

    # Clean up test data after run
    python scripts/d1_staging_test.py --cleanup

Environment:
    D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN  — for direct store tests
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD — for log store tests
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv()

# Test data prefix — all test entities use this so cleanup can find them
_PREFIX = "d1staging"
_TEST_EMAIL = f"{_PREFIX}_{secrets.token_hex(4)}@test.example.com"
_TEST_PASSWORD = f"Test{secrets.token_hex(8)}!Aa1"
_TEST_USER_NAME = f"{_PREFIX}_user"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Results:
    """Track pass/fail counts."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str) -> None:
        self.passed += 1
        print(f"  [OK]   {name}")

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f" — {detail}"
        self.errors.append(msg)
        print(msg)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.ok(name)
        else:
            self.fail(name, detail)
        return condition

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{'=' * 60}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print("\nFailures:")
            for e in self.errors:
                print(f"  {e}")
        return 0 if self.failed == 0 else 1


# ---------------------------------------------------------------------------
# Part 1: Direct store tests
# ---------------------------------------------------------------------------


async def _test_store(r: _Results) -> dict[str, str]:
    """Test D1OperationalStore directly. Returns test entity IDs for cleanup."""
    from serving.config.settings import get_settings
    from serving.storage.cache import CachedOperationalStore, InMemoryCache
    from serving.storage.d1_client import D1Client
    from serving.storage.d1_operational import D1OperationalStore

    get_settings.cache_clear()
    settings = get_settings()

    print("\n=== Part 1: Direct D1 Store Tests ===\n")

    # -- Connect
    if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
        r.fail("d1_credentials", "D1_ACCOUNT_ID / D1_DATABASE_ID / D1_API_TOKEN not set")
        return {}

    client = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    ids: dict[str, str] = {}

    try:
        # -- Health check
        healthy = await client.health_check()
        r.check("d1_health_check", healthy, "D1 API unreachable")
        if not healthy:
            return ids

        store = D1OperationalStore(client)
        cached = CachedOperationalStore(store, InMemoryCache())

        # -- Initialize schema (idempotent)
        try:
            await store.initialize()
            r.ok("schema_initialize")
        except Exception as exc:
            r.fail("schema_initialize", str(exc))
            return ids

        # ============================================================
        # USERS TABLE
        # ============================================================
        print("\n  --- users ---")
        user_id = f"{_PREFIX}_{secrets.token_hex(8)}"
        ids["user_id"] = user_id

        # Create user
        try:
            await store.create_user(
                user_id=user_id,
                email=_TEST_EMAIL,
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$fakehash",
                user_name=_TEST_USER_NAME,
                email_verified=False,
                status="active",
            )
            r.ok("create_user")
        except Exception as exc:
            r.fail("create_user", str(exc))
            return ids

        # Get user by ID
        user = await store.get_user_by_id(user_id)
        r.check("get_user_by_id", user is not None and user["id"] == user_id)

        # Get user by email (lowercased)
        user = await store.get_user_by_email(_TEST_EMAIL.upper())
        r.check("get_user_by_email", user is not None and user["email"] == _TEST_EMAIL.lower())

        # Update user fields
        await store.update_user_fields(user_id, user_name="Updated Name", role="admin")
        user = await store.get_user_by_id(user_id)
        r.check(
            "update_user_fields", user["user_name"] == "Updated Name" and user["role"] == "admin"
        )

        # Update last login
        await store.update_user_last_login(user_id)
        user = await store.get_user_by_id(user_id)
        r.check("update_user_last_login", user["last_login_at"] is not None)

        # Mark email verified
        await store.mark_user_email_verified(user_id)
        user = await store.get_user_by_id(user_id)
        # D1 returns INTEGER 1 for TRUE
        r.check("mark_email_verified", user["email_verified"] in (True, 1))

        # Get user counts
        counts = await store.get_user_counts_by_status()
        r.check("get_user_counts_by_status", isinstance(counts, dict) and "active" in counts)

        # Active user counts
        active = await store.get_active_user_counts()
        r.check("get_active_user_counts", "total" in active and "dau" in active and "mau" in active)

        # List users
        total, rows, _status_counts = await store.list_users(search=_PREFIX, limit=10)
        r.check("list_users", total >= 1 and len(rows) >= 1)

        # User preferences
        await store.update_user_preferences(user_id, {"theme": "dark", "lang": "en"})
        prefs = await store.get_user_preferences(user_id)
        r.check("user_preferences", prefs.get("theme") == "dark")

        # Cache test: get_user_by_id should be cached
        t0 = time.monotonic()
        await cached.get_user_by_id(user_id)  # warm
        await cached.get_user_by_id(user_id)  # should be cached
        t1 = time.monotonic()
        r.check("cache_hit_user", (t1 - t0) < 0.5, f"took {(t1 - t0) * 1000:.0f}ms")

        # ============================================================
        # API KEYS TABLE
        # ============================================================
        print("\n  --- api_keys ---")
        from serving.servers.auth import generate_api_key, hash_api_key

        api_key = generate_api_key()
        key_hash = hash_api_key(api_key)
        key_prefix = api_key[:12]

        # Create key
        key_row = await store.create_key(
            key_hash=key_hash,
            key_prefix=key_prefix,
            user_id=user_id,
            user_name=_TEST_USER_NAME,
            tier="free",
            quota_daily_cost_usd=100.0,
            account_id=user_id,
        )
        r.check("create_key", key_row is not None and "id" in key_row)
        ids["api_key"] = api_key

        # Check active key exists
        exists = await store.check_active_key_exists(user_id)
        r.check("check_active_key_exists", exists)

        # Auth context (the hot path)
        ctx = await store.get_auth_context_by_key_hash(key_hash)
        r.check(
            "get_auth_context_by_key_hash",
            ctx is not None and ctx["user_id"] == user_id and ctx["email"] == _TEST_EMAIL.lower(),
            f"got: {ctx}",
        )

        # Lightweight auth context
        ctx_light = await store.get_auth_context_lightweight(key_hash)
        r.check("get_auth_context_lightweight", ctx_light is not None and "role" in ctx_light)

        # Cache test: auth context should be cached
        t0 = time.monotonic()
        await cached.get_auth_context_by_key_hash(key_hash)  # warm
        await cached.get_auth_context_by_key_hash(key_hash)  # cached
        t1 = time.monotonic()
        r.check("cache_hit_auth", (t1 - t0) < 0.5, f"took {(t1 - t0) * 1000:.0f}ms")

        # Update key last used
        if key_row and "id" in key_row:
            await store.update_key_last_used(key_row["id"])
            r.ok("update_key_last_used")

        # List keys
        total_k, _key_rows = await store.list_keys(status="active", limit=10)
        r.check("list_keys", total_k >= 1)

        # Get key detail
        detail = await store.get_key_detail(user_id)
        r.check("get_key_detail", detail is not None and detail["key_prefix"] == key_prefix)

        # Get active key by account
        active_key = await store.get_active_key_by_account(user_id)
        r.check("get_active_key_by_account", active_key is not None)

        # Regenerate key
        new_key = generate_api_key()
        new_hash = hash_api_key(new_key)
        new_prefix = new_key[:12]
        old_prefix = await store.regenerate_key(
            user_id, new_key_hash=new_hash, new_key_prefix=new_prefix
        )
        r.check("regenerate_key", old_prefix == key_prefix)

        # Revoke key
        await store.revoke_key(user_id)
        ctx_after = await store.get_auth_context_by_key_hash(new_hash)
        r.check("revoke_key_invalidates", ctx_after is None, "revoked key should not resolve")

        # ============================================================
        # USER DAILY COST TABLE
        # ============================================================
        print("\n  --- user_daily_cost ---")
        test_day = "2020-06-15"

        # increment_user_cost: first insert
        await store.increment_user_cost(user_id, 0.05, day=test_day)
        result = await store._d1.query(
            "SELECT cost_usd, requests FROM user_daily_cost WHERE user_id = ? AND day = ?",
            [user_id, test_day],
        )
        r.check(
            "increment_user_cost_insert",
            len(result.rows) == 1
            and abs(result.rows[0]["cost_usd"] - 0.05) < 1e-6
            and result.rows[0]["requests"] == 1,
        )

        # increment_user_cost: upsert (add to existing)
        await store.increment_user_cost(user_id, 0.10, day=test_day)
        result = await store._d1.query(
            "SELECT cost_usd, requests FROM user_daily_cost WHERE user_id = ? AND day = ?",
            [user_id, test_day],
        )
        r.check(
            "increment_user_cost_upsert",
            len(result.rows) == 1
            and abs(result.rows[0]["cost_usd"] - 0.15) < 1e-6
            and result.rows[0]["requests"] == 2,
        )

        # get_user_cost_period (month)
        cost_month = await store.get_user_cost_period(user_id, "month")
        # test_day is 2020-06, not current month, so should be 0
        r.check("get_user_cost_period_no_match", cost_month == 0.0)

        # get_batch_usage with a specific day query
        batch_result = await store._d1.query(
            "SELECT user_id, COALESCE(SUM(cost_usd), 0) as cost "
            "FROM user_daily_cost WHERE day = ? AND user_id = ? GROUP BY user_id",
            [test_day, user_id],
        )
        r.check(
            "batch_usage_query",
            len(batch_result.rows) == 1 and abs(batch_result.rows[0]["cost"] - 0.15) < 1e-6,
        )

        # Cleanup test day cost entry (only our test data)
        await store._d1.execute(
            "DELETE FROM user_daily_cost WHERE user_id = ? AND day = ?",
            [user_id, test_day],
        )
        verify = await store._d1.query(
            "SELECT count(*) as cnt FROM user_daily_cost WHERE user_id = ? AND day = ?",
            [user_id, test_day],
        )
        r.check("user_daily_cost_cleanup", verify.rows[0]["cnt"] == 0)

        # ============================================================
        # AUTH SESSIONS TABLE
        # ============================================================
        print("\n  --- auth_sessions ---")
        session_id = f"{_PREFIX}_sess_{secrets.token_hex(4)}"
        refresh_hash = f"rth_{secrets.token_hex(16)}"
        jti = f"jti_{secrets.token_hex(8)}"
        sid = f"sid_{secrets.token_hex(8)}"
        ids["session_id"] = session_id

        await store.create_session(
            session_id=session_id,
            user_id=user_id,
            refresh_token_hash=refresh_hash,
            jti=jti,
            sid=sid,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        r.ok("create_session")

        sess = await store.get_session_by_token_hash(refresh_hash)
        r.check("get_session_by_token_hash", sess is not None and sess["user_id"] == user_id)

        # Rotate session
        new_rth = f"rth_{secrets.token_hex(16)}"
        new_jti = f"jti_{secrets.token_hex(8)}"
        await store.rotate_session(session_id, new_refresh_token_hash=new_rth, new_jti=new_jti)
        sess2 = await store.get_session_by_token_hash(new_rth)
        r.check("rotate_session", sess2 is not None)

        # Revoke session
        await store.revoke_session(session_id)
        sess3 = await store.get_session_by_token_hash(new_rth)
        r.check("revoke_session", sess3 is not None and sess3["revoked"] in (True, 1))

        # Delete user sessions
        await store.delete_user_sessions(user_id)
        sess4 = await store.get_session_by_token_hash(new_rth)
        r.check("delete_user_sessions", sess4 is None)

        # ============================================================
        # EMAIL VERIFICATION TOKENS TABLE
        # ============================================================
        print("\n  --- email_verification_tokens ---")
        ev_token = f"{_PREFIX}_evtoken_{secrets.token_hex(8)}"

        await store.create_verification_token(
            token=ev_token,
            user_id=user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        r.ok("create_verification_token")

        vt = await store.get_verification_token(ev_token)
        r.check("get_verification_token", vt is not None and vt["user_id"] == user_id)

        await store.mark_verification_used(ev_token)
        vt2 = await store.get_verification_token(ev_token)
        r.check("mark_verification_used", vt2 is not None and vt2["used_at"] is not None)

        await store.delete_user_verification_tokens(user_id)
        vt3 = await store.get_verification_token(ev_token)
        r.check("delete_user_verification_tokens", vt3 is None)

        # ============================================================
        # PASSWORD RESET TOKENS TABLE
        # ============================================================
        print("\n  --- password_reset_tokens ---")
        pr_token = f"{_PREFIX}_prtoken_{secrets.token_hex(8)}"

        await store.create_reset_token(
            token=pr_token,
            user_id=user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        r.ok("create_reset_token")

        rt = await store.get_reset_token(pr_token)
        r.check("get_reset_token", rt is not None and rt["user_id"] == user_id)

        await store.mark_reset_used(pr_token)
        rt2 = await store.get_reset_token(pr_token)
        r.check("mark_reset_used", rt2 is not None and rt2["used_at"] is not None)

        await store.delete_user_reset_tokens(user_id)
        rt3 = await store.get_reset_token(pr_token)
        r.check("delete_user_reset_tokens", rt3 is None)

        # ============================================================
        # ADMIN AUDIT LOG TABLE
        # ============================================================
        print("\n  --- admin_audit_log ---")
        await store.log_admin_action(
            admin_ip="10.0.0.1",
            action="staging_test",
            target_user_id=user_id,
            details={"test": True, "prefix": _PREFIX},
            success=True,
        )
        r.ok("log_admin_action")

        total_al, al_rows = await store.list_audit_log(action="staging_test", limit=5)
        r.check("list_audit_log", total_al >= 1 and al_rows[0]["action"] == "staging_test")

        # ============================================================
        # DELETE USER (atomic batch)
        # ============================================================
        print("\n  --- delete_user (atomic) ---")

        # Re-create a key so delete_user has something to revoke
        await store.create_key(
            key_hash=f"del_{secrets.token_hex(16)}",
            key_prefix=f"del_{secrets.token_hex(4)}",
            user_id=user_id,
            account_id=user_id,
        )

        await store.delete_user(user_id, admin_ip="10.0.0.1", admin_id="staging_test")
        deleted_user = await store.get_user_by_id(user_id)
        r.check(
            "delete_user_status", deleted_user is not None and deleted_user["status"] == "deleted"
        )

        # Verify sessions were purged
        sess_after = await store.get_session_by_token_hash(new_rth)
        r.check("delete_user_purged_sessions", sess_after is None)

    finally:
        await client.close()

    return ids


# ---------------------------------------------------------------------------
# Part 2: HTTP flow tests
# ---------------------------------------------------------------------------


async def _test_http(r: _Results, base_url: str) -> None:
    """Test full auth flow via HTTP endpoints."""
    import httpx

    print("\n=== Part 2: HTTP Flow Tests ===\n")

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        # -- Health check
        resp = await http.get("/health")
        r.check("http_health", resp.status_code == 200)
        health_data = resp.json()
        stores = health_data.get("stores", {})
        op_backend = stores.get("operational_store", {}).get("backend", "unknown")
        print(f"         operational_store backend: {op_backend}")
        print(
            f"         log_store backend: {stores.get('log_store', {}).get('backend', 'unknown')}"
        )

        # -- Signup
        signup_data = {
            "email": _TEST_EMAIL,
            "password": _TEST_PASSWORD,
            "user_name": _TEST_USER_NAME,
        }
        resp = await http.post("/auth/signup", json=signup_data)
        signup_ok = r.check(
            "http_signup",
            resp.status_code == 201,
            f"status={resp.status_code} body={resp.text[:200]}",
        )
        if not signup_ok:
            return

        user_id = resp.json()["user_id"]
        print(f"         user_id: {user_id}")

        # -- Login
        resp = await http.post(
            "/auth/login",
            json={
                "email": _TEST_EMAIL,
                "password": _TEST_PASSWORD,
            },
        )
        login_ok = r.check("http_login", resp.status_code == 200, f"status={resp.status_code}")
        if not login_ok:
            return

        access_token = resp.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {access_token}"}

        # -- Get user info
        resp = await http.get("/user/me", headers=auth_headers)
        r.check(
            "http_user_me", resp.status_code == 200 and resp.json()["email"] == _TEST_EMAIL.lower()
        )

        # -- Create API key
        resp = await http.post("/user/api-keys", headers=auth_headers)
        key_ok = r.check(
            "http_create_api_key", resp.status_code == 201, f"status={resp.status_code}"
        )
        api_key = resp.json().get("api_key") if key_ok else None

        if api_key:
            print(f"         api_key prefix: {api_key[:12]}...")

            # -- Chat completion with API key
            resp = await http.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "gpt-4",  # adjust to a model available on staging
                    "messages": [{"role": "user", "content": "Say hello in 3 words"}],
                    "max_tokens": 20,
                },
            )
            # May fail if no model is routed — that's OK, we're testing auth not inference
            if resp.status_code == 200:
                r.ok("http_chat_completion")
            elif resp.status_code == 404:
                r.ok("http_chat_completion (model not found — auth passed)")
            else:
                r.check(
                    "http_chat_completion",
                    resp.status_code in (200, 404),
                    f"status={resp.status_code}",
                )

        # Note: refresh token flow not tested here — httpx doesn't forward cookies
        # automatically the way a browser would. Test manually if needed.

        # -- Get usage
        resp = await http.get("/user/usage?period=today", headers=auth_headers)
        r.check("http_get_usage", resp.status_code == 200)

        # -- Update profile
        resp = await http.patch(
            "/user/profile", headers=auth_headers, json={"user_name": "Staging Test Updated"}
        )
        r.check("http_update_profile", resp.status_code == 200)

        # -- Deep health
        resp = await http.get("/health/deep")
        r.check("http_health_deep", resp.status_code in (200, 503))
        if resp.status_code == 200:
            deep = resp.json()
            r.check("http_deep_has_stores", "stores" in deep)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def _cleanup_user(user_id: str) -> None:
    """Hard-delete a single test user and all related rows created during this run."""
    from serving.config.settings import get_settings
    from serving.storage.d1_client import D1Client

    get_settings.cache_clear()
    settings = get_settings()

    client = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    try:
        print(f"\nAuto-cleaning test user {user_id}...")
        await client.batch(
            [
                ("DELETE FROM email_verification_tokens WHERE user_id = ?", [user_id]),
                ("DELETE FROM password_reset_tokens WHERE user_id = ?", [user_id]),
                ("DELETE FROM auth_sessions WHERE user_id = ?", [user_id]),
                ("DELETE FROM api_keys WHERE user_id = ?", [user_id]),
                ("DELETE FROM admin_audit_log WHERE target_user_id = ?", [user_id]),
                ("DELETE FROM users WHERE id = ?", [user_id]),
            ]
        )
        print("  Cleanup complete.")
    finally:
        await client.close()


async def _cleanup() -> None:
    """Remove all test entities with the staging prefix from D1."""
    from serving.config.settings import get_settings
    from serving.storage.d1_client import D1Client

    get_settings.cache_clear()
    settings = get_settings()

    client = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    try:
        print("Cleaning up test data...")
        # Delete in dependency order
        tables_and_cols = [
            ("email_verification_tokens", "user_id"),
            ("password_reset_tokens", "user_id"),
            ("auth_sessions", "user_id"),
            ("api_keys", "user_id"),
            ("admin_audit_log", "target_user_id"),
        ]
        for table, col in tables_and_cols:
            result = await client.execute(
                f"DELETE FROM {table} WHERE {col} IN "
                f"(SELECT id FROM users WHERE user_name LIKE ? OR email LIKE ?)",
                [f"{_PREFIX}%", f"{_PREFIX}%"],
            )
            print(f"  {table}: {result.changes} rows deleted")

        result = await client.execute(
            "DELETE FROM users WHERE user_name LIKE ? OR email LIKE ?",
            [f"{_PREFIX}%", f"{_PREFIX}%"],
        )
        print(f"  users: {result.changes} rows deleted")

        # Also clean audit log entries from staging_test action
        result = await client.execute(
            "DELETE FROM admin_audit_log WHERE action = 'staging_test'",
        )
        print(f"  admin_audit_log (staging_test): {result.changes} rows deleted")

    finally:
        await client.close()

    print("Cleanup complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    r = _Results()

    if args.cleanup:
        await _cleanup()
        return 0

    ids = {}
    if not args.http_only:
        ids = await _test_store(r)

    if not args.store_only and args.base_url:
        await _test_http(r, args.base_url)
    elif not args.store_only and not args.base_url:
        print("\nSkipping HTTP tests (no --base-url provided)")

    result = r.summary()

    if not args.no_cleanup and ids.get("user_id"):
        await _cleanup_user(ids["user_id"])

    return result


def main() -> None:
    """Parse arguments and run the staging test."""
    parser = argparse.ArgumentParser(description="Staging integration test for D1 backend.")
    parser.add_argument("--base-url", help="Staging server URL (e.g., https://staging.example.com)")
    parser.add_argument("--store-only", action="store_true", help="Only run direct store tests")
    parser.add_argument("--http-only", action="store_true", help="Only run HTTP flow tests")
    parser.add_argument("--cleanup", action="store_true", help="Remove test data from D1")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip auto-cleanup after tests")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
