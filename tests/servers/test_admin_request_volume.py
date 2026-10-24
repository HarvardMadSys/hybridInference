"""Unit tests for the admin Request volume rollup (``admin/request_volume.py``).

A card is assembled from three kinds of piece: stored hours, the minutes before
its first whole hour, and the live hours at its end. An off-by-one at any seam
drops or double-counts rows without changing the shape of the response, so these
run the assembly against an in-memory stand-in for Postgres and compare every bar
with a direct count of the same rows -- the definition the per-card queries it
replaced used: rows from "now minus the window" through the current minute,
inclusive, in epoch-aligned bars.

``tests/integration/test_admin_request_volume_postgres.py`` runs the real SQL.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from serving.servers.routers.admin import request_volume
from serving.servers.routers.admin.metrics import REQUEST_METRIC_WINDOWS
from serving.servers.routers.admin.request_volume import VolumeCounts

_CARDS = [(window, bucket) for *_, window, bucket in REQUEST_METRIC_WINDOWS]
_TICK = timedelta(microseconds=1)


@dataclass(frozen=True)
class _Log:
    timestamp: datetime
    status_code: int | None = 200
    error: str | None = None
    terminal_state: str | None = None
    latency_ms: int | None = 100


def _classify(row: _Log) -> VolumeCounts:
    """One row's counts, by the outcome filters' definitions."""
    status = row.status_code
    disconnect = status == 499 and row.terminal_state == "client_disconnect"
    failed = row.error is not None or status is None or status < 200 or status >= 400
    return VolumeCounts(
        request_count=1,
        success_count=int(status is not None and 200 <= status < 400),
        error_count=int(failed and not disconnect),
        client_disconnect_count=int(disconnect),
        latency_count=int(row.latency_ms is not None),
        latency_sum_ms=row.latency_ms or 0,
    )


def _count(rows: list[_Log], start: datetime, end: datetime) -> VolumeCounts:
    total = VolumeCounts()
    for row in rows:
        if start <= row.timestamp < end:
            total.add(_classify(row))
    return total


def _floor(ts: datetime, minutes: int) -> datetime:
    width = minutes * 60
    return datetime.fromtimestamp(int(ts.timestamp()) // width * width, tz=UTC)


def _expected(rows: list[_Log], end_time: datetime, window: int, bucket: int):
    """The bars a card should show, counted straight from the rows."""
    start = end_time - timedelta(minutes=window)
    bars: dict[datetime, VolumeCounts] = {}
    bar = _floor(start, bucket)
    while bar <= end_time:
        bars[bar] = VolumeCounts()
        bar += timedelta(minutes=bucket)
    for row in rows:
        if start <= row.timestamp <= end_time:
            bars[_floor(row.timestamp, bucket)].add(_classify(row))
    return list(bars.items())


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeDb:
    """Answers the module's statements from a list of rows and a stored-hours dict."""

    def __init__(self, rows: list[_Log], end_time: datetime) -> None:
        self.rows = rows
        self.end_time = end_time
        self.stored: dict[datetime, VolumeCounts] = {}
        self.filled: list[list[datetime]] = []
        self.executed: list[str] = []
        # Hours to drop from the table the next time it is read, as a racing
        # "Clear errors" would.
        self.vanish_on_next_read: set[datetime] = set()

    # -- pool -----------------------------------------------------------------
    def acquire(self) -> _FakeDb:
        return self

    async def __aenter__(self) -> _FakeDb:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    # -- connection -----------------------------------------------------------
    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchval(self, query: str, *args: Any) -> Any:
        assert query == "SELECT date_trunc('minute', NOW())"
        return self.end_time

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if query == request_volume._STORED_SQL:
            start, end = args
            reads = [
                {"hour_bucket": hour, **asdict(counts)}
                for hour, counts in self.stored.items()
                if start <= hour < end
            ]
            if self.vanish_on_next_read and not self.filled:
                return reads
            for hour in self.vanish_on_next_read:
                self.stored.pop(hour, None)
            self.vanish_on_next_read = set()
            return [row for row in reads if row["hour_bucket"] in self.stored]
        if query == request_volume._STORED_OF_SQL:
            (hours,) = args
            return [{"hour_bucket": hour} for hour in hours if hour in self.stored]
        if query == request_volume._RANGE_COUNTS_SQL:
            starts, ends = args
            return [
                {"range_start": start, "range_end": end, **asdict(_count(self.rows, start, end))}
                for start, end in zip(starts, ends, strict=True)
            ]
        raise AssertionError(f"unexpected fetch: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append(query)
        if query == request_volume._FILL_SQL:
            starts, ends = args
            self.filled.append(list(starts))
            for start, end in zip(starts, ends, strict=True):
                assert end - start == timedelta(hours=1)
                self.stored[start] = _count(self.rows, start, end)
        return "OK"


def _random_rows(end_time: datetime, count: int, seed: int) -> list[_Log]:
    """Rows spread over the month card plus a day either side, every outcome mixed in."""
    rng = random.Random(seed)
    span = timedelta(days=32).total_seconds()
    outcomes = [
        {},
        {"status_code": 500, "error": "boom"},
        {"status_code": 499, "error": "gone", "terminal_state": "client_disconnect"},
        # An upstream's own 499: a failure, not a disconnect.
        {"status_code": 499, "error": "upstream 499"},
        {"status_code": None, "error": "no response", "latency_ms": None},
        # A 2xx that also recorded an error counts as both.
        {"status_code": 200, "error": "partial"},
    ]
    rows = []
    for _ in range(count):
        offset = timedelta(seconds=rng.uniform(-86400, span), microseconds=rng.randrange(10**6))
        fields = {"latency_ms": rng.randrange(1, 60000), **rng.choice(outcomes)}
        rows.append(_Log(timestamp=end_time + timedelta(days=1) - offset, **fields))
    return rows


def _edge_rows(end_time: datetime) -> list[_Log]:
    """Rows exactly on, and one tick either side of, every seam a card has."""
    settled = request_volume.settled_before(end_time)
    seams = [end_time, settled, settled + timedelta(hours=1)]
    for window, bucket in _CARDS:
        start = end_time - timedelta(minutes=window)
        seams += [start, _floor(start, 60) + timedelta(hours=1), _floor(start, bucket)]
    rows = []
    for seam in seams:
        for nudge in (-_TICK, timedelta(0), _TICK):
            rows.append(_Log(timestamp=seam + nudge, status_code=500, error="edge"))
    return rows


async def _bars(db: _FakeDb):
    return await request_volume.load_window_buckets(db, _CARDS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end_time",
    [
        # On the hour, a minute after, mid-hour, a minute before.
        datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 18, 12, 1, tzinfo=UTC),
        datetime(2026, 10, 18, 12, 37, tzinfo=UTC),
        datetime(2026, 10, 18, 12, 59, tzinfo=UTC),
        # Midnight: the month card's bars and the week card's 6-hour bars turn.
        datetime(2026, 10, 18, 0, 0, tzinfo=UTC),
    ],
)
async def test_every_bar_matches_a_direct_count(end_time: datetime):
    rows = _random_rows(end_time, 4000, seed=end_time.minute) + _edge_rows(end_time)
    db = _FakeDb(rows, end_time)

    got = await _bars(db)

    for (window, bucket), bars in zip(_CARDS, got, strict=True):
        assert bars == _expected(rows, end_time, window, bucket), (window, bucket)


@pytest.mark.asyncio
async def test_completed_hours_are_counted_once_and_then_read_back():
    end_time = datetime(2026, 10, 18, 12, 37, tzinfo=UTC)
    db = _FakeDb(_random_rows(end_time, 500, seed=1), end_time)

    first = await _bars(db)
    filled = [hour for chunk in db.filled for hour in chunk]
    # Every completed hour the month card needs, each once, in bounded chunks.
    settled = request_volume.settled_before(end_time)
    first_hour = _floor(end_time - timedelta(days=30), 60) + timedelta(hours=1)
    assert sorted(filled) == [
        first_hour + timedelta(hours=i)
        for i in range(int((settled - first_hour) / timedelta(hours=1)))
    ]
    assert all(len(chunk) <= request_volume.FILL_CHUNK_HOURS for chunk in db.filled)
    # Each chunk is written under the rollup's advisory lock.
    assert db.executed.count("SELECT pg_advisory_xact_lock($1)") == len(db.filled)

    db.filled.clear()
    assert await _bars(db) == first
    assert db.filled == [], "a second load must not count stored hours again"


@pytest.mark.asyncio
async def test_stored_hours_are_read_not_recounted():
    end_time = datetime(2026, 10, 18, 12, 37, tzinfo=UTC)
    db = _FakeDb([], end_time)
    three_days_ago = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    db.stored[three_days_ago] = VolumeCounts(request_count=999, success_count=999)

    got = dict(zip([w for w, _ in _CARDS], await _bars(db), strict=True))

    week = dict(got[10080])
    assert week[datetime(2026, 10, 15, 6, 0, tzinfo=UTC)].request_count == 999
    month = dict(got[43200])
    assert month[datetime(2026, 10, 15, tzinfo=UTC)].request_count == 999


@pytest.mark.asyncio
async def test_the_last_two_hours_are_always_counted_live():
    # A stored row for an hour still inside the live span (none should exist,
    # but an hour can still be changing there) must not be read.
    end_time = datetime(2026, 10, 18, 12, 37, tzinfo=UTC)
    rows = [_Log(timestamp=datetime(2026, 10, 18, 11, 30, tzinfo=UTC))]
    db = _FakeDb(rows, end_time)
    db.stored[datetime(2026, 10, 18, 11, 0, tzinfo=UTC)] = VolumeCounts(request_count=50)

    day = dict((await _bars(db))[1])

    assert day[datetime(2026, 10, 18, 11, 0, tzinfo=UTC)].request_count == 1


@pytest.mark.asyncio
async def test_an_hour_dropped_between_fill_and_read_is_counted_live():
    end_time = datetime(2026, 10, 18, 12, 37, tzinfo=UTC)
    rows = _random_rows(end_time, 500, seed=2)
    db = _FakeDb(rows, end_time)
    gone = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
    db.vanish_on_next_read = {gone}

    got = await _bars(db)

    assert gone not in db.stored
    for (window, bucket), bars in zip(_CARDS, got, strict=True):
        assert bars == _expected(rows, end_time, window, bucket), (window, bucket)


@pytest.mark.asyncio
async def test_backfill_fills_newest_first_and_skips_stored_hours():
    end_time = datetime(2026, 10, 18, 12, 37, tzinfo=UTC)
    db = _FakeDb([], end_time)
    settled = request_volume.settled_before(end_time)
    already = settled - timedelta(hours=3)
    db.stored[already] = VolumeCounts()

    written = await request_volume.backfill_missing(db, days=1)

    filled = [hour for chunk in db.filled for hour in chunk]
    assert written == 23
    assert already not in filled
    assert filled == sorted(filled, reverse=True)
    assert filled[0] == settled - timedelta(hours=1)
    assert await request_volume.backfill_missing(db, days=1) == 0


@pytest.mark.asyncio
async def test_hourly_job_purges_and_survives_a_failure(caplog: pytest.LogCaptureFixture):
    end_time = datetime(2026, 10, 18, 12, 37, tzinfo=UTC)
    db = _FakeDb([], end_time)

    await request_volume.hourly_job(db, retention_days=1)
    assert request_volume._PURGE_SQL in db.executed

    async def _boom(*_args: Any) -> Any:
        raise RuntimeError("database went away")

    db.fetchval = _boom  # type: ignore[method-assign]
    await request_volume.hourly_job(db)  # logged, not raised
    assert "rollup_request_volume failed" in caplog.text


def test_job_is_registered_hourly_at_minute_fifteen():
    class _Scheduler:
        def __init__(self) -> None:
            self.jobs: list[dict[str, Any]] = []

        def add_job(self, func: Any, **kwargs: Any) -> None:
            self.jobs.append({"func": func, **kwargs})

    scheduler = _Scheduler()
    request_volume.register_rollup_job(scheduler, pool=object())

    (job,) = scheduler.jobs
    assert job["func"] is request_volume.hourly_job
    assert job["id"] == "rollup_request_volume"
    assert str(job["trigger"].fields[6]) == "15"  # minute


def test_average_latency_is_over_the_rows_that_recorded_one():
    assert VolumeCounts().avg_latency_ms is None
    assert VolumeCounts(latency_count=3, latency_sum_ms=1000).avg_latency_ms == 333.3


def test_counts_use_the_outcome_filters():
    """The rollup's error and disconnect columns are the list's outcome filters."""
    from serving.servers.routers.admin._common import REQUEST_OUTCOME_FILTERS

    sql = request_volume._RANGE_COUNTS_SQL
    assert REQUEST_OUTCOME_FILTERS["errors_excluding_disconnects"] in sql
    assert REQUEST_OUTCOME_FILTERS["client_disconnect"] in sql
    # The fill writes exactly what the live path reads.
    assert request_volume._RANGE_COUNTS_SQL in request_volume._FILL_SQL
