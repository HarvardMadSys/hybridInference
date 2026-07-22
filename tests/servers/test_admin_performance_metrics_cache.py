"""Focused cache and percentile-shape tests for admin performance metrics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.schemas_admin import AdminPerformanceMetricsResponse
from serving.servers.routers.admin import metrics


@pytest.fixture(autouse=True)
def _reset_performance_metrics_cache():
    """Keep module-level cache state isolated between tests."""
    original_cache = metrics._PERFORMANCE_METRICS_CACHE
    original_lock = metrics._PERFORMANCE_METRICS_LOCK
    original_lock_loop = metrics._PERFORMANCE_METRICS_LOCK_LOOP
    metrics._PERFORMANCE_METRICS_CACHE = None
    metrics._PERFORMANCE_METRICS_LOCK = None
    metrics._PERFORMANCE_METRICS_LOCK_LOOP = None
    yield
    metrics._PERFORMANCE_METRICS_CACHE = original_cache
    metrics._PERFORMANCE_METRICS_LOCK = original_lock
    metrics._PERFORMANCE_METRICS_LOCK_LOOP = original_lock_loop


def _db_logger() -> MagicMock:
    logger = MagicMock()
    logger.pool = object()
    return logger


def _response() -> AdminPerformanceMetricsResponse:
    return AdminPerformanceMetricsResponse(generated_at=datetime.now(timezone.utc), windows=[])


@pytest.mark.asyncio
async def test_performance_metrics_cache_reuses_fresh_result(monkeypatch):
    db_logger = _db_logger()
    response = _response()
    loader = AsyncMock(return_value=response)
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    first = await metrics._get_cached_performance_metrics(db_logger)
    second = await metrics._get_cached_performance_metrics(db_logger)

    assert first is response
    assert second is response
    assert loader.await_count == 1


@pytest.mark.asyncio
async def test_performance_metrics_refresh_bypasses_cache(monkeypatch):
    db_logger = _db_logger()
    first_response = _response()
    second_response = _response()
    loader = AsyncMock(side_effect=[first_response, second_response])
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    await metrics._get_cached_performance_metrics(db_logger)
    refreshed = await metrics._get_cached_performance_metrics(db_logger, refresh=True)

    assert refreshed is second_response
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_performance_metrics_expired_cache_reloads(monkeypatch):
    db_logger = _db_logger()
    first_response = _response()
    second_response = _response()
    loader = AsyncMock(side_effect=[first_response, second_response])
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    await metrics._get_cached_performance_metrics(db_logger)
    cached_at, pool_id, cached_response = metrics._PERFORMANCE_METRICS_CACHE
    metrics._PERFORMANCE_METRICS_CACHE = (
        cached_at - metrics._PERFORMANCE_METRICS_CACHE_TTL_SECONDS - 1,
        pool_id,
        cached_response,
    )

    refreshed = await metrics._get_cached_performance_metrics(db_logger)

    assert refreshed is second_response
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_performance_metrics_concurrent_misses_share_one_load(monkeypatch):
    db_logger = _db_logger()
    response = _response()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _load(_db_logger):
        started.set()
        await release.wait()
        return response

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    first_task = asyncio.create_task(metrics._get_cached_performance_metrics(db_logger))
    await started.wait()
    second_task = asyncio.create_task(metrics._get_cached_performance_metrics(db_logger))
    await asyncio.sleep(0)

    assert loader.await_count == 1
    release.set()
    assert await first_task is response
    assert await second_task is response
    assert loader.await_count == 1


def test_distribution_reads_array_percentiles_from_postgres():
    row = {
        "pt_count": 4,
        "pt_mean": 25.0,
        "pt_min": 10.0,
        "pt_max": 40.0,
        "pt_percentiles": [15.0, 30.0, 35.0, 39.0],
    }

    distribution = metrics._distribution_from_row(row, "pt", (0, 50), {1: 4})

    assert (distribution.p50, distribution.p90, distribution.p95, distribution.p99) == (
        15.0,
        30.0,
        35.0,
        39.0,
    )
