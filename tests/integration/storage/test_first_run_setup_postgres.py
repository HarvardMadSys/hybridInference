"""First-run setup against PostgreSQL: the claim, the marker and email-less users.

The unit tests model the advisory lock; this runs the real thing, so two
claims racing on separate connections can only create one administrator.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_MARKER = "setup_completed_at"
_CODE = "setup_code"


async def _wipe(pool) -> None:
    """Empty the tables setup reads: no users, no marker, no stored code."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM email_verification_tokens")
        await conn.execute("DELETE FROM password_reset_tokens")
        await conn.execute("DELETE FROM auth_sessions")
        await conn.execute("DELETE FROM api_keys WHERE account_id IS NOT NULL")
        await conn.execute("DELETE FROM signup_allowed_domains")
        await conn.execute("DELETE FROM users")
        await conn.execute(
            "DELETE FROM site_settings WHERE key = ANY($1::text[])", [_MARKER, _CODE]
        )
        await conn.execute("DELETE FROM admin_audit_log WHERE action = 'setup.admin_created'")


@pytest_asyncio.fixture
async def setup_store():
    """A PostgresOperationalStore on an empty users table, without a marker.

    The schema is built the way bootstrap builds it, ``DatabaseLogger`` first
    and the operational store second, so both users DDL copies are exercised.
    """
    from serving.storage.database import DatabaseLogger

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
    db_logger = DatabaseLogger(db_config)
    try:
        # Its pool (up to 10 connections) lets every claim below hold its own.
        await db_logger.initialize()
    except Exception as exc:
        await db_logger.cleanup()
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    pool = db_logger.pool
    store = PostgresOperationalStore(pool)
    await store.initialize()
    await _wipe(pool)
    try:
        yield store, pool
    finally:
        await _wipe(pool)
        await db_logger.cleanup()


def _claim(store: PostgresOperationalStore, n: int):
    return store.create_first_admin(
        user_id=f"u-setup-{n}",
        login_name=f"Admin{n}",
        password_hash="argon2-hash",
        user_name=f"Admin {n}",
        marker_key=_MARKER,
        marker_value=f"2026-10-10T12:00:0{n}+00:00",
        code_key=_CODE,
        admin_ip="203.0.113.7",
    )


def _code(store: PostgresOperationalStore, candidate: str):
    return store.get_or_create_setup_code(
        marker_key=_MARKER,
        code_key=_CODE,
        completed_at="2026-10-10T12:00:00+00:00",
        candidate_code=candidate,
    )


async def test_email_column_admits_null(setup_store):
    _store, pool = setup_store
    async with pool.acquire() as conn:
        not_null = await conn.fetchval(
            "SELECT attnotnull FROM pg_attribute "
            "WHERE attrelid = 'users'::regclass AND attname = 'email'"
        )
    assert not_null is False


async def test_concurrent_claims_create_exactly_one_admin(setup_store):
    store, pool = setup_store

    results = await asyncio.gather(*(_claim(store, n) for n in range(5)))

    assert sorted(results) == [False] * 4 + [True]
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT * FROM users")
        marker = await conn.fetchrow("SELECT * FROM site_settings WHERE key = $1", _MARKER)
        audit = await conn.fetch(
            "SELECT * FROM admin_audit_log WHERE action = 'setup.admin_created'"
        )
    assert len(users) == 1
    admin = users[0]
    assert admin["email"] is None
    assert admin["login_name"] == admin["login_name"].lower()
    assert admin["role"] == "admin"
    assert admin["status"] == "active"
    assert admin["email_verified"] is True
    winner = int(admin["id"].rsplit("-", 1)[1])
    assert marker["value"] == f"2026-10-10T12:00:0{winner}+00:00"
    assert marker["updated_by"] == f"admin{winner}"
    assert len(audit) == 1
    assert audit[0]["target_user_id"] == admin["id"]
    details = audit[0]["details"]
    details = json.loads(details) if isinstance(details, str) else details
    assert details["login_name"] == f"admin{winner}"


async def test_claim_refused_once_users_exist(setup_store):
    store, _pool = setup_store
    await store.create_user(user_id="u-setup-existing", email="a@example.com", password_hash="h")

    assert await _claim(store, 1) is False
    assert await store.get_setting(_MARKER) is None


async def test_login_name_lookup_and_uniqueness(setup_store):
    import asyncpg

    store, _pool = setup_store
    assert await _claim(store, 1) is True

    row = await store.get_user_by_login_name("ADMIN1")
    assert row is not None
    assert row["id"] == "u-setup-1"
    assert row["email"] is None
    assert (await store.get_user_by_id("u-setup-1"))["login_name"] == "admin1"

    with pytest.raises(asyncpg.UniqueViolationError):
        await store.create_user(
            user_id="u-setup-dup", email=None, password_hash="h", login_name="Admin1"
        )


async def test_one_code_shared_and_kept_until_setup_completes(setup_store):
    store, _pool = setup_store

    codes = await asyncio.gather(*(_code(store, f"CANDIDATE{n}") for n in range(5)))
    assert len(set(codes)) == 1
    # A restart (another candidate) gets the same code back.
    assert await _code(store, "RESTARTCODE2") == codes[0]

    assert await _claim(store, 1) is True

    assert await store.get_setting(_CODE) is None
    assert await _code(store, "LATECANDIDAT") is None
    assert await store.get_setting(_CODE) is None


async def test_existing_users_record_the_marker_and_drop_the_code(setup_store):
    store, _pool = setup_store
    stored = await _code(store, "PENDINGCODE2")
    assert stored == "PENDINGCODE2"

    await store.create_user(user_id="u-setup-existing", email="a@example.com", password_hash="h")

    assert await _code(store, "ANOTHERCODE2") is None
    marker = await store.get_setting(_MARKER)
    assert marker["value"] == "2026-10-10T12:00:00+00:00"
    assert marker["updated_by"] == "existing-users"
    assert await store.get_setting(_CODE) is None
    # Settled: answered without touching the marker again.
    assert await _code(store, "THIRDCODE234") is None
    assert (await store.get_setting(_MARKER))["value"] == "2026-10-10T12:00:00+00:00"
