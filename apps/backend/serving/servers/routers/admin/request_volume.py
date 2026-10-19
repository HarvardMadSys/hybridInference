"""Hourly rollup behind the admin Request volume cards (``/admin/request-metrics``).

The cards cover the last hour, day, week and month. Counting them straight off
``api_logs`` read every row of the past month on each page load -- the month
card's query read the month's whole heap and then sorted it, spilling to disk --
which ran past the pool's 60-second command timeout, and the console's proxy
gives up well before that. Yet only the newest hours of a card can still change:
once an hour is over, its rows are written.

So completed hours are counted once into ``request_volume_hourly`` (created with
``api_logs`` in ``serving.storage.log_schema``) and read back from there. What is
still counted from ``api_logs`` on each load is small:

- the last two hours (:func:`settled_before`);
- the minutes before a card's first whole hour: a card starts at "now minus its
  length", which is rarely on the hour;
- the hour card, whose 5-minute bars lie inside those two hours anyway.

A row is classified by the Recent Requests outcome filters themselves
(``_common.REQUEST_OUTCOME_FILTERS``), so a card's error and disconnect counts are
the rows the table lists under those outcomes, and an hour read from the rollup
and an hour counted live cannot disagree about what an error is.

Hours reach the table three ways, all through :func:`fill_hours`: the hourly job,
a backfill at startup, and the endpoint itself when an hour it needs is missing
(the job runs a little after the hour turns, and not at all when the scheduler is
off). Every fill and every invalidation holds one transaction-scoped advisory
lock, and a fill re-reads which hours are missing once it holds it: concurrent
fills do not repeat each other's work, and no fill can write back counts it read
from before a delete it raced (see :func:`invalidate_hours`).

Deletes are the one way a completed hour changes. "Clear errors" invalidates the
hours it deleted rows from, in the delete's own transaction. A user's hard delete does not: like ``provider_hourly_stats``
and the geo rollup, these are anonymous counts of traffic that did happen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from serving.servers.routers.admin._common import REQUEST_OUTCOME_FILTERS
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import asyncpg

logger = get_logger(__name__)

# Held by every write to the table (see the module docstring). A constant 64-bit
# key, so writers on every replica serialize on the same lock.
ADVISORY_LOCK_KEY = 0x726571766F6C  # ascii "reqvol" packed

# History stored and kept: the month card's 30 days plus the hour it starts
# part-way through, with the rest of a day as slack so a late job run never
# purges an hour a card still shows.
RETENTION_DAYS = 31

# Hours counted per fill statement. Bounded so that filling a large table never
# runs one statement for long enough to meet the pool's command timeout.
FILL_CHUNK_HOURS = 6

_HOUR = timedelta(hours=1)
# The smallest step a ``timestamptz`` resolves. A card ends *at* the current
# minute, inclusive, as the per-card queries this replaced did, so its last live
# range is half-open one tick past it.
_TICK = timedelta(microseconds=1)

# What a card counts over a set of rows. Success is a 2xx/3xx status. Errors and
# client disconnects are the outcome filters, so the two are disjoint, while a
# 2xx row that also recorded an error counts as a success and as an error, as it
# always has.
_COUNTS_SQL = f"""
        COUNT(*) AS request_count,
        COUNT(*) FILTER (WHERE l.status_code >= 200 AND l.status_code < 400)
            AS success_count,
        COUNT(*) FILTER (
            WHERE {REQUEST_OUTCOME_FILTERS["errors_excluding_disconnects"]}
        ) AS error_count,
        COUNT(*) FILTER (WHERE {REQUEST_OUTCOME_FILTERS["client_disconnect"]})
            AS client_disconnect_count,
        COUNT(l.latency_ms) AS latency_count,
        COALESCE(SUM(l.latency_ms), 0) AS latency_sum_ms"""

# Counts per half-open range [range_start, range_end), one row per range even when
# it holds no rows. One aggregate per range, each an index range scan -- not one
# GROUP BY over a computed bucket, which the planner cannot size and which it
# answered with a sort of every row in the window.
_RANGE_COUNTS_SQL = f"""
SELECT r.range_start, r.range_end, c.*
FROM unnest($1::timestamptz[], $2::timestamptz[]) AS r(range_start, range_end)
CROSS JOIN LATERAL (
    SELECT{_COUNTS_SQL}
    FROM api_logs l
    WHERE l.timestamp >= r.range_start AND l.timestamp < r.range_end
) c
"""

_FILL_SQL = f"""
INSERT INTO request_volume_hourly AS v (
    hour_bucket, request_count, success_count, error_count,
    client_disconnect_count, latency_count, latency_sum_ms, completed_at
)
SELECT
    counted.range_start, counted.request_count, counted.success_count,
    counted.error_count, counted.client_disconnect_count,
    counted.latency_count, counted.latency_sum_ms, NOW()
FROM ({_RANGE_COUNTS_SQL}) AS counted
ON CONFLICT (hour_bucket) DO UPDATE SET
    request_count = EXCLUDED.request_count,
    success_count = EXCLUDED.success_count,
    error_count = EXCLUDED.error_count,
    client_disconnect_count = EXCLUDED.client_disconnect_count,
    latency_count = EXCLUDED.latency_count,
    latency_sum_ms = EXCLUDED.latency_sum_ms,
    completed_at = EXCLUDED.completed_at
"""

_STORED_SQL = """
SELECT hour_bucket, request_count, success_count, error_count,
    client_disconnect_count, latency_count, latency_sum_ms
FROM request_volume_hourly
WHERE hour_bucket >= $1 AND hour_bucket < $2
"""

_STORED_OF_SQL = """
SELECT hour_bucket FROM request_volume_hourly WHERE hour_bucket = ANY($1::timestamptz[])
"""

_PURGE_SQL = """
DELETE FROM request_volume_hourly
WHERE hour_bucket < NOW() - make_interval(days => $1::int)
"""


@dataclass
class VolumeCounts:
    """Counts for one stretch of time; stretches add up into a bar."""

    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    client_disconnect_count: int = 0
    latency_count: int = 0
    latency_sum_ms: int = 0

    @classmethod
    def from_row(cls, row: Any) -> VolumeCounts:
        """Read the counts out of a rollup or range-count row."""
        return cls(**{field.name: int(row[field.name] or 0) for field in fields(cls)})

    def add(self, other: VolumeCounts) -> None:
        """Fold *other* into these counts."""
        for field in fields(self):
            setattr(self, field.name, getattr(self, field.name) + getattr(other, field.name))

    @property
    def avg_latency_ms(self) -> float | None:
        """Mean latency over the rows that recorded one, to one decimal."""
        if not self.latency_count:
            return None
        return round(self.latency_sum_ms / self.latency_count, 1)


def _floor(ts: datetime, minutes: int) -> datetime:
    """Floor *ts* to a whole multiple of *minutes* since the epoch, as a bar is."""
    width = minutes * 60
    return datetime.fromtimestamp(int(ts.timestamp()) // width * width, tz=timezone.utc)


def _ceil_hour(ts: datetime) -> datetime:
    floored = _floor(ts, 60)
    return floored if floored == ts else floored + _HOUR


def _hours(start: datetime, end: datetime) -> list[datetime]:
    """Hour starts in [start, end); *start* is on the hour."""
    hours = []
    while start < end:
        hours.append(start)
        start += _HOUR
    return hours


def _split(start: datetime, end: datetime, minutes: int) -> list[tuple[datetime, datetime]]:
    """Cut [start, end) at every bar boundary, so each piece lies in one bar."""
    pieces = []
    while start < end:
        cut = min(_floor(start, minutes) + timedelta(minutes=minutes), end)
        pieces.append((start, cut))
        start = cut
    return pieces


def settled_before(end_time: datetime) -> datetime:
    """The first hour counted live; the hours before it are read from the table.

    The previous hour stays live along with the current one. A row's timestamp
    is taken when its log write starts, and the write can commit after the hour
    it is stamped with has ended, so an hour is stored only once it has been over
    for a whole hour.
    """
    return _floor(end_time, 60) - _HOUR


async def fill_hours(
    conn: asyncpg.Connection,
    hours: Iterable[datetime],
    *,
    newest_first: bool = False,
) -> int:
    """Count *hours* into the table, skipping any another writer stored meanwhile.

    Each chunk is its own transaction under the advisory lock. Returns how many
    hours this call wrote.
    """
    pending = sorted(set(hours), reverse=newest_first)
    written = 0
    for offset in range(0, len(pending), FILL_CHUNK_HOURS):
        chunk = pending[offset : offset + FILL_CHUNK_HOURS]
        async with conn.transaction():
            # The estimate for a range bound by a lateral column is far above
            # what an hour holds, enough to trigger JIT compilation, which then
            # costs more than the scans it compiles.
            await conn.execute("SET LOCAL jit = off")
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
            stored = {row["hour_bucket"] for row in await conn.fetch(_STORED_OF_SQL, chunk)}
            todo = [hour for hour in chunk if hour not in stored]
            if todo:
                await conn.execute(_FILL_SQL, todo, [hour + _HOUR for hour in todo])
                written += len(todo)
    return written


async def _stored(
    conn: asyncpg.Connection, start: datetime, end: datetime
) -> dict[datetime, VolumeCounts]:
    rows = await conn.fetch(_STORED_SQL, start, end)
    return {row["hour_bucket"]: VolumeCounts.from_row(row) for row in rows}


async def load_window_buckets(
    pool: asyncpg.Pool,
    windows: Sequence[tuple[int, int]],
) -> list[list[tuple[datetime, VolumeCounts]]]:
    """Per-bar counts for each ``(window_minutes, bucket_minutes)`` card.

    A card's range is the last *window_minutes* up to the current minute,
    inclusive. Its bars are epoch-aligned, oldest first, quiet bars included; the
    first bar starts at or before the range and counts rows only from its start.
    """
    async with pool.acquire() as conn:
        end_time: datetime = await conn.fetchval("SELECT date_trunc('minute', NOW())")
        settled = settled_before(end_time)

        # Each card: its bar width and grid, the ranges it counts live, and the
        # stored hours it reads.
        cards: list[tuple[int, datetime, list[tuple[datetime, datetime]], list[datetime]]] = []
        for window_minutes, bucket_minutes in windows:
            start = end_time - timedelta(minutes=window_minutes)
            if bucket_minutes % 60 or start >= settled:
                # Bars finer than an hour cannot be built from stored hours; such
                # a card is counted live. (The hour card also lies inside the live
                # span, so it would be either way.)
                live = _split(start, end_time + _TICK, bucket_minutes)
                hours: list[datetime] = []
            else:
                first_hour = _ceil_hour(start)
                live = _split(start, first_hour, bucket_minutes)
                live += _split(settled, end_time + _TICK, 60)
                hours = _hours(first_hour, settled)
            cards.append((bucket_minutes, start, live, hours))

        needed = sorted({hour for *_, hours in cards for hour in hours})
        stored: dict[datetime, VolumeCounts] = {}
        if needed:
            stored = await _stored(conn, needed[0], needed[-1] + _HOUR)
            missing = [hour for hour in needed if hour not in stored]
            if missing:
                await fill_hours(conn, missing)
                stored = await _stored(conn, needed[0], needed[-1] + _HOUR)

        ranges = {piece for _, _, live, _ in cards for piece in live}
        # An hour can still be absent if a "Clear errors" invalidated it between
        # the fill and the read; count it live rather than show it empty.
        ranges.update((hour, hour + _HOUR) for hour in needed if hour not in stored)
        counted: dict[tuple[datetime, datetime], VolumeCounts] = {}
        if ranges:
            ordered = sorted(ranges)
            async with conn.transaction():
                await conn.execute("SET LOCAL jit = off")
                rows = await conn.fetch(
                    _RANGE_COUNTS_SQL,
                    [range_start for range_start, _ in ordered],
                    [range_end for _, range_end in ordered],
                )
            counted = {
                (row["range_start"], row["range_end"]): VolumeCounts.from_row(row) for row in rows
            }

    result = []
    for bucket_minutes, start, live, hours in cards:
        width = timedelta(minutes=bucket_minutes)
        bars: dict[datetime, VolumeCounts] = {}
        bar = _floor(start, bucket_minutes)
        while bar <= end_time:
            bars[bar] = VolumeCounts()
            bar += width
        for piece in live:
            bars[_floor(piece[0], bucket_minutes)].add(counted[piece])
        for hour in hours:
            counts = stored[hour] if hour in stored else counted[(hour, hour + _HOUR)]
            bars[_floor(hour, bucket_minutes)].add(counts)
        result.append(list(bars.items()))
    return result


async def invalidate_hours(conn: asyncpg.Connection, hours: list[datetime]) -> None:
    """Drop the stored *hours*, in the caller's transaction.

    The delete of ``api_logs`` rows passes it as ``on_deleted``, so the two
    commit or fail together: were they separate, a failed invalidation would
    leave an hour stored with the deleted rows in it, and since a stored hour is
    never counted again, it would keep them for good. Under the advisory lock, a
    fill either committed before this -- and whatever it read from before the
    delete is dropped here -- or starts after the delete commits and reads the
    rows as they are then.
    """
    if not hours:
        return
    await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
    await conn.execute(
        "DELETE FROM request_volume_hourly WHERE hour_bucket = ANY($1::timestamptz[])", hours
    )


async def backfill_missing(pool: asyncpg.Pool, *, days: int = RETENTION_DAYS) -> int:
    """Store every completed hour of the last *days* days the table lacks.

    Newest first, so the hours the cards show most are ready first. Returns how
    many hours it wrote.
    """
    async with pool.acquire() as conn:
        end_time = await conn.fetchval("SELECT date_trunc('minute', NOW())")
        end = settled_before(end_time)
        start = end - timedelta(days=days)
        stored = await _stored(conn, start, end)
        missing = [hour for hour in _hours(start, end) if hour not in stored]
        if not missing:
            return 0
        return await fill_hours(conn, missing, newest_first=True)


async def hourly_job(pool: asyncpg.Pool, *, retention_days: int = RETENTION_DAYS) -> None:
    """Store the hours that completed since the last run, then purge old ones."""
    started_at = time.monotonic()
    written = 0
    outcome = "error"
    try:
        written = await backfill_missing(pool, days=retention_days)
        async with pool.acquire() as conn:
            await conn.execute(_PURGE_SQL, retention_days)
        outcome = "ok"
    except Exception as exc:
        logger.exception(f"rollup_request_volume failed: {exc}")
    finally:
        logger.info(
            "rollup_request_volume: hours=%d duration_ms=%d outcome=%s",
            written,
            int((time.monotonic() - started_at) * 1000),
            outcome,
        )


def register_rollup_job(scheduler: Any, pool: asyncpg.Pool) -> None:
    """Register the hourly job on the existing AsyncIOScheduler.

    Minute 15: an hour is stored only once it has been over for a whole hour (see
    :func:`settled_before`), so this run picks up the hour before last, and stays
    clear of the provider (minute 5) and geo (minute 10) rollups.
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        hourly_job,
        trigger=CronTrigger(minute=15, timezone=timezone.utc),
        args=[pool],
        id="rollup_request_volume",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("rollup_request_volume: registered on scheduler (cron minute=15)")
