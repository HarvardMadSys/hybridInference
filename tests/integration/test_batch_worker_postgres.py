"""Batch inference end to end against Postgres: store, per-model caps and two workers.

Only the upstream is stubbed (an adapter behind a real ``FixedRouter``). The rest
is the code production runs: claiming, re-authenticating the owner from the
stored key against the real ``api_keys`` and ``users`` rows, the owner's
batch quota slot, the chat-completions handler, cost accounting into
``user_daily_cost``, and the batch's settlement. Two schedulers sharing one
database stand for two gateway processes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch.model_limits import BatchModelLimits
from serving.batch.scheduler import BatchScheduler, SchedulerTimings
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import AppServices
from serving.storage.batch_store import BatchStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# $1 per 1M prompt tokens and $2 per 1M completion tokens: each stubbed answer
# (100 prompt + 50 completion tokens) costs $0.0002.
_PRICING = {
    "prompt": "1",
    "completion": "2",
    "input_cache_reads": "0",
    "input_cache_writes": "0",
    "image": "0",
    "request": "0",
}
_ITEM_COST = 0.0002

_TIMINGS = SchedulerTimings(
    poll_seconds=0.02,
    idle_poll_seconds=0.1,
    sweep_seconds=0.5,
    heartbeat_seconds=0.2,
    lease_seconds=10,
    max_attempts=3,
    backoff_base_seconds=0.01,
    backoff_cap_seconds=0.02,
    cooldown_failures=100,
    slot_retry_seconds=0.05,
    owner_retry_seconds=0.05,
    role_pause_seconds=0.3,
    stop_grace_seconds=1.0,
)


class _Upstream(BaseAdapter):
    """A model endpoint that echoes the prompt, counting how many requests it holds."""

    def __init__(self, model_id: str) -> None:
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider="openai",
                base_url=f"https://{model_id}.test",
                pricing=_PRICING,
            )
        )
        self.running = 0
        self.peak = 0
        self.calls: Counter[str] = Counter()
        self.gate: asyncio.Event | None = None

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        prompt = messages[-1]["content"]
        self.calls[prompt] += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0.01)
            return self.format_response(
                content=f"echo {prompt}",
                model=self.config.id,
                usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )
        finally:
            self.running -= 1

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("batch items never stream")
        yield


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=10)
    await BatchStore(pool).initialize()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def owner(pool):
    """An internal user with a live API key; everything of theirs is removed afterwards."""
    tag = uuid.uuid4().hex[:10]
    user_id, key_hash = f"u-bwe2e-{tag}", f"hash-bwe2e-{tag}"
    op_store = PostgresOperationalStore(pool)
    await op_store.create_user(
        user_id=user_id, email=f"{tag}@bwe2e.test", password_hash="x", email_verified=True
    )
    await op_store.update_user_fields(user_id, role="internal")
    await op_store.create_key(
        key_hash=key_hash,
        key_prefix=f"sk-bwe2e-{tag}",
        user_id=user_id,
        account_id=user_id,
        quota_daily_cost_usd=Decimal("100.00"),
    )
    try:
        yield {"user_id": user_id, "key_hash": key_hash, "op_store": op_store}
    finally:
        await BatchStore(pool).delete_user_batches(user_id)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest_asyncio.fixture
async def gateway(pool, owner):
    """The services a gateway process holds, around two stubbed models."""
    models = {name: _Upstream(f"{name}-{uuid.uuid4().hex[:6]}") for name in ("a", "b")}
    router = FixedRouter()
    for upstream in models.values():
        router.register_route(upstream.config.id, [(upstream, 1.0)])
    store = BatchStore(pool)
    services = AppServices(
        router=router,
        operational_store=owner["op_store"],
        user_concurrency_limiter=UserConcurrencyLimiter(
            static_limits_provider({"free": 3, "pro": 3, "internal": 50, "admin": 0})
        ),
        batch_store=store,
        batch_model_limits=BatchModelLimits(store, ttl=0.05),
    )
    app = FastAPI()
    app.state.services = services
    workers: list[BatchScheduler] = []

    def start_worker(name: str) -> BatchScheduler:
        worker = BatchScheduler(
            app=app,
            services=services,
            store=store,
            limits=services.batch_model_limits,
            worker_id=f"{name}-{uuid.uuid4().hex[:6]}",
            timings=_TIMINGS,
        )
        worker.start()
        workers.append(worker)
        return worker

    try:
        yield MagicMock(store=store, models=models, services=services, start_worker=start_worker)
    finally:
        for worker in workers:
            await worker.stop()
        for upstream in models.values():
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM batch_model_limits WHERE model_id = $1", upstream.config.id
                )


async def _create(store: BatchStore, owner: dict, items: list[tuple[str, str]]) -> str:
    """Store a batch of ``(custom_id, model_id)`` requests for *owner*; return its id."""
    batch_id = f"batch_{uuid.uuid4().hex}"
    await store.create_batch(
        batch_id=batch_id,
        user_id=owner["user_id"],
        key_hash=owner["key_hash"],
        endpoint="/v1/chat/completions",
        model_ids=sorted({model for _, model in items}),
        items=[
            {
                "custom_id": custom_id,
                "model_id": model,
                "request": {"model": model, "messages": [{"role": "user", "content": custom_id}]},
            }
            for custom_id, model in items
        ],
        metadata=None,
        completion_window_seconds=3600,
    )
    return batch_id


async def _until(predicate, *, timeout: float = 20.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def _status(store: BatchStore, batch_id: str) -> str | None:
    row = await store.get_batch(batch_id)
    return row["status"] if row else None


async def _status_is(store: BatchStore, batch_id: str, status: str) -> bool:
    return await _status(store, batch_id) == status


async def _failed_count_is(store: BatchStore, batch_id: str, count: int) -> bool:
    row = await store.get_batch(batch_id)
    return row is not None and row["status"] == "failed" and row["failed_count"] == count


async def _paused(store: BatchStore, batch_id: str) -> bool:
    row = await store.get_batch(batch_id)
    return row is not None and row["paused_until"] is not None


async def test_two_workers_drain_a_batch_within_the_deployment_wide_cap(gateway, owner):
    """Every request runs exactly once, no model ever exceeds its cap, and spend is counted."""
    store, a, b = gateway.store, gateway.models["a"], gateway.models["b"]
    await store.set_model_limit(a.config.id, 2, "test")
    items = [(f"a{i}", a.config.id) for i in range(12)] + [(f"b{i}", b.config.id) for i in range(6)]
    batch_id = await _create(store, owner, items)

    gateway.start_worker("one")
    gateway.start_worker("two")
    await _until(
        lambda: _status_is(store, batch_id, "completed"), what="the batch to complete", timeout=30
    )

    assert a.peak <= 2, "two workers together must hold model a at its cap"
    assert b.peak <= 4, "model b runs at the default cap"
    assert set(a.calls.values()) == {1} and len(a.calls) == 12
    assert set(b.calls.values()) == {1} and len(b.calls) == 6

    row = await store.get_batch(batch_id)
    assert (row["completed_count"], row["failed_count"]) == (18, 0)
    assert row["cost_usd"] == pytest.approx(18 * _ITEM_COST)
    results = await store.list_results(batch_id, limit=100)
    assert {r["custom_id"]: r["response"]["choices"][0]["message"]["content"] for r in results} == {
        custom_id: f"echo {custom_id}" for custom_id, _ in items
    }
    assert all(r["request_id"] for r in results)
    # The owner was charged as for their own requests.
    async with store._pool.acquire() as conn:
        spent = await conn.fetchval(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM user_daily_cost WHERE user_id = $1",
            owner["user_id"],
        )
    assert float(spent) == pytest.approx(18 * _ITEM_COST)


async def test_an_admin_raises_a_models_cap_while_its_batch_runs(gateway, owner):
    """The per-model cap applies to the very next claim, without restarting anything."""
    store, a = gateway.store, gateway.models["a"]
    await store.set_model_limit(a.config.id, 1, "test")
    a.gate = asyncio.Event()
    batch_id = await _create(store, owner, [(f"a{i}", a.config.id) for i in range(6)])
    gateway.start_worker("one")

    await _until(lambda: a.running == 1, what="one request running")
    await asyncio.sleep(0.2)
    assert a.running == 1, "the cap of 1 holds while the batch waits"

    await store.set_model_limit(a.config.id, 3, "test")
    gateway.services.batch_model_limits.invalidate()
    await _until(lambda: a.running == 3, what="the raised cap to fill")
    assert a.peak == 3

    a.gate.set()
    await _until(lambda: _status_is(store, batch_id, "completed"), what="the batch to complete")


async def test_a_revoked_key_fails_the_batch_without_calling_upstream(gateway, owner):
    store, a = gateway.store, gateway.models["a"]
    batch_id = await _create(store, owner, [(f"a{i}", a.config.id) for i in range(3)])
    await owner["op_store"].revoke_key(owner["user_id"])

    gateway.start_worker("one")
    # The worker claims all three at once. The first to find the key revoked
    # fails the batch while the others may still be claimed; each of those
    # takes the batch's verdict when it hands its claim back, so the count
    # settles a moment after the status flips.
    await _until(lambda: _failed_count_is(store, batch_id, 3), what="the batch to fail")

    row = await store.get_batch(batch_id)
    assert row["status"] == "failed"
    assert row["error"]["code"] == "owner_unauthorized"
    assert {r["error"]["code"] for r in await store.list_results(batch_id)} == {"batch_failed"}
    assert not a.calls


async def test_an_owner_whose_role_drops_is_paused_until_it_returns(gateway, owner):
    store, a = gateway.store, gateway.models["a"]
    await owner["op_store"].update_user_fields(owner["user_id"], role="pro")
    batch_id = await _create(store, owner, [(f"a{i}", a.config.id) for i in range(3)])

    gateway.start_worker("one")
    await _until(lambda: _paused(store, batch_id), what="the owner's batch to be paused")
    assert not a.calls
    assert await _status(store, batch_id) in ("validating", "in_progress")

    await owner["op_store"].update_user_fields(owner["user_id"], role="internal")
    await _until(lambda: _status_is(store, batch_id, "completed"), what="the batch to complete")
    assert len(a.calls) == 3


async def test_a_spent_daily_quota_pauses_the_owner_until_it_renews(gateway, owner):
    store, a = gateway.store, gateway.models["a"]
    await owner["op_store"].update_key(owner["user_id"], quota_daily_cost_usd=Decimal("0.001"))
    async with store._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd) "
            "VALUES ($1, to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD'), 1)",
            owner["user_id"],
        )
    batch_id = await _create(store, owner, [(f"a{i}", a.config.id) for i in range(2)])

    gateway.start_worker("one")
    await _until(lambda: _paused(store, batch_id), what="the batch to be paused")

    row = await store.get_batch(batch_id)
    # Paused until the quota renews at the next UTC midnight.
    assert row["paused_until"].hour == 0 and row["paused_until"].minute == 0
    assert (row["paused_until"] - row["created_at"]).total_seconds() <= 24 * 3600
    assert row["status"] in ("validating", "in_progress")
    assert not a.calls


async def test_cancelling_lets_running_requests_finish_and_drops_the_rest(gateway, owner):
    store, a = gateway.store, gateway.models["a"]
    await store.set_model_limit(a.config.id, 2, "test")
    a.gate = asyncio.Event()
    batch_id = await _create(store, owner, [(f"a{i}", a.config.id) for i in range(6)])
    gateway.start_worker("one")
    await _until(lambda: a.running == 2, what="two requests running")

    row = await store.cancel_batch(batch_id, user_id=owner["user_id"])
    assert row["status"] == "cancelling"

    a.gate.set()
    await _until(lambda: _status_is(store, batch_id, "cancelled"), what="the batch to cancel")
    row = await store.get_batch(batch_id)
    assert row["completed_count"] == 2
    statuses = Counter(r["status"] for r in await store.list_results(batch_id))
    assert statuses == {"completed": 2, "cancelled": 4}
    assert len(a.calls) == 2


async def test_a_dead_workers_requests_are_taken_over_once_their_leases_lapse(gateway, owner):
    store, b = gateway.store, gateway.models["b"]
    batch_id = await _create(store, owner, [(f"b{i}", b.config.id) for i in range(2)])
    claimed = await store.claim_items(
        model_id=b.config.id, worker_id="dead", limit=2, max_running=4, lease_seconds=1
    )
    assert len(claimed) == 2

    gateway.start_worker("one")
    await _until(lambda: _status_is(store, batch_id, "completed"), what="the batch to complete")
    assert set(b.calls.values()) == {1}
