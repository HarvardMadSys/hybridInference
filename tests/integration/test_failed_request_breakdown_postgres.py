"""The DB failed-request alerter's breakdown, against PostgreSQL.

The sample it posts to Slack or WeCom is the newest failure's type and status,
both read from the same row, and never the error text, which an upstream can
fill with the caller's own request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.admin.failed_request_alerter import fetch_failure_breakdown
from serving.storage.database import DatabaseLogger
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return postgres.worker_dsn()


@pytest_asyncio.fixture
async def pool(pg_dsn: str):
    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    async with logger.pool.acquire() as connection:
        await connection.execute("TRUNCATE TABLE api_logs")
    try:
        yield logger.pool
    finally:
        async with logger.pool.acquire() as connection:
            await connection.execute("TRUNCATE TABLE api_logs")
        await logger.cleanup()


async def _insert(pool, request_id: str, *, at: datetime, status: int, error: str | None) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO api_logs (request_id, model_id, provider, timestamp, status_code, error)
            VALUES ($1, 'gpt-4o', 'openai', $2, $3, $4)
            """,
            request_id,
            at,
            status,
            error,
        )


async def test_the_sample_is_the_newest_failures_type_and_status_without_its_text(pool):
    now = datetime.now(UTC)
    await _insert(pool, "older", at=now - timedelta(minutes=2), status=500, error="KeyError: 'x'")
    # Two failures in the same instant: the later row is the sample, for both fields.
    tied = now - timedelta(seconds=30)
    await _insert(
        pool,
        "tied-first",
        at=tied,
        status=502,
        error="ServerDisconnectedError: upstream went away",
    )
    await _insert(
        pool,
        "tied-second",
        at=tied,
        status=400,
        error=(
            "ClientResponseError: 400, message='Bad Request' | upstream_body="
            "please summarise my diagnosis: patient Jane Roe"
        ),
    )
    await _insert(pool, "ok", at=now, status=200, error=None)

    breakdown = await fetch_failure_breakdown(pool, 5)

    assert breakdown["status_codes"] == "400, 500, 502"
    assert breakdown["sample_error_type"] == "ClientResponseError"
    assert breakdown["sample_status"] == 400
    assert "sample_error" not in breakdown
    assert "Jane Roe" not in repr(breakdown)
