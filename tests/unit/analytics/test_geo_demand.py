"""Tests for the geo demand aggregation service and cache."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from serving.analytics import geo_demand
from serving.analytics.geo_demand import (
    BUCKET_COLS,
    FLOW_COLS,
    GeoDemandCache,
    aggregate_geo_demand,
    normalize_window,
    percentile,
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
        "provider": "vllm",
        "served_endpoint_id": "vllm:default:8000",
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "latency_ms": 1500,
        "ttft_ms": 20,
        "is_err": False,
        "user_id": "secret-user-id",
    }
    value.update(overrides)
    return value


def resolver() -> GeoResolver:
    return GeoResolver(
        country_reader=FakeReader(
            {"8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}}}
        )
    )


def test_nearest_rank_percentile() -> None:
    assert percentile([], 0.5) is None
    assert percentile([10, 20, 30], 0.5) == 20
    assert percentile([10, 20, 30], 0.9) == 30


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
async def test_aggregation_contract_percentiles_and_no_raw_identifiers() -> None:
    connection = FakeConnection(
        [
            row(0, ttft_ms=10),
            row(0, ttft_ms=20, is_err=True, status_code=500),
            row(
                0,
                ttft_ms=30,
                provider="openrouter",
                served_endpoint_id=None,
                user_id="another-secret",
            ),
        ]
    )

    payload = await aggregate_geo_demand(connection, utc(0), utc(2), resolver())

    assert connection.transaction_entered is True
    assert connection.cursor_kwargs == {"prefetch": 5000}
    assert "served_endpoint_id" in connection.cursor_args[0]
    assert payload["bucket_cols"] == BUCKET_COLS
    assert payload["flow_cols"] == FLOW_COLS
    assert BUCKET_COLS == [
        "c",
        "cc",
        "cont",
        "n",
        "err",
        "users",
        "tin",
        "tout",
        "gs",
        "p50",
        "p90",
    ]
    assert FLOW_COLS == ["c", "p", "e", "n"]
    assert "classes" not in payload
    assert payload["hours_index"] == [utc(0).isoformat(), utc(1).isoformat()]
    assert payload["hours"][1] == {"b": [], "f": []}

    bucket = dict(zip(BUCKET_COLS, payload["hours"][0]["b"][0], strict=True))
    assert bucket == {
        "c": "USA",
        "cc": "US",
        "cont": "NA",
        "n": 3,
        "err": 1,
        "users": 2,
        "tin": 30,
        "tout": 12,
        "gs": 4.5,
        "p50": 20,
        "p90": 30,
    }
    providers = {provider["id"]: provider for provider in payload["providers"]}
    assert providers["vllm"]["kind"] == "local"
    assert providers["vllm"]["coord"] is not None
    assert providers["openrouter"]["kind"] == "remote_api"
    assert providers["openrouter"]["coord"] is None

    serialized = json.dumps(payload)
    assert "8.8.8.8" not in serialized
    assert "secret-user-id" not in serialized
    assert "another-secret" not in serialized
    assert payload["meta"]["geoip"] == {"country": True}
    assert payload["meta"]["degraded"] is False


@pytest.mark.asyncio
async def test_flows_keep_endpoints_separate_within_one_provider() -> None:
    connection = FakeConnection(
        [
            row(0, served_endpoint_id="vllm:us-east:8000"),
            row(0, served_endpoint_id="vllm:eu-west:8000"),
            row(0, served_endpoint_id=None),
        ]
    )

    payload = await aggregate_geo_demand(connection, utc(0), utc(1), resolver())

    flows = [dict(zip(FLOW_COLS, values, strict=True)) for values in payload["hours"][0]["f"]]
    assert {flow["e"] for flow in flows} == {
        "vllm:us-east:8000",
        "vllm:eu-west:8000",
        "vllm",
    }
    assert all(flow["p"] == "vllm" for flow in flows)
    assert all(flow["n"] == 1 for flow in flows)


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
