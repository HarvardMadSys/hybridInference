"""Integration test for the hard-delete purge of audit-owner rows (Postgres).

``api_logs`` names an account two ways. ``user_id`` is the caller the gateway
authenticated. ``metadata->>'credential_owner_id'`` is the account behind a key
presented on a rejection path that identified the caller without authenticating
them, and those rows carry a null ``user_id`` deliberately -- so a purge keyed
on ``user_id`` alone would leave a deleted account's identifier readable in the
admin request view.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_OWNER = "u-purge-owner"
_BYSTANDER = "u-purge-bystander"


@pytest_asyncio.fixture
async def log_store():
    """Provide a PostgresLogStore against the test database, rows wiped."""
    pool = await postgres.create_pool(min_size=1, max_size=5)

    # The suite-wide secret (tests/fixtures/postgres.py): the fence pins the
    # first one it sees into a database other files share.
    store = PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'req-purge-%'")
            await conn.execute("DELETE FROM erasure_fence")
            await conn.execute("DELETE FROM erasure_fence_metadata")
            await conn.execute(
                "DELETE FROM users WHERE id LIKE 'req-purge-%' OR id LIKE 'u-purge-%'"
            )

    async def _seed_deleted_user(uid: str) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, status) "
                "VALUES ($1, $1 || '@example.com', 'x', 'deleted') "
                "ON CONFLICT (id) DO UPDATE SET status = 'deleted'",
                uid,
            )

    await _wipe()
    try:
        await _seed_deleted_user(_OWNER)
        await _seed_deleted_user(_BYSTANDER)
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _seed(pool, *, request_id: str, user_id: str | None, owner_id: str | None) -> None:
    metadata = {"rejection": True}
    if owner_id is not None:
        metadata["credential_owner_id"] = owner_id
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, timestamp, "
            "status_code, user_id, metadata) "
            "VALUES ($1, 'm', 'p', $2, 429, $3, $4::jsonb)",
            request_id,
            datetime.now(UTC),
            user_id,
            json.dumps(metadata),
        )


async def _request_ids(pool) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT request_id FROM api_logs WHERE request_id LIKE 'req-purge-%'"
        )
    return {r["request_id"] for r in rows}


async def test_hard_delete_reaches_rows_that_name_the_owner_in_metadata(log_store):
    """Both spellings of the account go; another account's rows stay."""
    store, pool = log_store
    await _seed(pool, request_id="req-purge-authed", user_id=_OWNER, owner_id=None)
    await _seed(pool, request_id="req-purge-audit", user_id=None, owner_id=_OWNER)
    await _seed(pool, request_id="req-purge-other", user_id=None, owner_id=_BYSTANDER)

    counts = await store.hard_delete_user_data(_OWNER)

    assert counts["api_logs"] == 2
    assert await _request_ids(pool) == {"req-purge-other"}


async def test_the_purge_can_use_the_credential_owner_index(log_store):
    """The metadata predicate must be one the planner can match to the index.

    The partial index exists for this one query, and its predicate is written
    as ``IS NOT NULL`` precisely so an equality lookup can be proved to imply
    it. If that proof fails the index is unusable and the purge scans the
    largest table in the deployment while holding a transaction open.

    Planned with sequential scans disabled: on a table this size a seq scan is
    genuinely cheaper, so the question here is whether the index *can* serve
    the predicate, not which plan wins on three rows.
    """
    _store, pool = log_store
    await _seed(pool, request_id="req-purge-audit", user_id=None, owner_id=_OWNER)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        plan = await conn.fetch(
            "EXPLAIN DELETE FROM api_logs "
            "WHERE user_id = $1 OR metadata->>'credential_owner_id' = $1",
            _OWNER,
        )
    rendered = [row["QUERY PLAN"] for row in plan]
    assert any("idx_api_logs_credential_owner" in line for line in rendered), rendered
