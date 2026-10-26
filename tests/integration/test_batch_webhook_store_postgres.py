"""Completion callback state against Postgres: claiming, attempts and finality.

The delivery itself (HTTP, DNS checks) is unit-tested; this covers the row
transitions the store owns and the migration that adds the webhook columns to a
database from an earlier build.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio

from serving.storage.batch_store import BatchStore, _missing_schema_objects
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

URL = "https://hooks.example.com/batch"


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=4)
    user_id = f"u-bwh-{uuid.uuid4().hex[:10]}"
    batch_store = BatchStore(pool)
    await batch_store.initialize()
    try:
        yield batch_store, pool, user_id
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM batch_jobs WHERE user_id = $1", user_id)
        await pool.close()


async def _create(store: BatchStore, user_id: str, *, webhook_url: str | None = URL) -> str:
    batch_id = f"batch_{uuid.uuid4().hex}"
    await store.create_batch(
        batch_id=batch_id,
        user_id=user_id,
        key_hash=None,
        endpoint="/v1/chat/completions",
        model_ids=["m1"],
        items=[
            {
                "custom_id": "a",
                "model_id": "m1",
                "request": {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            }
        ],
        metadata=None,
        completion_window_seconds=3600,
        webhook_url=webhook_url,
    )
    return batch_id


async def _finish(pool: Any, batch_id: str, status: str = "completed") -> None:
    column = {
        "completed": "completed_at",
        "failed": "failed_at",
        "cancelled": "cancelled_at",
        "expired": "expired_at",
    }[status]
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE batch_jobs SET status = $2, {column} = now() WHERE id = $1",
            batch_id,
            status,
        )
        await conn.execute(
            "UPDATE batch_items SET status = $2, completed_at = now(), "
            "lease_expires_at = NULL WHERE batch_id = $1 "
            "AND status IN ('queued', 'running')",
            batch_id,
            status,
        )


async def _claim(store: BatchStore, worker: str = "worker-1") -> list[dict[str, Any]]:
    return await store.claim_webhook_deliveries(
        worker_id=worker, limit=10, lease_seconds=60, max_attempts=3
    )


async def test_a_batch_that_has_not_finished_is_not_claimable(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)

    assert await _claim(batch_store) == []

    await _finish(pool, batch_id)

    (row,) = await _claim(batch_store)
    assert row["id"] == batch_id
    assert row["webhook_status"] == "delivering"
    assert row["webhook_url"] == URL
    assert row["webhook_attempts"] == 0


async def test_a_batch_without_a_webhook_is_never_claimed(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id, webhook_url=None)
    await _finish(pool, batch_id)

    assert await _claim(batch_store) == []


@pytest.mark.parametrize("status", ["completed", "cancelled", "expired"])
async def test_every_terminal_status_is_delivered(store, status):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)
    await _finish(pool, batch_id, status)

    (row,) = await _claim(batch_store)
    assert row["id"] == batch_id


async def test_a_delivered_callback_is_not_claimed_again(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)
    await _finish(pool, batch_id)
    await _claim(batch_store)

    assert (
        await batch_store.record_webhook_attempt(
            batch_id=batch_id, worker_id="worker-1", status_code=200, error=None
        )
        == 1
    )
    assert await batch_store.finish_webhook_delivery(
        batch_id=batch_id, worker_id="worker-1", delivered=True, max_attempts=3
    )

    assert await _claim(batch_store) == []
    stored = await batch_store.get_batch(batch_id)
    assert stored["webhook_status"] == "delivered"
    assert stored["webhook_delivered_at"] is not None
    assert stored["webhook_last_status_code"] == 200


async def test_running_out_of_attempts_marks_the_callback_failed(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)
    await _finish(pool, batch_id)
    await _claim(batch_store)
    for _ in range(3):
        await batch_store.record_webhook_attempt(
            batch_id=batch_id, worker_id="worker-1", status_code=500, error="HTTP 500"
        )

    assert await batch_store.finish_webhook_delivery(
        batch_id=batch_id, worker_id="worker-1", delivered=False, max_attempts=3
    )

    assert await _claim(batch_store) == []
    stored = await batch_store.get_batch(batch_id)
    assert stored["webhook_status"] == "failed"
    assert stored["webhook_attempts"] == 3
    assert stored["webhook_last_error"] == "HTTP 500"


async def test_an_expired_lease_can_be_claimed_by_another_worker(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)
    await _finish(pool, batch_id)
    await _claim(batch_store, "worker-1")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_jobs SET webhook_lease_expires_at = now() - interval '1 second' "
            "WHERE id = $1",
            batch_id,
        )

    (row,) = await _claim(batch_store, "worker-2")
    assert row["id"] == batch_id
    assert row["webhook_claimed_by"] == "worker-2"


async def test_releasing_hands_the_callback_back_without_spending_an_attempt(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)
    await _finish(pool, batch_id)
    await _claim(batch_store)

    assert await batch_store.release_webhook_delivery(batch_id=batch_id, worker_id="worker-1")

    stored = await batch_store.get_batch(batch_id)
    assert stored["webhook_status"] == "pending"
    assert stored["webhook_attempts"] == 0
    (row,) = await _claim(batch_store)
    assert row["id"] == batch_id


async def test_deleting_the_batch_removes_the_pending_callback(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)
    await _finish(pool, batch_id)

    assert await batch_store.delete_batch(batch_id)

    assert await _claim(batch_store) == []


async def test_the_schema_check_leaves_no_missing_webhook_column(store):
    _batch_store, pool, _user_id = store

    async with pool.acquire() as conn:
        assert await _missing_schema_objects(conn) == set()


async def test_a_failed_batch_with_a_running_item_waits_for_it(store):
    batch_store, pool, user_id = store
    batch_id = await _create(batch_store, user_id)
    await _finish(pool, batch_id, "failed")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_items SET status = 'running', "
            "lease_expires_at = now() + interval '5 minutes' WHERE batch_id = $1",
            batch_id,
        )

    assert await _claim(batch_store) == []

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_items SET status = 'failed', lease_expires_at = NULL WHERE batch_id = $1",
            batch_id,
        )
    (row,) = await _claim(batch_store)
    assert row["id"] == batch_id
