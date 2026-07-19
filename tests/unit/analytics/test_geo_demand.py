"""Tests for the geo demand aggregation service and cache."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.analytics import geo_demand
from serving.analytics.geo_demand import (
    BUCKET_COLS,
    GeoDemandCache,
    aggregate_geo_demand,
    aggregate_geo_demand_rollup,
    get_geo_demand,
    normalize_window,
)
from serving.utils.geo_resolver import GeoResolver


class FakeReader:
    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self.records = records

    def get(self, ip: str) -> dict[str, Any] | None:
        return self.records.get(ip)

    def close(self) -> None:
        pass


class AsyncRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


class AsyncContext:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.cursor_args: tuple[Any, ...] | None = None
        self.cursor_kwargs: dict[str, Any] | None = None
        self.transaction_entered = False

    def transaction(self):
        connection = self

        class Transaction(AsyncContext):
            async def __aenter__(self):
                connection.transaction_entered = True

        return Transaction()

    def cursor(self, *args, **kwargs):
        assert self.transaction_entered
        self.cursor_args = args
        self.cursor_kwargs = kwargs
        return AsyncRows(list(self.rows))


def utc(hour: int) -> datetime:
    return datetime(2026, 7, 1, hour, tzinfo=timezone.utc)


def row(hour: int, **overrides: Any) -> dict[str, Any]:
    value = {
        "hour": utc(hour),
        "ip": "8.8.8.8",
        "request_count": 1,
        "completion_tokens": 4,
    }
    value.update(overrides)
    return value


def resolver() -> GeoResolver:
    return GeoResolver(
        country_reader=FakeReader(
            {"8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}}}
        )
    )


def test_window_alignment_and_validation() -> None:
    start, end = normalize_window(
        datetime(2026, 7, 1, 3, 41, tzinfo=timezone.utc),
        datetime(2026, 7, 2, 4, 59, tzinfo=timezone.utc),
    )
    assert start == datetime(2026, 7, 1, 3, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 2, 4, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone"):
        normalize_window(datetime(2026, 7, 1), datetime(2026, 7, 2))
    with pytest.raises(ValueError, match="90 days"):
        normalize_window(utc(0), utc(0) + timedelta(days=91))


@pytest.mark.asyncio
async def test_aggregation_contract_is_demand_only_and_has_no_raw_identifiers() -> None:
    connection = FakeConnection(
        [
            row(0, user_id="secret-user-id"),
            row(0, completion_tokens=None),
            row(0, completion_tokens=8, user_id="another-secret"),
        ]
    )

    payload = await aggregate_geo_demand(connection, utc(0), utc(2), resolver())

    assert connection.transaction_entered is True
    assert connection.cursor_kwargs == {"prefetch": 5000}
    query = connection.cursor_args[0]
    assert "completion_tokens" in query
    assert "metadata->>'ip'" in query
    assert "metadata->>'synthetic_probe'" in query
    assert "provider" not in query
    assert "served_endpoint_id" not in query
    assert "prompt_tokens" not in query
    assert "latency_ms" not in query
    assert "ttft_ms" not in query
    assert "user_id" not in query
    assert "ORDER BY" not in query
    assert "GROUP BY 1, 2" in query
    assert payload["bucket_cols"] == BUCKET_COLS
    assert BUCKET_COLS == ["c", "cont", "n", "tout"]
    assert "flow_cols" not in payload
    assert "providers" not in payload
    assert "classes" not in payload
    assert payload["hours_index"] == [utc(0).isoformat(), utc(1).isoformat()]
    assert payload["hours"][1] == {"b": []}

    bucket = dict(zip(BUCKET_COLS, payload["hours"][0]["b"][0], strict=True))
    assert bucket == {
        "c": "USA",
        "cont": "NA",
        "n": 3,
        "tout": 12,
    }

    serialized = json.dumps(payload)
    assert "8.8.8.8" not in serialized
    assert "secret-user-id" not in serialized
    assert "another-secret" not in serialized
    assert payload["meta"]["geoip"] == {
        "country": True,
        "provider": None,
        "attribution": None,
    }
    assert payload["meta"]["degraded"] is False
    assert payload["meta"]["rows_total"] == 3
    assert payload["meta"]["rows_with_ip"] == 3


@pytest.mark.asyncio
async def test_aggregation_identifies_dbip_lite_and_required_attribution() -> None:
    connection = FakeConnection([row(0)])
    dbip_resolver = GeoResolver(
        country_reader=FakeReader(
            {"8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}}}
        ),
        country_provider="dbip-lite",
    )

    payload = await aggregate_geo_demand(connection, utc(0), utc(1), dbip_resolver)

    assert payload["meta"]["geoip"] == {
        "country": True,
        "provider": "dbip-lite",
        "attribution": {
            "label": "IP Geolocation by DB-IP",
            "url": "https://db-ip.com",
        },
    }


@pytest.mark.asyncio
async def test_aggregation_retains_unknown_origin_bucket() -> None:
    connection = FakeConnection([row(0, ip=None)])

    payload = await aggregate_geo_demand(connection, utc(0), utc(1), resolver())

    bucket = dict(zip(BUCKET_COLS, payload["hours"][0]["b"][0], strict=True))
    assert bucket == {"c": "?", "cont": "?", "n": 1, "tout": 4}
    assert payload["meta"]["rows_total"] == 1
    assert payload["meta"]["rows_with_ip"] == 0


@pytest.mark.asyncio
async def test_grouped_raw_rows_preserve_request_and_ip_count_semantics() -> None:
    connection = FakeConnection(
        [
            row(0, request_count=7, completion_tokens=21),
            row(0, ip=None, request_count=3, completion_tokens=5),
        ]
    )

    payload = await aggregate_geo_demand(connection, utc(0), utc(1), resolver())

    assert payload["meta"]["rows_total"] == 10
    assert payload["meta"]["rows_with_ip"] == 7
    buckets = {
        item[0]: dict(zip(BUCKET_COLS, item, strict=True)) for item in payload["hours"][0]["b"]
    }
    assert buckets["USA"] == {"c": "USA", "cont": "NA", "n": 7, "tout": 21}
    assert buckets["?"] == {"c": "?", "cont": "?", "n": 3, "tout": 5}


@pytest.mark.asyncio
async def test_rollup_requires_gap_free_coverage_and_preserves_contract() -> None:
    connection = MagicMock()
    connection.transaction.return_value = AsyncContext()
    connection.fetchrow = AsyncMock(
        return_value={
            "complete_hours": 2,
            "rows_total": 12,
            "rows_with_ip": 9,
            "completed_at": utc(2),
        }
    )
    connection.fetch = AsyncMock(
        return_value=[
            {
                "hour": utc(0),
                "country_code": "USA",
                "continent_code": "NA",
                "request_count": 9,
                "completion_tokens": 40,
            }
        ]
    )

    payload = await aggregate_geo_demand_rollup(connection, utc(0), utc(2), resolver())

    assert payload is not None
    assert payload["bucket_cols"] == BUCKET_COLS
    assert payload["hours_index"] == [utc(0).isoformat(), utc(1).isoformat()]
    assert payload["hours"][1] == {"b": []}
    assert payload["meta"]["rows_total"] == 12
    assert payload["meta"]["rows_with_ip"] == 9
    assert payload["meta"]["generated_at"] == utc(2).isoformat()

    connection.fetchrow.return_value = {
        "complete_hours": 1,
        "rows_total": 10,
        "rows_with_ip": 8,
    }
    assert await aggregate_geo_demand_rollup(connection, utc(0), utc(2), resolver()) is None
    assert connection.fetch.await_count == 1


def test_cache_defaults_to_four_entries() -> None:
    assert GeoDemandCache().max_entries == 4


@pytest.mark.asyncio
async def test_cache_coalesces_concurrent_misses(monkeypatch) -> None:
    cache = GeoDemandCache(ttl_seconds=60)
    connection = object()

    class Pool:
        def acquire(self):
            return AsyncContext(connection)

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_aggregate(conn, since, until, geo_resolver):
        assert conn is connection
        started.set()
        await release.wait()
        return {"meta": {"generated_at": "once"}}

    aggregate = AsyncMock(side_effect=fake_aggregate)
    monkeypatch.setattr(geo_demand, "aggregate_geo_demand", aggregate)

    first = asyncio.create_task(cache.get(Pool(), utc(0), utc(1)))
    await started.wait()
    second = asyncio.create_task(cache.get(Pool(), utc(0), utc(1)))
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result is second_result
    assert aggregate.await_count == 1
    cached = await cache.get(Pool(), utc(0), utc(1))
    assert cached is first_result
    assert aggregate.await_count == 1


@pytest.mark.asyncio
async def test_cache_clears_failed_inflight_scan_and_allows_retry(monkeypatch) -> None:
    cache = GeoDemandCache(ttl_seconds=60)

    class Pool:
        def acquire(self):
            return AsyncContext(object())

    aggregate = AsyncMock(
        side_effect=[RuntimeError("scan failed"), {"meta": {"generated_at": "retry"}}]
    )
    monkeypatch.setattr(geo_demand, "aggregate_geo_demand", aggregate)

    with pytest.raises(RuntimeError, match="scan failed"):
        await cache.get(Pool(), utc(0), utc(1))
    assert cache._inflight == {}

    result = await cache.get(Pool(), utc(0), utc(1))
    assert result == {"meta": {"generated_at": "retry"}}
    assert aggregate.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("rollup_error", [False, True])
async def test_geo_service_falls_back_to_raw_cache(monkeypatch, rollup_error) -> None:
    connection = MagicMock()
    if rollup_error:
        connection.fetchrow = AsyncMock(side_effect=RuntimeError("rollup unavailable"))
    else:
        connection.fetchrow = AsyncMock(
            return_value={"complete_hours": 0, "rows_total": 0, "rows_with_ip": 0}
        )

    class Pool:
        def acquire(self):
            return AsyncContext(connection)

    cached = AsyncMock(return_value={"meta": {"source": "api_logs"}})
    monkeypatch.setattr(geo_demand.geo_demand_cache, "get", cached)

    payload = await get_geo_demand(Pool(), utc(0), utc(1), resolver_factory=resolver)

    assert payload == {"meta": {"source": "api_logs"}}
    cached.assert_awaited_once()


@pytest.mark.asyncio
async def test_geo_service_prefers_complete_rollup(monkeypatch) -> None:
    connection = MagicMock()
    connection.transaction.return_value = AsyncContext()
    connection.fetchrow = AsyncMock(
        return_value={
            "complete_hours": 1,
            "rows_total": 5,
            "rows_with_ip": 5,
            "completed_at": utc(1),
        }
    )
    connection.fetch = AsyncMock(
        return_value=[
            {
                "hour": utc(0),
                "country_code": "USA",
                "continent_code": "NA",
                "request_count": 5,
                "completion_tokens": 15,
            }
        ]
    )

    class Pool:
        def acquire(self):
            return AsyncContext(connection)

    cached = AsyncMock()
    monkeypatch.setattr(geo_demand.geo_demand_cache, "get", cached)

    payload = await get_geo_demand(Pool(), utc(0), utc(1), resolver_factory=resolver)

    assert payload["hours"][0]["b"] == [["USA", "NA", 5, 15]]
    cached.assert_not_awaited()


@pytest.mark.asyncio
async def test_geo_service_serves_previous_complete_rollup_while_latest_hour_is_pending(
    monkeypatch,
) -> None:
    connection = MagicMock()
    connection.transaction.return_value = AsyncContext()
    connection.fetchrow = AsyncMock(
        side_effect=[
            {"complete_hours": 0, "rows_total": 0, "rows_with_ip": 0},
            {
                "complete_hours": 1,
                "rows_total": 5,
                "rows_with_ip": 5,
                "completed_at": utc(0),
            },
        ]
    )
    connection.fetch = AsyncMock(
        return_value=[
            {
                "hour": utc(0) - timedelta(hours=1),
                "country_code": "USA",
                "continent_code": "NA",
                "request_count": 5,
                "completion_tokens": 15,
            }
        ]
    )

    class Pool:
        def acquire(self):
            return AsyncContext(connection)

    cached = AsyncMock()
    monkeypatch.setattr(geo_demand.geo_demand_cache, "get", cached)

    payload = await get_geo_demand(Pool(), utc(0), utc(1), resolver_factory=resolver)

    assert payload["hours_index"] == [(utc(0) - timedelta(hours=1)).isoformat()]
    assert payload["hours"][0]["b"] == [["USA", "NA", 5, 15]]
    assert "previous complete window" in payload["meta"]["notes"][-1]
    cached.assert_not_awaited()
