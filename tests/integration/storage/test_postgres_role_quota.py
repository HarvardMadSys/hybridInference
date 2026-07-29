"""Integration tests for Postgres role-quota helpers."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a bare PostgresOperationalStore pointed at the test database.

    Mirrors the pattern in tests/servers/conftest_auth.py (_init_pg_backend)
    but returns the raw PostgresOperationalStore (no cache wrapper) so that
    seed writes are immediately visible to assertions.
    """
    import asyncpg

    # Resolve a per-xdist-worker database name so parallel CI workers don't
    # share a single test DB and race on the bulk DELETEs in setup/teardown.
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run against a non-test database."
        )

    if worker_id != "master":
        # Provision the worker-specific DB if it doesn't exist yet. Idempotent.
        try:
            admin_conn = await asyncpg.connect(
                host=os.getenv("TEST_DB_HOST", "localhost"),
                port=int(os.getenv("TEST_DB_PORT", "5432")),
                user=os.getenv("TEST_DB_USER", "postgres"),
                password=os.getenv("TEST_DB_PASSWORD", "postgres"),
                database="postgres",
                timeout=5,
            )
            try:
                exists = await admin_conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", test_db_name
                )
                if not exists:
                    await admin_conn.execute(f'CREATE DATABASE "{test_db_name}"')
            finally:
                await admin_conn.close()
        except Exception:
            # If postgres isn't reachable here, the create_pool call below
            # will fail and we'll skip the test cleanly.
            pass

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
        return  # unreachable; satisfies type-checker

    store = PostgresOperationalStore(pool)
    await store.initialize()

    # Wipe rows seeded by previous test runs so each test starts clean.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM api_keys WHERE key_prefix LIKE 'sk-%'")
        await conn.execute("DELETE FROM users WHERE id LIKE 'u-pro-%' OR id LIKE 'u-free-%'")

    try:
        yield store
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE key_prefix LIKE 'sk-%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-pro-%' OR id LIKE 'u-free-%'")
        await pool.close()


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------


async def _seed_user_with_key(
    store: PostgresOperationalStore,
    *,
    user_id: str,
    email: str,
    role: str,
    quota: Decimal = Decimal("100.00"),
    status: str = "active",
) -> None:
    """Create a user (default role=free) then patch the role, then add a key."""
    await store.create_user(
        user_id=user_id,
        email=email,
        password_hash="x",
    )
    # create_user always inserts role='free'; update to desired role.
    if role != "free":
        await store.update_user_fields(user_id, role=role)

    await store.create_key(
        key_hash=f"hash-{user_id}",
        key_prefix=f"sk-{user_id[:6]}",
        user_id=user_id,
        account_id=user_id,
        quota_daily_cost_usd=quota,
    )

    # create_key has no status param (rows default to 'active' via the schema);
    # apply a non-active status with a follow-up update_key instead.
    if status != "active":
        await store.update_key(user_id, status=status)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_count_active_keys_for_role(postgres_op_store: PostgresOperationalStore):
    await _seed_user_with_key(postgres_op_store, user_id="u-pro-1", email="p1@x.com", role="pro")
    await _seed_user_with_key(postgres_op_store, user_id="u-pro-2", email="p2@x.com", role="pro")
    await _seed_user_with_key(postgres_op_store, user_id="u-free-1", email="f1@x.com", role="free")
    await _seed_user_with_key(
        postgres_op_store,
        user_id="u-pro-rev",
        email="pr@x.com",
        role="pro",
        status="revoked",
    )

    keys, users = await postgres_op_store.count_active_keys_for_role("pro")
    assert keys == 2
    assert users == 2


async def test_apply_role_quota_updates_only_matching_role(
    postgres_op_store: PostgresOperationalStore,
):
    await _seed_user_with_key(postgres_op_store, user_id="u-pro-1", email="p1@x.com", role="pro")
    await _seed_user_with_key(postgres_op_store, user_id="u-free-1", email="f1@x.com", role="free")

    updated = await postgres_op_store.apply_role_quota("pro", Decimal("250.00"))
    assert updated == 1

    pro_key = await postgres_op_store.get_active_key_by_account("u-pro-1")
    free_key = await postgres_op_store.get_active_key_by_account("u-free-1")
    assert pro_key is not None
    assert free_key is not None
    assert pro_key["quota_daily_cost_usd"] == Decimal("250.00")
    assert free_key["quota_daily_cost_usd"] == Decimal("100.00")


async def test_apply_role_quota_skips_revoked_keys(
    postgres_op_store: PostgresOperationalStore,
):
    await _seed_user_with_key(
        postgres_op_store,
        user_id="u-pro-rev",
        email="pr@x.com",
        role="pro",
        status="revoked",
    )
    updated = await postgres_op_store.apply_role_quota("pro", Decimal("250.00"))
    assert updated == 0


async def test_create_key_persists_encrypted_column(
    postgres_op_store: PostgresOperationalStore,
):
    """create_key writes the api_key_encrypted ciphertext to the row verbatim."""
    await postgres_op_store.create_user(
        user_id="u-free-enc",
        email="enc@x.com",
        password_hash="x",
    )

    ciphertext = "gAAAAA-example-display-ciphertext"
    await postgres_op_store.create_key(
        key_hash="hash-u-free-enc",
        key_prefix="sk-encff",
        user_id="u-free-enc",
        account_id="u-free-enc",
        quota_daily_cost_usd=Decimal("100.00"),
        api_key_encrypted=ciphertext,
    )

    async with postgres_op_store._pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT api_key_encrypted FROM api_keys WHERE user_id = $1",
            "u-free-enc",
        )
    assert stored == ciphertext
