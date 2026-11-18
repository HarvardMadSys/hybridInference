"""A CNY ledger end to end through the batch worker, against Postgres.

Each test owns a throwaway database the schema migrations built, holds
synthetic USD usage, is converted by the offline migration (the operator step,
run here only on that disposable database) and then has its policy loaded and
bound exactly as gateway startup does. The real ``BatchScheduler`` then runs
items through the shared protocol executions, so what is held is the code
production runs: native charges reach ``user_daily_cost.cost_amount``, the
batch's ``cost_usd`` stays the USD equivalent of exactly those charges, the
owner's daily quota is read from the native counter, and startup neither
re-converts persisted amounts nor runs the migration again.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI

from ops.accounting_currency import migrate_currency
from routing.routers import FixedRouter
from serving.accounting import (
    MIGRATION_KEY,
    POLICY_KEY,
    AccountingPolicy,
    bind_accounting_policy,
    get_accounting_policy,
    load_accounting_policy,
)
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch.model_limits import BatchModelLimits
from serving.batch.scheduler import BatchScheduler, SchedulerTimings
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import AppServices
from serving.storage.batch_store import BatchStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# Synthetic, deliberately non-1 rate; never a real or approved rate.
POLICY = AccountingPolicy("CNY", Decimal("7.25"), "2026-10-24", "synthetic-test-fixture")
# $1 / $2 per million prompt / completion tokens: 100 + 50 tokens cost $0.0002.
_PRICING = {
    "prompt": "1",
    "completion": "2",
    "input_cache_reads": "0",
    "input_cache_writes": "0",
    "image": "0",
    "request": "0",
}
_ITEM_USD = Decimal("0.0002")
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
    def __init__(self, model_id: str) -> None:
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider="openai",
                base_url=f"https://{model_id}.test",
                # The gateway's own server, so batch work may use it (batch
                # routes are local ones by default).
                deployment_type="self_hosted",
                pricing=_PRICING,
            )
        )
        self.calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls += 1
        return self.format_response(
            content="ok",
            model=self.config.id,
            usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("batch items never stream")
        yield


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


@pytest_asyncio.fixture
async def cny_database():
    """A disposable migrated database with synthetic USD history, converted to CNY."""
    base = postgres.target().name
    name = f"{base}_cny_{uuid.uuid4().hex[:10]}"
    await postgres.ensure_database(name)
    previous = get_accounting_policy()
    pool = None
    try:
        # The schema migrations, as a deploy applies them before the gateway starts.
        await postgres.prepare_schema(name)
        pool = await postgres.create_pool(database=name, min_size=1, max_size=10)
        user_id, key_hash = "cny-batch-owner", "cny-batch-key-hash"
        op_store = PostgresOperationalStore(pool)
        await op_store.create_user(
            user_id=user_id, email="owner@cny-batch.test", password_hash="x", email_verified=True
        )
        await op_store.update_user_fields(user_id, role="internal")
        await op_store.create_key(
            key_hash=key_hash,
            key_prefix="sk-cny-batch",
            user_id=user_id,
            account_id=user_id,
            quota_daily_cost_usd=Decimal("10.00"),
        )
        # Historical USD usage from before the migration (yesterday's row).
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) VALUES "
                "($1, to_char((NOW() AT TIME ZONE 'UTC') - INTERVAL '1 day', 'YYYY-MM-DD'), 2, 1)",
                user_id,
            )
        await pool.close()
        pool = None
        # The offline operator step, on this disposable database only.
        conn = await postgres.connect(database=name)
        try:
            result = await migrate_currency(conn, POLICY, apply=True)
        finally:
            await conn.close()
        assert result["applied"] and result["migration_complete"]
        pool = await postgres.create_pool(database=name, min_size=1, max_size=10)
        op_store = PostgresOperationalStore(pool)
        yield {"pool": pool, "op_store": op_store, "user_id": user_id, "key_hash": key_hash}
    finally:
        bind_accounting_policy(previous)
        if pool is not None:
            await pool.close()
        admin = await postgres.connect_maintenance()
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def _snapshot(pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {
            "policy": await conn.fetchval(
                "SELECT value FROM site_settings WHERE key = $1", POLICY_KEY
            ),
            "marker": await conn.fetchval(
                "SELECT value FROM site_settings WHERE key = $1", MIGRATION_KEY
            ),
            "migrations": await conn.fetchval(
                "SELECT count(*) FROM accounting_currency_migrations"
            ),
            "daily": [
                tuple(row)
                for row in await conn.fetch(
                    "SELECT user_id, day, cost_usd, cost_amount, accounting_currency "
                    "FROM user_daily_cost ORDER BY user_id, day"
                )
            ],
            "keys": [
                tuple(row)
                for row in await conn.fetch(
                    "SELECT user_id, quota_daily_cost_usd, quota_daily_cost_amount, "
                    "accounting_currency FROM api_keys ORDER BY user_id"
                )
            ],
        }


async def _startup(pool, op_store) -> None:
    """What gateway startup does with the database: verify, read-only, then load and bind.

    Startup issues no DDL and converts nothing: it checks the migrated schema
    and the native accounting schema, then loads the persisted policy.
    """
    from migrations.compat import require_schema
    from serving.storage.accounting_schema import require_accounting_schema

    await require_schema(pool)
    await require_accounting_schema(pool)
    policy = await load_accounting_policy(op_store)
    assert policy == POLICY
    bind_accounting_policy(policy)


def _gateway(db: dict[str, Any], upstream: _Upstream) -> tuple[BatchStore, Any]:
    router = FixedRouter()
    router.register_route(upstream.config.id, [(upstream, 1.0)])
    store = BatchStore(db["pool"])
    services = AppServices(
        router=router,
        operational_store=db["op_store"],
        user_concurrency_limiter=UserConcurrencyLimiter(
            static_limits_provider({"free": 3, "pro": 3, "internal": 50, "admin": 0})
        ),
        batch_store=store,
        batch_model_limits=BatchModelLimits(store, ttl=0.05),
    )
    app = FastAPI()
    app.state.services = services

    def start() -> BatchScheduler:
        worker = BatchScheduler(
            app=app,
            services=services,
            store=store,
            limits=services.batch_model_limits,
            worker_id=f"cny-{uuid.uuid4().hex[:6]}",
            timings=_TIMINGS,
        )
        worker.start()
        return worker

    return store, start


async def _create(store: BatchStore, db: dict[str, Any], model: str, count: int) -> str:
    batch_id = f"batch_{uuid.uuid4().hex}"
    await store.create_batch(
        batch_id=batch_id,
        user_id=db["user_id"],
        key_hash=db["key_hash"],
        endpoint="/v1/chat/completions",
        model_ids=[model],
        items=[
            {
                "custom_id": f"c{i}",
                "model_id": model,
                "request": {"model": model, "messages": [{"role": "user", "content": f"c{i}"}]},
            }
            for i in range(count)
        ],
        metadata=None,
        completion_window_seconds=3600,
    )
    return batch_id


async def test_startup_keeps_migrated_amounts_and_never_reconverts(cny_database):
    pool, op_store = cny_database["pool"], cny_database["op_store"]
    before = await _snapshot(pool)
    assert json.loads(before["policy"]) == POLICY.as_dict()
    # The migration converted the historical USD row once, at the policy rate.
    ((_, _, usd, amount, currency),) = before["daily"]
    assert (usd, amount, currency) == (Decimal(2), Decimal(2) * POLICY.usd_to_currency_rate, "CNY")
    ((_, quota_usd, quota_amount, _),) = before["keys"]
    assert quota_amount == quota_usd * POLICY.usd_to_currency_rate

    # Two startups: the schema initialization and policy load are idempotent
    # reads; nothing is converted again and no migration is recorded.
    await _startup(pool, op_store)
    await _startup(pool, op_store)
    assert await _snapshot(pool) == before

    # The operator tool's preview recognizes the exact completed policy.
    async with pool.acquire() as conn:
        again = await migrate_currency(conn, POLICY, apply=False)
    assert again["previous_policy"] == again["target_policy"] == POLICY.as_dict()
    assert again["already_applied"] is True and again["applied"] is False
    assert await _snapshot(pool) == before


async def test_batch_items_charge_the_native_ledger_and_record_usd(cny_database):
    pool, op_store = cny_database["pool"], cny_database["op_store"]
    await _startup(pool, op_store)
    upstream = _Upstream(f"cny-model-{uuid.uuid4().hex[:6]}")
    store, start = _gateway(cny_database, upstream)
    batch_id = await _create(store, cny_database, upstream.config.id, 3)

    worker = start()
    try:
        await _until(lambda: _completed(store, batch_id), what="the batch to complete", timeout=30)
    finally:
        await worker.stop()

    # Background billing tasks settle shortly after the items do.
    expected_native = 3 * _ITEM_USD * POLICY.usd_to_currency_rate
    await _until(
        lambda: _native_today(pool, cny_database["user_id"], expected_native),
        what="the native charges to land",
    )
    assert upstream.calls == 3
    async with pool.acquire() as conn:
        today = await conn.fetchrow(
            "SELECT cost_usd, cost_amount, accounting_currency, requests FROM user_daily_cost "
            "WHERE user_id = $1 AND day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
            cny_database["user_id"],
        )
        job_usd = await conn.fetchval("SELECT cost_usd FROM batch_jobs WHERE id = $1", batch_id)
        item_usd = await conn.fetch(
            "SELECT cost_usd, cost_status FROM batch_items WHERE batch_id = $1", batch_id
        )
    # Native primary amount, with its USD equivalent maintained alongside.
    assert today["accounting_currency"] == "CNY"
    assert today["cost_amount"] == expected_native
    assert today["cost_usd"] == pytest.approx(3 * _ITEM_USD, abs=Decimal("1e-8"))
    # The batch records the USD equivalent of exactly those native charges.
    assert Decimal(str(job_usd)) == pytest.approx(3 * _ITEM_USD, abs=Decimal("1e-8"))
    assert [row["cost_status"] for row in item_usd] == ["known"] * 3
    assert all(
        Decimal(str(row["cost_usd"])) == pytest.approx(_ITEM_USD, abs=Decimal("1e-8"))
        for row in item_usd
    )
    assert await op_store.get_user_cost_today_amount(cny_database["user_id"]) == expected_native


async def test_a_spent_native_quota_pauses_the_batch_owner(cny_database):
    pool, op_store = cny_database["pool"], cny_database["op_store"]
    await _startup(pool, op_store)
    # Today's native counter is spent past the 72.5 CNY quota while its legacy
    # USD column says nothing was spent: only a native read can see it.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, cost_amount, requests) "
            "VALUES ($1, to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD'), 0, 100, 1)",
            cny_database["user_id"],
        )
    assert await op_store.get_user_cost_today(cny_database["user_id"]) == 0
    upstream = _Upstream(f"cny-model-{uuid.uuid4().hex[:6]}")
    store, start = _gateway(cny_database, upstream)
    batch_id = await _create(store, cny_database, upstream.config.id, 2)

    worker = start()
    try:
        await _until(lambda: _paused(store, batch_id), what="the batch to be paused")
    finally:
        await worker.stop()
    row = await store.get_batch(batch_id)
    assert row["paused_reason"] == "quota"
    assert upstream.calls == 0


async def _completed(store: BatchStore, batch_id: str) -> bool:
    row = await store.get_batch(batch_id)
    return row is not None and row["status"] == "completed"


async def _paused(store: BatchStore, batch_id: str) -> bool:
    row = await store.get_batch(batch_id)
    return row is not None and row["paused_until"] is not None


async def _native_today(pool, user_id: str, expected: Decimal) -> bool:
    async with pool.acquire() as conn:
        amount = await conn.fetchval(
            "SELECT cost_amount FROM user_daily_cost "
            "WHERE user_id = $1 AND day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
            user_id,
        )
    return amount is not None and amount >= expected
