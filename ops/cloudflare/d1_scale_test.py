#!/usr/bin/env python3
"""Scale test for the D1 backend.

Tests D1's ability to handle concurrent load for operational workloads:
  1. Bulk account creation (N users + API keys)
  2. Concurrent auth lookups (the verify_api_key hot path)
  3. Concurrent mixed reads/writes (sessions, tokens, updates)
  4. Correctness validation (every created entity is retrievable)

Usage:
    # Quick test (100 users, 100 concurrent auth lookups)
    python ops/cloudflare/d1_scale_test.py --users 100 --concurrency 20

    # Full scale test (1000 users, 1000 concurrent auth lookups)
    python ops/cloudflare/d1_scale_test.py --users 1000 --concurrency 50

    # Auth hot-path only (skip user creation, use existing data)
    python ops/cloudflare/d1_scale_test.py --auth-only --users 1000 --concurrency 50

    # Clean up after test
    python ops/cloudflare/d1_scale_test.py --cleanup

Environment:
    D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "apps" / "backend"))

from dotenv import load_dotenv

load_dotenv()

_PREFIX = "d1scale"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_key(key: str) -> str:
    """Hash an API key using the same logic as serving.servers.auth."""
    from serving.servers.auth import hash_api_key

    return hash_api_key(key)


def _percentile(data: list[float], p: int) -> float:
    """Return the p-th percentile of a sorted list."""
    if not data:
        return 0.0
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])


def _print_latency_stats(label: str, latencies: list[float]) -> None:
    """Print latency distribution for a set of operations."""
    if not latencies:
        print(f"  {label}: no data")
        return
    latencies.sort()
    total = len(latencies)
    ok_count = total  # all completed if we got here
    mean = statistics.mean(latencies)
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)
    max_val = max(latencies)
    total_time = sum(latencies)

    print(f"  {label}:")
    print(f"    count:    {ok_count}")
    print(f"    mean:     {mean * 1000:.1f}ms")
    print(f"    p50:      {p50 * 1000:.1f}ms")
    print(f"    p95:      {p95 * 1000:.1f}ms")
    print(f"    p99:      {p99 * 1000:.1f}ms")
    print(f"    max:      {max_val * 1000:.1f}ms")
    print(f"    total:    {total_time:.1f}s")


# ---------------------------------------------------------------------------
# Phase 1: Bulk account creation
# ---------------------------------------------------------------------------


async def _create_users(
    store: Any,
    count: int,
    concurrency: int,
) -> list[dict[str, str]]:
    """Create N users with API keys. Returns list of {user_id, email, key, key_hash}."""
    sem = asyncio.Semaphore(concurrency)
    users: list[dict[str, str]] = []
    errors: list[str] = []
    latencies: list[float] = []

    async def _create_one(i: int) -> None:
        async with sem:
            uid = f"{_PREFIX}_{i:06d}_{secrets.token_hex(4)}"
            email = f"{uid}@scale.test"
            key = f"hyi-{secrets.token_urlsafe(32)}"
            key_hash = _hash_key(key)
            key_prefix = key[:12]

            t0 = time.monotonic()
            try:
                await store.create_user(
                    user_id=uid,
                    email=email,
                    password_hash=f"$argon2id$fakehash_{i}",
                    user_name=f"{_PREFIX}_user_{i}",
                    email_verified=True,
                    status="active",
                )
                await store.create_key(
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    user_id=uid,
                    user_name=f"{_PREFIX}_user_{i}",
                    tier="free",
                    quota_daily_cost_usd=100.0,
                    account_id=uid,
                )
                users.append(
                    {
                        "user_id": uid,
                        "email": email,
                        "key": key,
                        "key_hash": key_hash,
                    }
                )
                latencies.append(time.monotonic() - t0)
            except Exception as exc:
                latencies.append(time.monotonic() - t0)
                errors.append(f"user {i}: {exc}")

    print(f"\n=== Phase 1: Create {count} users + API keys (concurrency={concurrency}) ===\n")
    wall_start = time.monotonic()

    tasks = [_create_one(i) for i in range(count)]
    await asyncio.gather(*tasks)

    wall_time = time.monotonic() - wall_start
    print(f"  Created: {len(users)}/{count} ({len(errors)} errors)")
    print(f"  Wall time: {wall_time:.1f}s ({count / wall_time:.0f} users/sec)")
    if errors:
        print("  First 5 errors:")
        for e in errors[:5]:
            print(f"    {e}")
    _print_latency_stats("create_user + create_key", latencies)

    return users


# ---------------------------------------------------------------------------
# Phase 2: Concurrent auth lookups
# ---------------------------------------------------------------------------


async def _auth_lookups(
    store: Any,
    cached_store: Any,
    users: list[dict[str, str]],
    concurrency: int,
) -> None:
    """Run concurrent get_auth_context_by_key_hash — the verify_api_key hot path."""
    sem = asyncio.Semaphore(concurrency)
    cold_latencies: list[float] = []
    warm_latencies: list[float] = []
    errors: list[str] = []
    mismatches: list[str] = []

    async def _lookup_cold(u: dict[str, str]) -> None:
        """Cold lookup (no cache)."""
        async with sem:
            t0 = time.monotonic()
            try:
                ctx = await store.get_auth_context_by_key_hash(u["key_hash"])
                cold_latencies.append(time.monotonic() - t0)
                if ctx is None:
                    mismatches.append(f"{u['user_id']}: auth context is None")
                elif ctx["user_id"] != u["user_id"]:
                    mismatches.append(f"{u['user_id']}: got user_id={ctx['user_id']}")
            except Exception as exc:
                cold_latencies.append(time.monotonic() - t0)
                errors.append(f"{u['user_id']}: {exc}")

    async def _lookup_warm(u: dict[str, str]) -> None:
        """Warm lookup (through cache)."""
        async with sem:
            t0 = time.monotonic()
            try:
                ctx = await cached_store.get_auth_context_by_key_hash(u["key_hash"])
                warm_latencies.append(time.monotonic() - t0)
                if ctx is None:
                    mismatches.append(f"{u['user_id']}: cached auth context is None")
            except Exception as exc:
                warm_latencies.append(time.monotonic() - t0)
                errors.append(f"{u['user_id']}: {exc}")

    count = len(users)
    print(f"\n=== Phase 2: Auth lookups x{count} (concurrency={concurrency}) ===\n")

    # Cold (uncached)
    print("  Cold (direct D1)...")
    wall_start = time.monotonic()
    await asyncio.gather(*[_lookup_cold(u) for u in users])
    cold_wall = time.monotonic() - wall_start
    print(f"  Wall time: {cold_wall:.1f}s ({count / cold_wall:.0f} lookups/sec)")
    _print_latency_stats("cold auth lookup", cold_latencies)

    if mismatches:
        print(f"  MISMATCHES ({len(mismatches)}):")
        for m in mismatches[:5]:
            print(f"    {m}")

    # Warm (cached) — prime cache first, then measure
    print("\n  Warm (cached)...")
    for u in users:
        await cached_store.get_auth_context_by_key_hash(u["key_hash"])

    wall_start = time.monotonic()
    await asyncio.gather(*[_lookup_warm(u) for u in users])
    warm_wall = time.monotonic() - wall_start
    print(f"  Wall time: {warm_wall:.1f}s ({count / warm_wall:.0f} lookups/sec)")
    _print_latency_stats("warm auth lookup (cached)", warm_latencies)

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for e in errors[:5]:
            print(f"    {e}")


# ---------------------------------------------------------------------------
# Phase 3: Mixed concurrent operations
# ---------------------------------------------------------------------------


async def _mixed_operations(
    store: Any,
    users: list[dict[str, str]],
    concurrency: int,
) -> None:
    """Run concurrent mixed reads and writes across all tables."""
    sem = asyncio.Semaphore(concurrency)
    latencies: dict[str, list[float]] = {
        "get_user_by_id": [],
        "get_user_by_email": [],
        "update_user_last_login": [],
        "create_session": [],
        "get_session": [],
        "create_token": [],
        "list_users": [],
    }
    errors: list[str] = []

    async def _mixed_one(u: dict[str, str], i: int) -> None:
        async with sem:
            uid = u["user_id"]
            try:
                # Read user by ID
                t0 = time.monotonic()
                await store.get_user_by_id(uid)
                latencies["get_user_by_id"].append(time.monotonic() - t0)

                # Read user by email
                t0 = time.monotonic()
                await store.get_user_by_email(u["email"])
                latencies["get_user_by_email"].append(time.monotonic() - t0)

                # Update last login
                t0 = time.monotonic()
                await store.update_user_last_login(uid)
                latencies["update_user_last_login"].append(time.monotonic() - t0)

                # Create + read session
                sid = f"{_PREFIX}_sess_{i}_{secrets.token_hex(4)}"
                rth = f"rth_{secrets.token_hex(16)}"
                t0 = time.monotonic()
                await store.create_session(
                    session_id=sid,
                    user_id=uid,
                    refresh_token_hash=rth,
                    jti=f"jti_{secrets.token_hex(8)}",
                    sid=f"sid_{secrets.token_hex(8)}",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
                latencies["create_session"].append(time.monotonic() - t0)

                t0 = time.monotonic()
                await store.get_session_by_token_hash(rth)
                latencies["get_session"].append(time.monotonic() - t0)

                # Create verification token
                t0 = time.monotonic()
                await store.create_verification_token(
                    token=f"{_PREFIX}_tok_{i}_{secrets.token_hex(8)}",
                    user_id=uid,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                )
                latencies["create_token"].append(time.monotonic() - t0)

            except Exception as exc:
                errors.append(f"user {i}: {exc}")

    # Also run some list queries concurrently
    async def _list_query() -> None:
        async with sem:
            t0 = time.monotonic()
            await store.list_users(search=_PREFIX, limit=50)
            latencies["list_users"].append(time.monotonic() - t0)

    count = len(users)
    print(f"\n=== Phase 3: Mixed operations x{count} (concurrency={concurrency}) ===\n")

    wall_start = time.monotonic()
    tasks = [_mixed_one(u, i) for i, u in enumerate(users)]
    # Add some list queries
    tasks.extend([_list_query() for _ in range(min(count // 10, 50))])
    await asyncio.gather(*tasks)
    wall_time = time.monotonic() - wall_start

    print(f"  Wall time: {wall_time:.1f}s")
    for op_name, lats in latencies.items():
        _print_latency_stats(op_name, lats)

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for e in errors[:5]:
            print(f"    {e}")


# ---------------------------------------------------------------------------
# Phase 4: Correctness validation
# ---------------------------------------------------------------------------


async def _validate(store: Any, users: list[dict[str, str]]) -> None:
    """Verify every created user and key is retrievable and correct."""
    print(f"\n=== Phase 4: Correctness validation ({len(users)} users) ===\n")
    missing_users = 0
    missing_keys = 0
    wrong_email = 0

    for u in users:
        user = await store.get_user_by_id(u["user_id"])
        if user is None:
            missing_users += 1
            continue
        if user["email"] != u["email"]:
            wrong_email += 1

        ctx = await store.get_auth_context_by_key_hash(u["key_hash"])
        if ctx is None:
            missing_keys += 1

    total = len(users)
    print(f"  Users found:    {total - missing_users}/{total}")
    print(f"  Keys found:     {total - missing_keys}/{total}")
    print(f"  Email correct:  {total - wrong_email}/{total}")

    if missing_users or missing_keys or wrong_email:
        print("  STATUS: FAIL")
    else:
        print("  STATUS: OK")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def _cleanup_run(store: Any, user_ids: list[str]) -> None:
    """Remove only the users created in this test run."""
    print(f"  Deleting {len(user_ids)} test users from this run...")
    batch_size = 50
    for i in range(0, len(user_ids), batch_size):
        batch_ids = user_ids[i : i + batch_size]
        placeholders = ",".join(["?"] * len(batch_ids))
        await store._d1.batch(
            [
                (
                    f"DELETE FROM email_verification_tokens WHERE user_id IN ({placeholders})",
                    batch_ids,
                ),
                (f"DELETE FROM password_reset_tokens WHERE user_id IN ({placeholders})", batch_ids),
                (f"DELETE FROM auth_sessions WHERE user_id IN ({placeholders})", batch_ids),
                (f"DELETE FROM api_keys WHERE user_id IN ({placeholders})", batch_ids),
                (
                    f"DELETE FROM admin_audit_log WHERE target_user_id IN ({placeholders})",
                    batch_ids,
                ),
                (f"DELETE FROM users WHERE id IN ({placeholders})", batch_ids),
            ]
        )
        print(f"  Deleted batch {i + 1}-{i + len(batch_ids)}")
    print("  Cleanup complete.")


async def _cleanup(store: Any) -> None:
    """Remove all scale test data (use --cleanup flag)."""
    print("Cleaning up scale test data...")

    # Get all test user IDs
    result = await store._d1.query(
        "SELECT id FROM users WHERE user_name LIKE ?",
        [f"{_PREFIX}%"],
    )
    user_ids = [r["id"] for r in result.rows]
    print(f"  Found {len(user_ids)} test users")

    if not user_ids:
        print("  Nothing to clean up.")
        return

    # Delete in batches to avoid hitting D1 limits
    batch_size = 50
    for i in range(0, len(user_ids), batch_size):
        batch_ids = user_ids[i : i + batch_size]
        placeholders = ",".join(["?"] * len(batch_ids))

        await store._d1.batch(
            [
                (
                    f"DELETE FROM email_verification_tokens WHERE user_id IN ({placeholders})",
                    batch_ids,
                ),
                (f"DELETE FROM password_reset_tokens WHERE user_id IN ({placeholders})", batch_ids),
                (f"DELETE FROM auth_sessions WHERE user_id IN ({placeholders})", batch_ids),
                (f"DELETE FROM api_keys WHERE user_id IN ({placeholders})", batch_ids),
                (
                    f"DELETE FROM admin_audit_log WHERE target_user_id IN ({placeholders})",
                    batch_ids,
                ),
                (f"DELETE FROM users WHERE id IN ({placeholders})", batch_ids),
            ]
        )
        print(f"  Deleted batch {i + 1}-{i + len(batch_ids)}")

    print("  Cleanup complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    from serving.config.settings import get_settings
    from serving.storage.cache import CachedOperationalStore, InMemoryCache
    from serving.storage.d1_client import D1Client
    from serving.storage.d1_operational import D1OperationalStore

    get_settings.cache_clear()
    settings = get_settings()

    if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
        print("ERROR: D1 credentials missing.", file=sys.stderr)
        return 1

    client = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    if not await client.health_check():
        print("ERROR: D1 unreachable.", file=sys.stderr)
        await client.close()
        return 1

    store = D1OperationalStore(client)
    cached = CachedOperationalStore(store, InMemoryCache())

    try:
        if args.cleanup:
            await _cleanup(store)
            return 0

        # Ensure schema exists
        await store.initialize()

        if args.auth_only:
            # Use existing users
            result = await client.query(
                "SELECT u.id AS user_id, u.email, k.key_hash "
                "FROM users u JOIN api_keys k ON k.user_id = u.id "
                "WHERE u.user_name LIKE ? AND k.status = 'active' "
                "LIMIT ?",
                [f"{_PREFIX}%", args.users],
            )
            users = [
                {"user_id": r["user_id"], "email": r["email"], "key_hash": r["key_hash"], "key": ""}
                for r in result.rows
            ]
            if not users:
                print("No existing test users found. Run without --auth-only first.")
                return 1
            print(f"Found {len(users)} existing test users")
        else:
            users = await _create_users(store, args.users, args.concurrency)

        if users:
            await _auth_lookups(store, cached, users, args.concurrency)
            await _mixed_operations(store, users, args.concurrency)
            await _validate(store, users)

        if not args.no_cleanup and users:
            print("\nAuto-cleaning test data...")
            await _cleanup_run(store, [u["user_id"] for u in users])

    finally:
        await client.close()

    return 0


def main() -> None:
    """Parse arguments and run the scale test."""
    parser = argparse.ArgumentParser(description="D1 scale test.")
    parser.add_argument("--users", type=int, default=100, help="Number of users (default: 100)")
    parser.add_argument(
        "--concurrency", type=int, default=20, help="Max concurrent D1 requests (default: 20)"
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Skip creation, test auth lookups with existing data",
    )
    parser.add_argument("--cleanup", action="store_true", help="Remove all scale test data")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip auto-cleanup after tests")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
