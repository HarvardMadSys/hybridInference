"""The admin request list's ``start_time`` / ``end_time``: one Request volume bar.

Clicking a bar lists the requests it counted, so the list takes the bar's
half-open stretch. A stretch older than the lookback widens the lookback to
reach it -- a month card's bar is older than the list's default week -- but
never past the 90 days the lookback is bounded by.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from serving.servers.routers.admin import metrics
from tests.servers.test_admin_recent_requests_source import _app, _client, _Pool, _row

START = "2026-10-01T10:00:00Z"
END = "2026-10-01T11:00:00Z"


@pytest.fixture(autouse=True)
def _isolate_count_cache():
    metrics._RECENT_REQUESTS_COUNT_CACHE.clear()
    yield
    metrics._RECENT_REQUESTS_COUNT_CACHE.clear()


async def test_the_list_and_its_total_take_the_stretch():
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        resp = await client.get(f"/admin/recent-requests?start_time={START}&end_time={END}")

    assert resp.status_code == 200, resp.text
    ((page_sql, page_args),) = pool.fetches
    ((count_sql, count_args),) = pool.counts
    for sql, args in ((page_sql, page_args), (count_sql, count_args)):
        # $1 is the lookback; the stretch follows, start inclusive, end not.
        assert "l.timestamp >= $2" in sql
        assert "l.timestamp < $3" in sql
        assert args[1] == datetime(2026, 10, 1, 10, tzinfo=UTC)
        assert args[2] == datetime(2026, 10, 1, 11, tzinfo=UTC)


async def test_either_end_alone_narrows_the_list():
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        assert (await client.get(f"/admin/recent-requests?start_time={START}")).status_code == 200
        assert (await client.get(f"/admin/recent-requests?end_time={END}")).status_code == 200

    (start_sql, _), (end_sql, end_args) = pool.fetches
    assert "l.timestamp >= $2" in start_sql and "l.timestamp < $" not in start_sql
    assert "l.timestamp < $2" in end_sql
    assert end_args[1] == datetime(2026, 10, 1, 11, tzinfo=UTC)


async def test_each_stretch_is_counted_apart():
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        for end in (END, "2026-10-01T12:00:00Z", END):
            resp = await client.get(f"/admin/recent-requests?start_time={START}&end_time={end}")
            assert resp.status_code == 200, resp.text

    # The third reuses the first's total; the wider stretch is its own.
    assert len(pool.counts) == 2


@pytest.mark.parametrize(
    ("days_ago", "days", "lookback"),
    [
        # Inside the lookback: left alone.
        (2, 7, 7),
        # Older: widened to reach it, with a day to spare for the clocks.
        (30.5, 7, 31),
        (10, 1, 11),
        # Never past the bound.
        (200, 7, 90),
        # Never narrowed.
        (2, 30, 30),
    ],
)
async def test_an_older_stretch_widens_the_lookback_to_reach_it(days_ago, days, lookback):
    start = datetime.now(UTC) - timedelta(days=days_ago)
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        resp = await client.get(
            "/admin/recent-requests",
            params={"days": days, "start_time": start.isoformat()},
        )

    assert resp.status_code == 200, resp.text
    ((_, page_args),) = pool.fetches
    assert page_args[0] == lookback


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("start_time=2026-10-01T10:00:00", "start_time must be timezone-aware"),
        ("end_time=2026-10-01T10:00:00", "end_time must be timezone-aware"),
        (f"start_time={END}&end_time={START}", "end_time must be after start_time"),
        (f"start_time={START}&end_time={START}", "end_time must be after start_time"),
    ],
)
async def test_a_naive_or_inverted_stretch_is_refused_before_anything_runs(query, message):
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        resp = await client.get(f"/admin/recent-requests?{query}")

    assert resp.status_code == 422, resp.text
    assert message in resp.text
    assert pool.statements() == []
