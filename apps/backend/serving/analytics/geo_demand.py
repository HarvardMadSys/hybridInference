"""Aggregate privacy-safe geo-temporal demand from ``api_logs``."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from serving.utils.geo_resolver import GeoResolver

if TYPE_CHECKING:
    from collections.abc import Callable

BUCKET_COLS = ["c", "cont", "n", "tout"]
MAX_WINDOW = timedelta(days=90)

ROWS_QUERY = """
SELECT
  date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour,
  metadata->>'ip'               AS ip,
  completion_tokens
FROM api_logs
WHERE timestamp >= $1 AND timestamp < $2
  AND COALESCE(metadata->>'synthetic_probe', 'false') <> 'true'
"""


class GeoBucket:
    """Mutable accumulator for one hour and country bucket."""

    __slots__ = ("n", "tout")

    def __init__(self) -> None:
        self.n = 0
        self.tout = 0


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


def add_geo_row(
    row: Any,
    resolver: GeoResolver,
    buckets: dict[tuple[datetime, str, str], GeoBucket],
) -> bool:
    """Resolve and add one database row; return whether it carried an IP."""
    ip = row["ip"]
    alpha3, _, continent = resolver.resolve(ip)

    bucket = buckets[(row["hour"], alpha3, continent)]
    bucket.n += 1
    bucket.tout += row["completion_tokens"] or 0
    return bool(ip)


async def aggregate_geo_demand(
    connection: Any,
    since: datetime,
    until: datetime,
    resolver: GeoResolver,
    *,
    prefetch: int = 5_000,
) -> dict[str, Any]:
    """Stream one window from Postgres and return aggregate-only globe data."""
    since, until = normalize_window(since, until)
    buckets: dict[tuple[datetime, str, str], GeoBucket] = defaultdict(GeoBucket)
    rows_total = 0
    rows_with_ip = 0

    async with connection.transaction():
        cursor = connection.cursor(ROWS_QUERY, since, until, prefetch=prefetch)
        async for row in cursor:
            rows_total += 1
            rows_with_ip += add_geo_row(row, resolver, buckets)
            if rows_total % 10_000 == 0:
                await asyncio.sleep(0)

    hours_index = build_hours_index(since, until)
    meta = {
        "source": "api_logs",
        "generated_at": datetime.now(timezone.utc).isoformat(),
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
