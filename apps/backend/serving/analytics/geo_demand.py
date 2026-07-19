"""Aggregate privacy-safe geo-temporal demand from ``api_logs``."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from serving.utils.geo_resolver import GeoResolver
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

BUCKET_COLS = ["c", "cont", "n", "tout"]
MAX_WINDOW = timedelta(days=90)
logger = get_logger(__name__)

ROWS_QUERY = """
SELECT
  date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour,
  metadata->>'ip'               AS ip,
  COUNT(*)::BIGINT              AS request_count,
  COALESCE(SUM(completion_tokens), 0)::BIGINT AS completion_tokens
FROM api_logs
WHERE timestamp >= $1 AND timestamp < $2
  AND COALESCE(metadata->>'synthetic_probe', 'false') <> 'true'
GROUP BY 1, 2
"""

ROLLUP_COVERAGE_QUERY = """
SELECT
  COUNT(*)::BIGINT AS complete_hours,
  COALESCE(SUM(rows_total), 0)::BIGINT AS rows_total,
  COALESCE(SUM(rows_with_ip), 0)::BIGINT AS rows_with_ip,
  MAX(completed_at) AS completed_at
FROM geo_hourly_coverage
WHERE hour_bucket >= $1 AND hour_bucket < $2
"""

ROLLUP_ROWS_QUERY = """
SELECT
  hour_bucket AS hour,
  country_code,
  continent_code,
  request_count,
  completion_tokens
FROM geo_hourly_demand
WHERE hour_bucket >= $1 AND hour_bucket < $2
"""


class GeoBucket:
    """Mutable accumulator for one hour and country bucket."""

    __slots__ = ("n", "tout")

    def __init__(self) -> None:
        self.n = 0
        self.tout = 0


class GeoCoverage:
    """Mutable raw-row counts for one completed hour."""

    __slots__ = ("rows_total", "rows_with_ip")

    def __init__(self) -> None:
        self.rows_total = 0
        self.rows_with_ip = 0


def floor_hour(value: datetime) -> datetime:
    """Normalize an aware datetime to the beginning of its UTC hour."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def normalize_window(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    """Hour-align and validate a geo demand query window."""
    start = floor_hour(since)
    end = floor_hour(until)
    if start >= end:
        raise ValueError("since must be before until after hourly alignment")
    if end - start > MAX_WINDOW:
        raise ValueError("geo demand window cannot exceed 90 days")
    return start, end


def build_hours_index(since: datetime, until: datetime) -> list[datetime]:
    """Build a gap-free, inclusive-start/exclusive-end hourly index."""
    hours: list[datetime] = []
    current = since
    while current < until:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def finalize_geo_demand(
    hours_index: list[datetime],
    buckets: dict[tuple[datetime, str, str], GeoBucket],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the demand-only columnar payload consumed by the product globe."""
    hour_positions = {hour: index for index, hour in enumerate(hours_index)}
    hours_out: list[dict[str, list[list[Any]]]] = [{"b": []} for _ in hours_index]

    for (hour, alpha3, continent), bucket in sorted(
        buckets.items(), key=lambda item: (item[0][0], -item[1].n)
    ):
        if hour not in hour_positions:
            continue
        hours_out[hour_positions[hour]]["b"].append(
            [
                alpha3,
                continent,
                bucket.n,
                bucket.tout,
            ]
        )

    return {
        "meta": meta,
        "bucket_cols": BUCKET_COLS,
        "hours_index": [hour.isoformat() for hour in hours_index],
        "hours": hours_out,
    }


def build_geo_meta(
    hours_index: list[datetime],
    resolver: GeoResolver,
    *,
    rows_total: int,
    rows_with_ip: int,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the stable metadata shared by raw and rollup-backed responses."""
    return {
        "source": "api_logs",
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "start": hours_index[0].isoformat() if hours_index else None,
        "hours": len(hours_index),
        "rows_total": rows_total,
        "rows_with_ip": rows_with_ip,
        "geoip": {
            "country": resolver.country_enabled,
            "provider": resolver.country_provider,
            "attribution": resolver.country_attribution,
        },
        "degraded": resolver.degraded,
        "degraded_reasons": list(resolver.degraded_reasons),
        "unmapped_alpha2": sorted(resolver.unmapped_a2),
        "notes": ["origin = network origin (IP-based), not user residence"],
    }


def add_geo_row(
    row: Any,
    resolver: GeoResolver,
    buckets: dict[tuple[datetime, str, str], GeoBucket],
) -> bool:
    """Resolve and add one grouped database row; return whether it carried an IP."""
    ip = row["ip"]
    alpha3, _, continent = resolver.resolve(ip)
    request_count = int(row["request_count"])

    bucket = buckets[(row["hour"], alpha3, continent)]
    bucket.n += request_count
    bucket.tout += int(row["completion_tokens"] or 0)
    return bool(ip)


async def consume_geo_rows(
    connection: Any,
    since: datetime,
    until: datetime,
    resolver: GeoResolver,
    *,
    prefetch: int = 5_000,
) -> tuple[
    dict[tuple[datetime, str, str], GeoBucket],
    dict[datetime, GeoCoverage],
]:
    """Consume SQL-preaggregated hour/IP rows inside an existing transaction."""
    buckets: dict[tuple[datetime, str, str], GeoBucket] = defaultdict(GeoBucket)
    coverage = {hour: GeoCoverage() for hour in build_hours_index(since, until)}
    rows_consumed = 0

    cursor = connection.cursor(ROWS_QUERY, since, until, prefetch=prefetch)
    async for row in cursor:
        request_count = int(row["request_count"])
        rows_consumed += request_count
        hour_coverage = coverage[row["hour"]]
        hour_coverage.rows_total += request_count
        if add_geo_row(row, resolver, buckets):
            hour_coverage.rows_with_ip += request_count
        if rows_consumed and rows_consumed % 10_000 < request_count:
            await asyncio.sleep(0)

    return buckets, coverage


async def aggregate_geo_demand(
    connection: Any,
    since: datetime,
    until: datetime,
    resolver: GeoResolver,
    *,
    prefetch: int = 5_000,
) -> dict[str, Any]:
    """Stream one SQL-preaggregated window and return aggregate-only globe data."""
    since, until = normalize_window(since, until)

    async with connection.transaction():
        buckets, coverage = await consume_geo_rows(
            connection,
            since,
            until,
            resolver,
            prefetch=prefetch,
        )

    hours_index = build_hours_index(since, until)
    rows_total = sum(item.rows_total for item in coverage.values())
    rows_with_ip = sum(item.rows_with_ip for item in coverage.values())
    meta = build_geo_meta(
        hours_index,
        resolver,
        rows_total=rows_total,
        rows_with_ip=rows_with_ip,
    )
    return finalize_geo_demand(hours_index, buckets, meta)


async def aggregate_geo_demand_rollup(
    connection: Any,
    since: datetime,
    until: datetime,
    resolver: GeoResolver,
) -> dict[str, Any] | None:
    """Return the persistent rollup only when every requested hour is complete."""
    since, until = normalize_window(since, until)
    hours_index = build_hours_index(since, until)
    async with connection.transaction(isolation="repeatable_read", readonly=True):
        coverage = await connection.fetchrow(ROLLUP_COVERAGE_QUERY, since, until)
        if coverage is None or int(coverage["complete_hours"] or 0) != len(hours_index):
            return None

        buckets: dict[tuple[datetime, str, str], GeoBucket] = defaultdict(GeoBucket)
        for row in await connection.fetch(ROLLUP_ROWS_QUERY, since, until):
            bucket = buckets[(row["hour"], row["country_code"], row["continent_code"])]
            bucket.n += int(row["request_count"])
            bucket.tout += int(row["completion_tokens"])

    meta = build_geo_meta(
        hours_index,
        resolver,
        rows_total=int(coverage["rows_total"] or 0),
        rows_with_ip=int(coverage["rows_with_ip"] or 0),
        generated_at=coverage["completed_at"],
    )
    return finalize_geo_demand(hours_index, buckets, meta)


@dataclass
class _CacheEntry:
    payload: dict[str, Any]
    expires_at: float


class GeoDemandCache:
    """One-hour process-local cache with one in-flight scan per window."""

    def __init__(self, ttl_seconds: float = 3_600, max_entries: int = 4) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[datetime, datetime], _CacheEntry] = OrderedDict()
        self._inflight: dict[tuple[datetime, datetime], asyncio.Task[dict[str, Any]]] = {}

    def clear(self) -> None:
        """Drop cached entries and cancel in-flight scans."""
        self._entries.clear()
        for task in self._inflight.values():
            task.cancel()
        self._inflight.clear()

    async def _scan(
        self,
        pool: Any,
        key: tuple[datetime, datetime],
        resolver_factory: Callable[[], GeoResolver],
    ) -> dict[str, Any]:
        resolver = resolver_factory()
        try:
            async with pool.acquire() as connection:
                payload = await aggregate_geo_demand(connection, *key, resolver)
        finally:
            resolver.close()

        self._entries[key] = _CacheEntry(payload, time.monotonic() + self.ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return payload

    def _finish_scan(
        self,
        key: tuple[datetime, datetime],
        task: asyncio.Task[dict[str, Any]],
    ) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def get(
        self,
        pool: Any,
        since: datetime,
        until: datetime,
        *,
        resolver_factory: Callable[[], GeoResolver] = GeoResolver,
    ) -> dict[str, Any]:
        """Return a fresh cached payload, coalescing concurrent cache misses."""
        key = normalize_window(since, until)
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            self._entries.move_to_end(key)
            return entry.payload

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._scan(pool, key, resolver_factory))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._finish_scan(key, done))
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._inflight.get(key) is task:
                self._inflight.pop(key, None)


geo_demand_cache = GeoDemandCache()


async def get_geo_demand(
    pool: Any,
    since: datetime,
    until: datetime,
    *,
    resolver_factory: Callable[[], GeoResolver] = GeoResolver,
) -> dict[str, Any]:
    """Prefer complete persistent rollups and safely fall back to the raw cache."""
    resolver = resolver_factory()
    try:
        async with pool.acquire() as connection:
            payload = await aggregate_geo_demand_rollup(connection, since, until, resolver)
            if payload is None:
                stale_by = timedelta(hours=1)
                payload = await aggregate_geo_demand_rollup(
                    connection,
                    since - stale_by,
                    until - stale_by,
                    resolver,
                )
                if payload is not None:
                    payload["meta"]["notes"].append(
                        "latest complete hour is pending; serving the previous complete window"
                    )
    except Exception as exc:
        logger.warning("geo rollup read failed; falling back to api_logs: %s", exc)
        payload = None
    finally:
        resolver.close()

    if payload is not None:
        return payload
    return await geo_demand_cache.get(
        pool,
        since,
        until,
        resolver_factory=resolver_factory,
    )
