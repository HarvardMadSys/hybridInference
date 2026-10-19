"""An agent job's spend and usage come from the gateway's own ledger.

``get_agent_job_cost`` is the authority for a job's budget and
``get_agent_job_usage`` for what it consumed: both sum ``api_logs`` by
``agent_job_id``, so a job's numbers never depend on what the agent inside the
sandbox reports about itself. The grant routes are tested with them mocked;
here their SQL runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def ledger():
    pool = await postgres.create_pool(min_size=1, max_size=2)
    store = PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'req-jobledger-%'")

    await _wipe()
    rows = [
        ("a1", "job-a", "0.25", 100, 40),
        ("a2", "job-a", "0.50", 300, 60),
        ("b1", "job-b", "9.00", 999, 999),  # another job's spend
        ("n1", None, "7.00", 500, 500),  # the same user, not through a job
    ]
    async with pool.acquire() as conn:
        for suffix, job, cost, tokens_in, tokens_out in rows:
            await conn.execute(
                "INSERT INTO api_logs (request_id, model_id, provider, timestamp, user_id, "
                "agent_job_id, cost_usd, prompt_tokens, completion_tokens) "
                "VALUES ($1, 'm', 'p', $2, 'u-jobledger', $3, $4, $5, $6)",
                f"req-jobledger-{suffix}",
                datetime.now(UTC),
                job,
                Decimal(cost),
                tokens_in,
                tokens_out,
            )
    try:
        yield store
    finally:
        await _wipe()
        await pool.close()


async def test_a_jobs_cost_is_its_own_rows_only(ledger):
    assert await ledger.get_agent_job_cost("job-a") == pytest.approx(0.75)
    assert await ledger.get_agent_job_cost("job-b") == pytest.approx(9.0)


async def test_a_jobs_usage_is_its_own_rows_only(ledger):
    assert await ledger.get_agent_job_usage("job-a") == {
        "tokens_in": 400.0,
        "tokens_out": 100.0,
        "calls": 2.0,
    }


async def test_a_job_with_no_rows_has_spent_nothing(ledger):
    assert await ledger.get_agent_job_cost("job-unknown") == 0.0
    assert await ledger.get_agent_job_usage("job-unknown") == {
        "tokens_in": 0.0,
        "tokens_out": 0.0,
        "calls": 0.0,
    }
