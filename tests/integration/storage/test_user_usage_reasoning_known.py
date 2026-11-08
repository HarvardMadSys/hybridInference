"""The dashboard's reasoning sum tells unknown, 0 and a partial sum apart.

``api_logs.reasoning_tokens`` is NULL when the upstream reported no count (a
native Anthropic upstream never does), 0 when it reported none spent. The
window sum must not coalesce the first into the second, and must say how many
rows it summed so a partial figure can be labelled. Runs against PostgreSQL,
whose ``SUM``/``COUNT`` NULL semantics this depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PREFIX = "req-rsnknown-"
PARTIAL = "u-rsnknown-partial"
UNKNOWN = "u-rsnknown-unknown"
ZERO = "u-rsnknown-zero"
EMPTY = "u-rsnknown-empty"


def _today() -> datetime:
    # After UTC midnight, so inside every window whatever the time of day.
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=30
    )


def _long_ago() -> datetime:
    # Outside today, the rolling week and this month: all-time only.
    return datetime.now(UTC) - timedelta(days=70)


#: (user, request id suffix, timestamp, reasoning_tokens)
def _rows() -> list[tuple[str, str, datetime, int | None]]:
    return [
        (PARTIAL, "p-null", _today(), None),
        (PARTIAL, "p-zero", _today(), 0),
        (PARTIAL, "p-fifty", _today(), 50),
        (PARTIAL, "p-old", _long_ago(), 30),
        (UNKNOWN, "u-1", _today(), None),
        (UNKNOWN, "u-2", _today(), None),
        (UNKNOWN, "u-old", _long_ago(), 7),
        (ZERO, "z-1", _today(), 0),
    ]


@pytest_asyncio.fixture
async def log_store():
    pool = await postgres.create_pool(min_size=1, max_size=2)
    store = PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE $1", f"{PREFIX}%")

    await _wipe()
    async with pool.acquire() as conn:
        for user, suffix, at, reasoning in _rows():
            await conn.execute(
                "INSERT INTO api_logs (request_id, model_id, provider, timestamp, user_id, "
                "cost_usd, prompt_tokens, completion_tokens, reasoning_tokens) "
                "VALUES ($1, 'm', 'rsnknown-p', $2, $3, 0.5, 10, 5, $4)",
                f"{PREFIX}{suffix}",
                at,
                user,
                reasoning,
            )
    try:
        yield store
    finally:
        await _wipe()
        await pool.close()


def _reasoning(window: dict) -> tuple[int | None, int, int]:
    return window["reasoning_tokens"], window["reasoning_tokens_known_requests"], window["requests"]


async def test_a_partial_window_sums_only_the_rows_that_report(log_store):
    detail = await log_store.get_user_usage_detail(PARTIAL)

    # NULL, 0 and 50 today: two of three rows report, and they sum to 50.
    for window in ("today", "week", "month"):
        assert _reasoning(detail[window]) == (50, 2, 3), window
    assert _reasoning(detail["alltime"]) == (80, 3, 4)
    # The other sums are unchanged: every row still counts.
    assert detail["today"]["completion_tokens"] == 15
    assert detail["alltime"]["cost_usd"] == pytest.approx(2.0)


async def test_a_window_where_no_row_reports_is_unknown_not_zero(log_store):
    detail = await log_store.get_user_usage_detail(UNKNOWN)

    assert _reasoning(detail["today"]) == (None, 0, 2)
    assert _reasoning(detail["month"]) == (None, 0, 2)
    # The older row is outside this month but inside all-time.
    assert _reasoning(detail["alltime"]) == (7, 1, 3)


async def test_a_reported_zero_stays_zero(log_store):
    detail = await log_store.get_user_usage_detail(ZERO)

    assert _reasoning(detail["today"]) == (0, 1, 1)


async def test_a_user_without_rows_has_no_reasoning_sum(log_store):
    detail = await log_store.get_user_usage_detail(EMPTY)

    for window in ("today", "week", "month", "alltime"):
        assert _reasoning(detail[window]) == (None, 0, 0)
        assert detail[window]["prompt_tokens"] == 0
