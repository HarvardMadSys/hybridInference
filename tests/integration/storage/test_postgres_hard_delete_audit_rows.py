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
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from serving.storage.postgres_log import PostgresLogStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "u-purge-owner"
_BYSTANDER = "u-purge-bystander"


@pytest_asyncio.fixture
async def log_store():
    """Provide a PostgresLogStore against the test database, rows wiped."""
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

    store = PostgresLogStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'req-purge-%'")

    await _wipe()
    try:
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
            datetime.now(timezone.utc),
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
