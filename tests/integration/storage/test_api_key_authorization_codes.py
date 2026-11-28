"""Integration tests for api_key_authorization_codes (Postgres).

The unit tests use a fake store that honours single use because it was written
to. What actually stops one approval from becoming two keys is that Postgres
honours it under concurrency, and only a real database can show that.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

CALLBACK = "https://app.example.org/model-connections/callback"
USER = "user-key-authz"


@pytest_asyncio.fixture
async def postgres_op_store():
    """A PostgresOperationalStore with one account and no codes."""
    pool = await postgres.create_pool(min_size=2, max_size=10)
    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def clean() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_key_authorization_codes")
            await conn.execute("DELETE FROM users WHERE id = $1", USER)

    await clean()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, email_verified, status) "
            "VALUES ($1, 'key-authz@example.org', TRUE, 'active')",
            USER,
        )
    try:
        yield store
    finally:
        await clean()
        await pool.close()


async def _issue(store: PostgresOperationalStore, code_hash: str, *, ttl: int = 180) -> None:
    await store.create_api_key_authorization_code(
        code_hash=code_hash,
        user_id=USER,
        callback_url=CALLBACK,
        code_challenge="challenge-value",
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
    )


async def test_consume_returns_the_issued_row(postgres_op_store: PostgresOperationalStore):
    await _issue(postgres_op_store, "hash-round-trip")
    claim = await postgres_op_store.consume_api_key_authorization_code("hash-round-trip")
    assert claim == {
        "user_id": USER,
        "callback_url": CALLBACK,
        "code_challenge": "challenge-value",
    }


async def test_a_claim_stamps_the_row_and_a_second_finds_nothing(
    postgres_op_store: PostgresOperationalStore,
):
    await _issue(postgres_op_store, "hash-single-use")
    assert await postgres_op_store.consume_api_key_authorization_code("hash-single-use")
    assert await postgres_op_store.consume_api_key_authorization_code("hash-single-use") is None
    async with postgres_op_store._pool.acquire() as conn:
        consumed = await conn.fetchval(
            "SELECT consumed_at FROM api_key_authorization_codes WHERE code_hash = $1",
            "hash-single-use",
        )
    assert consumed is not None


async def test_an_expired_or_unknown_code_is_not_claimable(
    postgres_op_store: PostgresOperationalStore,
):
    await _issue(postgres_op_store, "hash-expired", ttl=-1)
    assert await postgres_op_store.consume_api_key_authorization_code("hash-expired") is None
    assert await postgres_op_store.consume_api_key_authorization_code("never-issued") is None


async def test_concurrent_claims_produce_exactly_one_winner(
    postgres_op_store: PostgresOperationalStore,
):
    """Twenty simultaneous exchanges of one code: one key, nineteen refusals."""
    await _issue(postgres_op_store, "hash-race")

    results = await asyncio.gather(
        *(postgres_op_store.consume_api_key_authorization_code("hash-race") for _ in range(20))
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["user_id"] == USER


async def test_issuing_a_code_sweeps_long_expired_rows(
    postgres_op_store: PostgresOperationalStore,
):
    await postgres_op_store.create_api_key_authorization_code(
        code_hash="hash-ancient",
        user_id=USER,
        callback_url=CALLBACK,
        code_challenge="challenge-value",
        expires_at=datetime.now(UTC) - timedelta(hours=6),
    )
    await _issue(postgres_op_store, "hash-fresh")

    async with postgres_op_store._pool.acquire() as conn:
        left = {
            row["code_hash"]
            for row in await conn.fetch("SELECT code_hash FROM api_key_authorization_codes")
        }
    assert left == {"hash-fresh"}


async def test_codes_leave_with_their_account(postgres_op_store: PostgresOperationalStore):
    await _issue(postgres_op_store, "hash-owned")
    async with postgres_op_store._pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", USER)
        left = await conn.fetchval("SELECT COUNT(*) FROM api_key_authorization_codes")
    assert left == 0
