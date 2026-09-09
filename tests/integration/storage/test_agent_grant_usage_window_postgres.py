"""PostgreSQL tests for the grant usage window queries.

The route tests in ``tests/unit/test_agent_grants.py`` cover the report's
shape against a fake ledger. What they cannot show is that the SQL selects
the right rows, and its predicate is exactly the kind that reads fine while
being wrong: an indexed column for the access path, the grant read back from
JSONB for the exact match, a cast of a JSONB timestamp for the window, and a
row-value comparison for the page cursor. These tests write rows through the
real ``log_request`` — with the same attribution helper the surfaces call — and
read them back through the real queries.

Connection: ``TEST_PG_DSN`` when set, otherwise the ``DB_HOST`` / ``DB_PORT`` /
``DB_NAME`` / ``DB_USER`` / ``DB_PASSWORD`` variables CI exports for its
``postgres`` service. Skipped when no database is reachable.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import pytest_asyncio

from serving.grant_auth import ledger_attribution
from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.utils import calculate_cost
from serving.utils.token_utils import normalize_usage
from tests.fixtures.auth_helpers import assert_test_db_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

JOB = "thread:athr_1"
GRANT = "agr_1"
OTHER_GRANT = "agr_2"
PRICING = {"prompt": "1", "completion": "2"}

# The window closes a minute before the rows are written, so every row's
# insert ``timestamp`` is later than ``until``. Rows are placed by the start
# time the surfaces record, which is the only bound that must decide the
# upper edge: a request that started inside the window and was logged after
# it closed belongs to it.
NOW = datetime.now(UTC)
SINCE = NOW - timedelta(minutes=10)
UNTIL = NOW - timedelta(minutes=1)


def _dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if dsn:
        return dsn
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "hybridinference_test_db")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """A reachable test-database DSN, or skip."""
    dsn = _dsn()

    async def _probe() -> str | None:
        try:
            conn = await asyncpg.connect(dsn, timeout=2)
        except Exception as exc:
            return str(exc)
        try:
            assert_test_db_name(await conn.fetchval("SELECT current_database()"), "grant usage")
        finally:
            await conn.close()
        return None

    loop = asyncio.new_event_loop()
    try:
        failure = loop.run_until_complete(_probe())
    finally:
        loop.close()
    if failure is not None:
        pytest.skip(f"PostgreSQL test database not available: {failure}")
    return dsn


@pytest_asyncio.fixture
async def log_store(pg_dsn: str, request: pytest.FixtureRequest) -> AsyncGenerator[Any, None]:
    """A store over a private schema holding a fresh ``api_logs``.

    Schema-per-test rather than TRUNCATE: CI runs several files against one
    database concurrently, so truncating the shared table would delete rows
    out from under whatever else is running.
    """
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    schema = f"grant_usage_{worker}_{digest}"

    admin_conn = await asyncpg.connect(pg_dsn)
    try:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin_conn.close()

    pool = await asyncpg.create_pool(
        pg_dsn, min_size=1, max_size=3, server_settings={"search_path": schema}
    )
    assert pool is not None
    async with pool.acquire() as conn:
        await ensure_api_logs_schema(conn)
    try:
        yield PostgresLogStore(pool, store_full_prompts=False)
    finally:
        await pool.close()
        admin_conn = await asyncpg.connect(pg_dsn)
        try:
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin_conn.close()


async def _write(
    store: PostgresLogStore,
    request_id: str,
    *,
    grant: str | None,
    started_at: datetime,
    usage: dict[str, Any] | None,
    status_code: int = 200,
    estimated: bool = False,
    pricing: dict[str, str] | None = PRICING,
) -> None:
    """One row as a surface would write it: the job column plus attribution."""
    metadata: dict[str, Any] = {"agent_job_id": JOB}
    if grant is not None:
        metadata.update(
            ledger_attribution(
                {"agent_grant_id": grant, "agent_job_id": JOB},
                started_at=started_at.timestamp(),
            )
        )
    if estimated:
        metadata["usage_estimated"] = True
    await store.log_request(
        request_id=request_id,
        model_id="glm-5.1",
        provider="zai",
        prompt="hi",
        response=None,
        usage=usage,
        latency_ms=50,
        status_code=status_code,
        metadata=metadata,
        pricing=pricing,
        ttft_ms=5,
    )


async def _seed(store: PostgresLogStore) -> None:
    await _write(
        store,
        "in-first",
        grant=GRANT,
        started_at=SINCE,  # inclusive lower edge
        usage={"prompt_tokens": 100, "completion_tokens": 10, "cache_read_tokens": 80},
    )
    await _write(
        store,
        "in-second",
        grant=GRANT,
        started_at=SINCE + timedelta(seconds=30),
        usage={"prompt_tokens": 200, "completion_tokens": 20, "reasoning_tokens": 5},
        estimated=True,
    )
    # A cancelled stream: logged and counted, nothing known about its usage.
    await _write(
        store,
        "in-cancelled",
        grant=GRANT,
        started_at=SINCE + timedelta(seconds=45),
        usage=None,
        status_code=499,
        pricing=None,
    )
    await _write(
        store,
        "before-window",
        grant=GRANT,
        started_at=SINCE - timedelta(seconds=1),
        usage={"prompt_tokens": 999, "completion_tokens": 999},
    )
    await _write(
        store,
        "at-until",
        grant=GRANT,
        started_at=UNTIL,  # exclusive upper edge
        usage={"prompt_tokens": 999, "completion_tokens": 999},
    )
    # Same external job, another grant: a retained thread re-minting.
    await _write(
        store,
        "other-grant",
        grant=OTHER_GRANT,
        started_at=SINCE + timedelta(seconds=10),
        usage={"prompt_tokens": 999, "completion_tokens": 999},
    )
    # Written before attribution existed: the job column only.
    await _write(
        store,
        "unattributed",
        grant=None,
        started_at=SINCE + timedelta(seconds=20),
        usage={"prompt_tokens": 999, "completion_tokens": 999},
    )


@pytest.mark.asyncio
async def test_totals_cover_this_grant_inside_the_window_and_nothing_else(log_store) -> None:
    await _seed(log_store)

    summary = await log_store.get_agent_grant_usage(
        agent_job_id=JOB, grant_id=GRANT, since=SINCE, until=UNTIL
    )

    assert summary["calls"] == 3
    assert summary["rows"] == 3
    assert summary["estimated_calls"] == 1
    metrics = summary["metrics"]
    assert metrics["tokens_in"] == {"value": 300, "known_rows": 2, "unknown_rows": 1}
    assert metrics["tokens_out"] == {"value": 30, "known_rows": 2, "unknown_rows": 1}
    assert metrics["cache_read"] == {"value": 80, "known_rows": 1, "unknown_rows": 2}
    assert metrics["reasoning"] == {"value": 5, "known_rows": 1, "unknown_rows": 2}
    spent = metrics["spent_usd"]
    assert (spent["known_rows"], spent["unknown_rows"]) == (2, 1)
    # What the writer billed the two priced rows, through the same helpers it
    # used, so the assertion follows the billing rules rather than restating
    # them (reasoning tokens are billed as output, for one).
    expected = sum(
        Decimal(str(calculate_cost(normalize_usage(usage) or usage, PRICING)))
        for usage in (
            {"prompt_tokens": 100, "completion_tokens": 10, "cache_read_tokens": 80},
            {"prompt_tokens": 200, "completion_tokens": 20, "reasoning_tokens": 5},
        )
    )
    assert abs(Decimal(spent["value"]) - expected) < Decimal("1e-9")


@pytest.mark.asyncio
async def test_rows_logged_after_the_window_closed_still_belong_to_it(log_store) -> None:
    """The upper edge is the start time; the insert time is only a pre-filter."""
    await _write(
        log_store,
        "late",
        grant=GRANT,
        started_at=UNTIL - timedelta(seconds=1),
        usage={"prompt_tokens": 7, "completion_tokens": 3},
    )
    async with log_store.pool.acquire() as conn:
        logged_at = await conn.fetchval("SELECT timestamp FROM api_logs WHERE request_id = 'late'")
    assert logged_at > UNTIL

    summary = await log_store.get_agent_grant_usage(
        agent_job_id=JOB, grant_id=GRANT, since=SINCE, until=UNTIL
    )
    assert summary["calls"] == 1
    assert summary["metrics"]["tokens_in"]["value"] == 7


@pytest.mark.asyncio
async def test_an_empty_window_knows_nothing_rather_than_reporting_zero(log_store) -> None:
    await _seed(log_store)
    # Every seeded start time is earlier than NOW, whatever the insert time.
    summary = await log_store.get_agent_grant_usage(
        agent_job_id=JOB, grant_id=GRANT, since=NOW, until=NOW + timedelta(hours=1)
    )
    assert summary["calls"] == 0
    assert all(m["value"] is None for m in summary["metrics"].values())
    assert all(m["unknown_rows"] == 0 for m in summary["metrics"].values())


@pytest.mark.asyncio
async def test_requests_page_in_start_order_and_resume_after_a_row(log_store) -> None:
    await _seed(log_store)
    query = {"agent_job_id": JOB, "grant_id": GRANT, "since": SINCE, "until": UNTIL}

    first = await log_store.list_agent_grant_requests(**query, limit=2)
    assert [r["request_id"] for r in first] == ["in-first", "in-second"]
    assert first[0]["request_started_at"] == SINCE
    assert first[0]["request_started_at"].tzinfo is not None
    assert first[0]["logged_at"] > UNTIL
    assert first[0]["prompt_tokens"] == 100
    assert first[0]["cache_read_tokens"] == 80
    assert first[0]["usage_estimated"] is False
    assert first[1]["usage_estimated"] is True
    assert first[1]["reasoning_tokens"] == 5
    assert isinstance(first[0]["cost_usd"], Decimal)

    after = (first[-1]["request_started_at"], first[-1]["request_id"])
    second = await log_store.list_agent_grant_requests(**query, limit=2, after=after)
    assert [r["request_id"] for r in second] == ["in-cancelled"]
    assert second[0]["prompt_tokens"] is None
    assert second[0]["cost_usd"] is None
    assert second[0]["status_code"] == 499

    after = (second[-1]["request_started_at"], second[-1]["request_id"])
    assert await log_store.list_agent_grant_requests(**query, limit=2, after=after) == []


@pytest.mark.asyncio
async def test_the_legacy_job_total_still_counts_every_grant_on_the_job(log_store) -> None:
    """The lifetime report is keyed on the job column and is left as it was."""
    await _seed(log_store)
    usage = await log_store.get_agent_job_usage(JOB)
    assert usage["calls"] == 7
