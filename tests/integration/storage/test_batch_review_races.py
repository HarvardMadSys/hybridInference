"""Independent PostgreSQL regressions for batch lifecycle and fair scheduling."""

import asyncio
import uuid
from collections import Counter

import pytest
import pytest_asyncio

from serving.storage.batch_store import BatchStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=6)
    store = BatchStore(pool)
    await store.initialize()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE batch_jobs, batch_idempotency, batch_user_state CASCADE")
    try:
        yield store
    finally:
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE batch_jobs, batch_idempotency, batch_user_state CASCADE")
        await pool.close()


async def create(store, *, user, model, count, **kwargs):
    return await store.create_batch(
        batch_id=f"batch_{uuid.uuid4().hex}",
        user_id=user,
        key_hash=None,
        endpoint="/v1/chat/completions",
        model_ids=[model],
        items=[
            {"custom_id": str(i), "model_id": model, "request": {"model": model}}
            for i in range(count)
        ],
        metadata=None,
        completion_window_seconds=3600,
        **kwargs,
    )


async def test_first_sweep_after_retention_does_not_leave_immortal_idempotency(store):
    batch = await create(
        store,
        user="review-idem",
        model="review-model",
        count=0,
        retention_seconds=60,
        idempotency={"key": "review-key", "digest": "review-digest"},
    )
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_jobs SET sealed_at = now() - interval '2 days', "
            "completed_at = now() - interval '2 days' WHERE id = $1",
            batch["id"],
        )
    result = await store.sweep(retention_days=7)
    assert result["purged"] == 1
    replay = await store.find_idempotent(
        owner_id="review-idem", api_surface="openai_inline", key="review-key"
    )
    assert replay is None, "An expired job must not leave an unlimited idempotency tombstone"


@pytest.mark.parametrize(
    "unsupported",
    [
        {"schema_version": 3},
        {"api_surface": "future_surface"},
        {"target": "future_target"},
    ],
)
async def test_sweep_does_not_purge_jobs_outside_worker_supported_contract(store, unsupported):
    """An old worker cannot know the cleanup contract of a newer job kind."""
    batch = await create(
        store, user="review-future", model="review-model", count=0, retention_seconds=60
    )
    column, value = next(iter(unsupported.items()))
    async with store.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE batch_jobs SET {column} = $2, "
            "sealed_at = now() - interval '2 days', "
            "completed_at = now() - interval '2 days' WHERE id = $1",
            batch["id"],
            value,
        )
    await store.sweep(retention_days=7)
    assert await store.batch_exists(batch["id"]), (
        f"The worker purged an unsupported job: {unsupported}"
    )


async def test_odd_sized_claim_waves_rotate_extra_slot_between_users(store):
    model = "review-fair-model"
    for owner in ("review-a", "review-b"):
        await create(store, user=owner, model=model, count=30)
    served = Counter()
    for _ in range(6):
        claimed = await store.claim_items(
            model_id=model,
            worker_id="review-worker",
            limit=3,
            max_running=3,
            lease_seconds=60,
        )
        assert len(claimed) == 3
        for item in claimed:
            served[item["user_id"]] += 1
            assert await store.complete_item(
                item_id=item["id"],
                worker_id="review-worker",
                attempt=item["attempt_seq"],
                response={"ok": True},
                error=None,
                status_code=200,
                cost_usd=0,
            )
    assert abs(served["review-a"] - served["review-b"]) <= 1, served


async def test_read_lease_cannot_succeed_behind_gc_deletion_snapshot(store):
    """Pause GC after choosing its victim, then register a concurrent reader."""
    batch = await create(
        store,
        user="review-reader",
        model="review-read-model",
        count=0,
        retention_seconds=60,
    )
    lock_id = 611492381
    async with store.pool.acquire() as gate, store.pool.acquire() as deleting:
        await gate.execute(
            "UPDATE batch_jobs SET sealed_at = now() - interval '2 days' WHERE id = $1",
            batch["id"],
        )
        await gate.execute(
            f"CREATE OR REPLACE FUNCTION review_gc_barrier() RETURNS trigger LANGUAGE plpgsql "
            f"AS $$ BEGIN PERFORM pg_advisory_xact_lock({lock_id}); RETURN OLD; END $$"
        )
        await gate.execute(
            "CREATE TRIGGER review_gc_barrier BEFORE DELETE ON batch_jobs "
            "FOR EACH ROW EXECUTE FUNCTION review_gc_barrier()"
        )
        await gate.execute("SELECT pg_advisory_lock($1)", lock_id)
        from serving.storage.batch_store import _PURGE_ONE_SQL

        deletion = asyncio.create_task(deleting.fetchval(_PURGE_ONE_SQL, 7))
        lease = None
        try:
            for _ in range(100):
                blocked = await gate.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = $1 "
                    "AND wait_event = 'advisory'",
                    deleting.get_server_pid(),
                )
                if blocked:
                    break
                await asyncio.sleep(0.01)
            assert blocked, "Deletion must be paused after its no-reader snapshot"
            lease = asyncio.create_task(store.take_read_lease(batch_id=batch["id"], ttl_seconds=60))
            await asyncio.wait({lease}, timeout=0.1)
        finally:
            await gate.execute("SELECT pg_advisory_unlock($1)", lock_id)
            await deletion
            await gate.execute("DROP TRIGGER review_gc_barrier ON batch_jobs")
            await gate.execute("DROP FUNCTION review_gc_barrier()")
        result = (await asyncio.gather(lease, return_exceptions=True))[0]
        assert isinstance(result, Exception) or await store.batch_exists(batch["id"]), (
            "A successfully acquired read lease must protect its resource; GC removed it"
        )
