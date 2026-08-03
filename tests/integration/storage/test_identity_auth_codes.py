"""Integration tests for identity_auth_codes (Postgres).

The unit tests use a fake store that honours single-use because it was written
to. The property that actually protects the flow is that *Postgres* honours it
under concurrency, and only a real database can show that.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"

REDIRECT = "https://agents.staging.freeinference.org/auth/callback"


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a clean PostgresOperationalStore for authorization code tests."""
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
        pool = await asyncpg.create_pool(**db_config, min_size=2, max_size=10)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM identity_auth_codes")

    try:
        yield store
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM identity_auth_codes")
        await pool.close()


async def _issue(store: PostgresOperationalStore, code_hash: str, *, ttl: int = 60) -> None:
    await store.create_identity_auth_code(
        code_hash=code_hash,
        user_id="user_1",
        client_id="cloud-agent",
        redirect_uri=REDIRECT,
        code_challenge="challenge-value",
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
    )


async def test_consume_returns_the_issued_row(postgres_op_store: PostgresOperationalStore):
    await _issue(postgres_op_store, "hash-round-trip")
    claim = await postgres_op_store.consume_identity_auth_code("hash-round-trip")
    assert claim == {
        "user_id": "user_1",
        "client_id": "cloud-agent",
        "redirect_uri": REDIRECT,
        "code_challenge": "challenge-value",
    }


async def test_a_second_consume_finds_nothing(postgres_op_store: PostgresOperationalStore):
    await _issue(postgres_op_store, "hash-single-use")
    assert await postgres_op_store.consume_identity_auth_code("hash-single-use") is not None
    assert await postgres_op_store.consume_identity_auth_code("hash-single-use") is None


async def test_an_expired_code_is_not_claimable(postgres_op_store: PostgresOperationalStore):
    await _issue(postgres_op_store, "hash-expired", ttl=-1)
    assert await postgres_op_store.consume_identity_auth_code("hash-expired") is None


async def test_an_unknown_code_is_not_claimable(postgres_op_store: PostgresOperationalStore):
    assert await postgres_op_store.consume_identity_auth_code("never-issued") is None


async def test_concurrent_consumes_produce_exactly_one_winner(
    postgres_op_store: PostgresOperationalStore,
):
    """The reason consume is one statement rather than read-then-mark.

    Twenty simultaneous exchanges of the same code must yield one token and
    nineteen refusals. Read-then-mark passes the sequential tests above and
    fails this one.
    """
    await _issue(postgres_op_store, "hash-race")

    results = await asyncio.gather(
        *(postgres_op_store.consume_identity_auth_code("hash-race") for _ in range(20))
    )

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one claim, got {len(winners)}"
    assert winners[0]["user_id"] == "user_1"


async def test_creating_a_code_sweeps_long_expired_rows(
    postgres_op_store: PostgresOperationalStore,
):
    """Housekeeping rides on issuance so the table needs no scheduled job."""
    await postgres_op_store.create_identity_auth_code(
        code_hash="hash-ancient",
        user_id="user_1",
        client_id="cloud-agent",
        redirect_uri=REDIRECT,
        code_challenge="challenge-value",
        expires_at=datetime.now(UTC) - timedelta(hours=6),
    )
    await _issue(postgres_op_store, "hash-fresh")

    # Gone from the table entirely, not merely unclaimable.
    assert await postgres_op_store.consume_identity_auth_code("hash-ancient") is None
    assert await postgres_op_store.consume_identity_auth_code("hash-fresh") is not None
