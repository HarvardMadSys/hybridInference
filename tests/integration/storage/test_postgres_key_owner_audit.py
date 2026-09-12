"""Integration tests for ``get_key_owner_for_audit`` (Postgres).

The point of this lookup is the credentials the *authenticating* lookups
deliberately hide: revoked, expired, or belonging to a suspended account. Those
are what the auth-failure blocklist is made of, so each case is asserted against
``get_auth_context_lightweight`` returning nothing for the same hash — the gap
this method exists to close.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a clean PostgresOperationalStore for audit-lookup tests."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE key_prefix LIKE 'sk-audit%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-audit-%'")

    await _wipe()
    try:
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _seed(
    store: PostgresOperationalStore,
    pool: Any,
    *,
    user_id: str,
    key_hash: str,
    user_status: str = "active",
    key_status: str = "active",
    expires_at: datetime | None = None,
) -> None:
    """Create one user and one key for them, in the requested states.

    The key row is inserted directly rather than through ``create_key``: that
    method writes ``api_key_encrypted``, a column this store's own
    ``initialize()`` does not create, so going through it would make these
    tests depend on some other module having built the fuller schema first.
    Only columns ``initialize()`` creates are touched here.
    """
    await store.create_user(
        user_id=user_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        status=user_status,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id, "
            "status, expires_at) VALUES ($1, $2, $3, $3, $4, $5)",
            key_hash,
            "sk-audit",
            user_id,
            key_status,
            expires_at,
        )


async def test_live_key_resolves_and_reads_as_active(postgres_op_store):
    store, pool = postgres_op_store
    await _seed(store, pool, user_id="u-audit-live", key_hash="h-audit-live")

    row = await store.get_key_owner_for_audit("h-audit-live")

    assert row is not None
    assert row["user_id"] == "u-audit-live"
    assert row["key_status"] == "active"
    assert row["key_expired"] is False
    assert row["user_status"] == "active"


async def test_revoked_key_still_names_its_owner(postgres_op_store):
    """The case the blocklist actually produces: a key that was rotated away."""
    store, pool = postgres_op_store
    await _seed(
        store,
        pool,
        user_id="u-audit-revoked",
        key_hash="h-audit-revoked",
        key_status="revoked",
    )

    assert await store.get_auth_context_lightweight("h-audit-revoked") is None

    row = await store.get_key_owner_for_audit("h-audit-revoked")
    assert row is not None
    assert row["user_id"] == "u-audit-revoked"
    assert row["key_status"] == "revoked"


async def test_expired_key_is_reported_as_expired(postgres_op_store):
    """Expiry is compared against the database clock, not the caller's."""
    store, pool = postgres_op_store
    await _seed(
        store,
        pool,
        user_id="u-audit-expired",
        key_hash="h-audit-expired",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    assert await store.get_auth_context_lightweight("h-audit-expired") is None

    row = await store.get_key_owner_for_audit("h-audit-expired")
    assert row is not None
    assert row["key_status"] == "active"
    assert row["key_expired"] is True


async def test_suspended_owner_is_reported_with_a_live_key(postgres_op_store):
    store, pool = postgres_op_store
    await _seed(
        store,
        pool,
        user_id="u-audit-suspended",
        key_hash="h-audit-suspended",
        user_status="suspended",
    )

    assert await store.get_auth_context_lightweight("h-audit-suspended") is None

    row = await store.get_key_owner_for_audit("h-audit-suspended")
    assert row is not None
    assert row["key_status"] == "active"
    assert row["key_expired"] is False
    assert row["user_status"] == "suspended"


async def test_unknown_hash_resolves_to_nothing(postgres_op_store):
    """A scanner's random token names nobody, which is the honest answer."""
    store, _pool = postgres_op_store
    assert await store.get_key_owner_for_audit("h-audit-never-issued") is None
