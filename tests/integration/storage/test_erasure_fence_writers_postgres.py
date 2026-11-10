"""A stream that outlives its account's hard delete writes back no Responses row and no cost.

Against PostgreSQL: a tombstone in ``erasure_fence`` keeps the account's rows out
of ``openai_responses``, ``user_daily_cost`` and ``api_key_daily_cost``; and a
write that arrives while the hard delete holds the account's exclusive fence
lock waits for it, then sees the tombstone, exactly as an ``api_logs`` write does.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from serving.storage.base import CostIncrement
from serving.storage.erasure_fence import establish_erasure_fence, fence_account_digest
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.storage.responses_store import ResponseStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

SECRET = postgres.FENCE_SECRET
DAY = "2026-11-08"


@pytest_asyncio.fixture
async def world():
    pool = await postgres.create_pool(min_size=1, max_size=4)
    tag = uuid.uuid4().hex[:10]
    erased, kept = f"u-fenced-{tag}", f"u-kept-{tag}"
    try:
        yield pool, erased, kept
    finally:
        async with pool.acquire() as conn:
            users = [erased, kept]
            await conn.execute("DELETE FROM openai_responses WHERE user_id = ANY($1)", users)
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id = ANY($1)", users)
            await conn.execute("DELETE FROM api_key_daily_cost WHERE user_id = ANY($1)", users)
            await conn.execute(
                "DELETE FROM erasure_fence WHERE account_digest = $1",
                fence_account_digest(erased, SECRET),
            )
        await pool.close()


async def _tombstone(pool, user_id: str) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await establish_erasure_fence(conn, fence_key=fence_account_digest(user_id, SECRET))


async def _save(store: ResponseStore, user_id: str) -> str:
    rid = f"resp_{uuid.uuid4().hex}"
    await store.save(
        response_id=rid,
        user_id=user_id,
        response={"id": rid},
        messages=[{"role": "user", "content": "still streaming when erased"}],
    )
    return rid


async def _stored(pool, user_id: str) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM openai_responses WHERE user_id = $1", user_id
        )


async def _costs(pool, user_id: str) -> tuple[int, int]:
    async with pool.acquire() as conn:
        return (
            await conn.fetchval("SELECT count(*) FROM user_daily_cost WHERE user_id = $1", user_id),
            await conn.fetchval(
                "SELECT count(*) FROM api_key_daily_cost WHERE user_id = $1", user_id
            ),
        )


async def test_an_erased_account_gets_no_stored_response(world):
    pool, erased, kept = world
    await _tombstone(pool, erased)
    store = ResponseStore(pool, fence_secret=SECRET)

    await _save(store, erased)
    await _save(store, kept)

    assert (await _stored(pool, erased), await _stored(pool, kept)) == (0, 1)


async def test_an_erased_accounts_cost_is_left_out_of_a_batch_and_the_rest_written(world):
    pool, erased, kept = world
    await _tombstone(pool, erased)
    store = PostgresOperationalStore(pool, fence_secret=SECRET)

    await store.increment_user_costs(
        [
            CostIncrement(erased, DAY, 7_001, Decimal("0.25"), 1),
            CostIncrement(kept, DAY, 7_002, Decimal("0.50"), 2),
        ]
    )

    assert await _costs(pool, erased) == (0, 0)
    assert await _costs(pool, kept) == (1, 1)


async def test_a_write_racing_the_hard_delete_waits_for_it_then_sees_the_tombstone(world):
    """The hard delete holds the account's exclusive fence lock until it commits."""
    pool, erased, _ = world
    responses = ResponseStore(pool, fence_secret=SECRET)
    counters = PostgresOperationalStore(pool, fence_secret=SECRET)
    hard_delete = await postgres.connect()
    deleting = hard_delete.transaction()
    await deleting.start()
    committed = False
    try:
        await establish_erasure_fence(hard_delete, fence_key=fence_account_digest(erased, SECRET))
        save = asyncio.ensure_future(_save(responses, erased))
        cost = asyncio.ensure_future(
            counters.increment_user_costs([CostIncrement(erased, DAY, None, Decimal("1"), 1)])
        )
        await asyncio.sleep(0.3)
        # Both wait on the lock: neither has written, neither has finished.
        assert not save.done() and not cost.done()
        await deleting.commit()
        committed = True
        await asyncio.wait_for(asyncio.gather(save, cost), 10)
    finally:
        if not committed:
            await deleting.rollback()
        await hard_delete.close()

    assert await _stored(pool, erased) == 0
    assert await _costs(pool, erased) == (0, 0)
