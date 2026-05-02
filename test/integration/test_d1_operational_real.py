"""Live Cloudflare D1 integration tests for D1OperationalStore.

Requires real credentials in the environment (or .env):
    D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN

Run with:
    make test-d1
    uv run pytest -vv -m d1

All tests are isolated via a unique TEST_RUN_ID prefix and clean up after
themselves regardless of pass/fail.  They are excluded from the standard
`make test` suite.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

pytestmark = pytest.mark.d1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _require_d1_env() -> tuple[str, str, str]:
    """Return (account_id, database_id, api_token) or skip the test."""
    account_id = os.getenv("D1_ACCOUNT_ID")
    database_id = os.getenv("D1_DATABASE_ID")
    api_token = os.getenv("D1_API_TOKEN")
    if not all([account_id, database_id, api_token]):
        pytest.skip("D1_ACCOUNT_ID / D1_DATABASE_ID / D1_API_TOKEN not set")
    return account_id, database_id, api_token  # type: ignore[return-value]


@pytest_asyncio.fixture
async def d1_client():
    from serving.storage.d1_client import D1Client

    account_id, database_id, api_token = _require_d1_env()
    client = D1Client(account_id=account_id, database_id=database_id, api_token=api_token)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def store(d1_client):
    from serving.storage.d1_operational import D1OperationalStore

    s = D1OperationalStore(d1_client)
    await s.initialize()
    return s


@pytest.fixture
def run_id() -> str:
    """Unique prefix for all test data created in this run, enabling safe cleanup."""
    return f"d1test_{uuid.uuid4().hex[:10]}_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _cleanup_users(store, *user_ids: str) -> None:
    """Best-effort cleanup — ignore errors so teardown never masks a test failure."""
    for uid in user_ids:
        with contextlib.suppress(Exception):
            await store.delete_user(uid, admin_ip="127.0.0.1", admin_id="test-cleanup")


async def _cleanup_cost_rows(d1_client, *user_ids: str) -> None:
    for uid in user_ids:
        with contextlib.suppress(Exception):
            await d1_client.execute("DELETE FROM user_daily_cost WHERE user_id = ?", [uid])


async def _cleanup_orphan_key(d1_client, key_hash: str) -> None:
    with contextlib.suppress(Exception):
        await d1_client.execute("DELETE FROM api_keys WHERE key_hash = ?", [key_hash])


# ---------------------------------------------------------------------------
# Tests: active user counts (validates strftime datetime comparison on D1)
# ---------------------------------------------------------------------------


class TestActiveUserCounts:
    """Verify get_active_user_counts executes valid SQL against real D1."""

    async def test_counts_include_recently_logged_in_user(self, store, run_id):
        uid = f"{run_id}dau"
        try:
            await store.create_user(
                user_id=uid,
                email=f"{uid}@d1test.invalid",
                password_hash="x",
                user_name="D1 DAU Test",
            )
            await store.update_user_last_login(uid)

            counts = await store.get_active_user_counts()

            assert isinstance(counts["total"], int)
            assert isinstance(counts["dau"], int)
            assert isinstance(counts["mau"], int)
            assert counts["total"] >= 1
            assert counts["dau"] >= 1, (
                "User logged in just now must appear in DAU — "
                "strftime datetime comparison may be broken"
            )
            assert counts["mau"] >= 1
        finally:
            await _cleanup_users(store, uid)


# ---------------------------------------------------------------------------
# Tests: auth context queries (validates JOIN condition + expiry check)
# ---------------------------------------------------------------------------


class TestAuthContext:
    """Verify get_auth_context_by_key_hash behaviour against real D1."""

    async def _create_user_and_key(self, store, run_id: str, suffix: str = "u1"):
        uid = f"{run_id}{suffix}"
        await store.create_user(
            user_id=uid,
            email=f"{uid}@d1test.invalid",
            password_hash="x",
            user_name="Auth Test",
        )
        await store.create_key(
            key_hash=f"{run_id}kh_{suffix}",
            key_prefix=f"{run_id[:14]}{suffix}",
            user_id=uid,
            user_name="Auth Test",
            quota_daily_cost_usd=Decimal("100.00"),
            account_id=uid,
        )
        return uid

    async def test_valid_key_returns_context(self, store, run_id):
        uid = await self._create_user_and_key(store, run_id)
        try:
            ctx = await store.get_auth_context_by_key_hash(f"{run_id}kh_u1")
            assert ctx is not None
            assert ctx["user_id"] == uid
        finally:
            await _cleanup_users(store, uid)

    async def test_revoked_key_returns_none(self, store, run_id):
        uid = await self._create_user_and_key(store, run_id, suffix="rev")
        try:
            await store.revoke_key(uid)
            ctx = await store.get_auth_context_by_key_hash(f"{run_id}kh_rev")
            assert ctx is None
        finally:
            await _cleanup_users(store, uid)

    async def test_orphan_key_returns_none(self, store, d1_client, run_id):
        """Key with no matching user row must not authenticate.

        This directly validates the fix from `u.id IS NULL OR u.status = 'active'`
        → `u.id IS NOT NULL AND u.status = 'active'`.
        """
        orphan_hash = f"{run_id}orphan_kh"
        orphan_user_id = f"{run_id}ghost_user"
        try:
            await d1_client.execute(
                "INSERT INTO api_keys "
                "(key_hash, key_prefix, user_id, user_name, status, "
                " quota_daily_cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, 'active', 10.0, ?)",
                [
                    orphan_hash,
                    f"{run_id[:8]}orp",
                    orphan_user_id,
                    "Ghost",
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ],
            )
            ctx = await store.get_auth_context_by_key_hash(orphan_hash)
            assert ctx is None, (
                "Orphan key (no matching user row) must not authenticate — "
                "check LEFT JOIN condition in get_auth_context_by_key_hash"
            )
        finally:
            await _cleanup_orphan_key(d1_client, orphan_hash)

    async def test_suspended_user_key_returns_none(self, store, run_id):
        uid = await self._create_user_and_key(store, run_id, suffix="sus")
        try:
            await store.update_user_fields(uid, status="suspended")
            ctx = await store.get_auth_context_by_key_hash(f"{run_id}kh_sus")
            assert ctx is None
        finally:
            await _cleanup_users(store, uid)


# ---------------------------------------------------------------------------
# Tests: get_batch_usage (validates chunking + SQL correctness on real D1)
# ---------------------------------------------------------------------------


class TestBatchUsage:
    """Verify get_batch_usage executes correctly against real D1."""

    async def test_batch_usage_returns_correct_costs(self, store, d1_client, run_id):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        u1, u2 = f"{run_id}cost1", f"{run_id}cost2"
        try:
            await store.increment_user_cost(u1, 3.00, day=today)
            await store.increment_user_cost(u1, 2.00, day=today)
            await store.increment_user_cost(u2, 7.50, day=today)

            result = await store.get_batch_usage([u1, u2, f"{run_id}absent"], period="today")

            assert result[u1] == pytest.approx(5.00, rel=1e-4)
            assert result[u2] == pytest.approx(7.50, rel=1e-4)
            assert f"{run_id}absent" not in result
        finally:
            await _cleanup_cost_rows(d1_client, u1, u2)

    async def test_batch_usage_over_99_ids_chunks_correctly(self, store, d1_client, run_id):
        """100 user IDs must be split into two queries without hitting D1's param limit."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        user_ids = [f"{run_id}cu{i:03d}" for i in range(100)]
        try:
            # Insert cost rows in batches to stay within D1 batch limits
            for uid in user_ids:
                await store.increment_user_cost(uid, 1.0, day=today)

            result = await store.get_batch_usage(user_ids, period="today")

            assert len(result) == 100, f"expected 100 results, got {len(result)}"
            for uid in user_ids:
                assert uid in result, f"{uid} missing from batch result"
                assert result[uid] == pytest.approx(1.0, rel=1e-4)
        finally:
            await _cleanup_cost_rows(d1_client, *user_ids)
