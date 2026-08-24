"""Focused cache and percentile-shape tests for admin performance metrics.

Covers both cached metric endpoints: the fixed-window
``/admin/performance-metrics`` and the filter-keyed
``/admin/recent-requests/performance`` breakdown.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.schemas_admin import (
    AdminPerformanceMetricsResponse,
    AdminRequestPerfBreakdownResponse,
)
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


# ---------------------------------------------------------------------------
# /admin/recent-requests/performance — per-filter cache
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_perf_breakdown_cache():
    """Keep the breakdown cache/lock state isolated between tests."""
    original_cache = dict(metrics._PERF_BREAKDOWN_CACHE)
    original_lock = metrics._PERF_BREAKDOWN_LOCK
    original_lock_loop = metrics._PERF_BREAKDOWN_LOCK_LOOP
    metrics._PERF_BREAKDOWN_CACHE.clear()
    metrics._PERF_BREAKDOWN_LOCK = None
    metrics._PERF_BREAKDOWN_LOCK_LOOP = None
    yield
    metrics._PERF_BREAKDOWN_CACHE.clear()
    metrics._PERF_BREAKDOWN_CACHE.update(original_cache)
    metrics._PERF_BREAKDOWN_LOCK = original_lock
    metrics._PERF_BREAKDOWN_LOCK_LOOP = original_lock_loop


def _breakdown(days: int = 7) -> AdminRequestPerfBreakdownResponse:
    return AdminRequestPerfBreakdownResponse(
        generated_at=datetime.now(timezone.utc), days=days, groups=[]
    )


_NO_FILTERS: dict[str, object] = {"user_id": None, "model_id": None, "request_type": None}


@pytest.mark.asyncio
async def test_breakdown_cache_reuses_fresh_result(monkeypatch):
    db_logger = _db_logger()
    response = _breakdown()
    loader = AsyncMock(return_value=response)
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    first = await metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)
    second = await metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)

    assert first is response
    assert second is response
    assert loader.await_count == 1


@pytest.mark.asyncio
async def test_breakdown_cache_is_keyed_by_filters(monkeypatch):
    """Different filters must never read each other's cached numbers."""
    db_logger = _db_logger()
    by_key = {
        (7, None, None, None): _breakdown(7),
        (30, None, None, None): _breakdown(30),
        (7, "alice", None, None): _breakdown(7),
        (7, None, "glm", None): _breakdown(7),
        (7, None, None, "chat"): _breakdown(7),
    }

    async def _load(_db_logger, *, days, user_id, model_id, request_type):
        return by_key[(days, user_id, model_id, request_type)]

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    async def _get(**kwargs):
        return await metrics._get_cached_request_perf_breakdown(
            db_logger, **{**{"days": 7, **_NO_FILTERS}, **kwargs}
        )

    assert await _get() is by_key[(7, None, None, None)]
    assert await _get(days=30) is by_key[(30, None, None, None)]
    assert await _get(user_id="alice") is by_key[(7, "alice", None, None)]
    assert await _get(model_id="glm") is by_key[(7, None, "glm", None)]
    assert await _get(request_type="chat") is by_key[(7, None, None, "chat")]
    assert loader.await_count == 5

    # Each of those five keys is now cached independently.
    assert await _get(days=30) is by_key[(30, None, None, None)]
    assert await _get() is by_key[(7, None, None, None)]
    assert loader.await_count == 5


@pytest.mark.asyncio
async def test_breakdown_empty_filter_strings_share_the_unfiltered_key(monkeypatch):
    db_logger = _db_logger()
    response = _breakdown()
    loader = AsyncMock(return_value=response)
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    await metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)
    await metrics._get_cached_request_perf_breakdown(
        db_logger, days=7, user_id="", model_id="", request_type=""
    )

    assert loader.await_count == 1


@pytest.mark.asyncio
async def test_breakdown_refresh_bypasses_cache(monkeypatch):
    db_logger = _db_logger()
    first_response = _breakdown()
    second_response = _breakdown()
    loader = AsyncMock(side_effect=[first_response, second_response])
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    await metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)
    refreshed = await metrics._get_cached_request_perf_breakdown(
        db_logger, days=7, refresh=True, **_NO_FILTERS
    )

    assert refreshed is second_response
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_breakdown_expired_entry_reloads(monkeypatch):
    db_logger = _db_logger()
    first_response = _breakdown()
    second_response = _breakdown()
    loader = AsyncMock(side_effect=[first_response, second_response])
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    await metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)
    key, (cached_at, cached) = next(iter(metrics._PERF_BREAKDOWN_CACHE.items()))
    metrics._PERF_BREAKDOWN_CACHE[key] = (
        cached_at - metrics._PERF_BREAKDOWN_CACHE_TTL_SECONDS - 1,
        cached,
    )

    refreshed = await metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)

    assert refreshed is second_response
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_breakdown_concurrent_identical_misses_share_one_load(monkeypatch):
    """The second caller waits and reads the cache instead of rescanning."""
    db_logger = _db_logger()
    response = _breakdown()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _load(_db_logger, **_kwargs):
        started.set()
        await release.wait()
        return response

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    first_task = asyncio.create_task(
        metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)
    )
    await started.wait()
    second_task = asyncio.create_task(
        metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)
    )
    await asyncio.sleep(0)

    assert loader.await_count == 1
    release.set()
    assert await first_task is response
    assert await second_task is response
    assert loader.await_count == 1


@pytest.mark.asyncio
async def test_breakdown_concurrent_misses_do_not_overlap(monkeypatch):
    """Distinct filters still queue: only one heavy scan runs at a time."""
    db_logger = _db_logger()
    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()

    async def _load(_db_logger, *, days, **_kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await release.wait()
            return _breakdown(days)
        finally:
            in_flight -= 1

    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", AsyncMock(side_effect=_load))

    tasks = [
        asyncio.create_task(
            metrics._get_cached_request_perf_breakdown(db_logger, days=days, **_NO_FILTERS)
        )
        for days in (1, 7, 30, 90)
    ]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert [r.days for r in results] == [1, 7, 30, 90]
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_breakdown_cache_is_bounded(monkeypatch):
    """A long session of filter edits can't grow the cache without limit."""
    db_logger = _db_logger()
    monkeypatch.setattr(
        metrics,
        "_load_request_perf_breakdown",
        AsyncMock(side_effect=lambda _db, **kwargs: _breakdown(kwargs["days"])),
    )

    for i in range(metrics._PERF_BREAKDOWN_CACHE_MAX_ENTRIES + 10):
        await metrics._get_cached_request_perf_breakdown(
            db_logger, days=7, user_id=f"user-{i}", model_id=None, request_type=None
        )

    assert len(metrics._PERF_BREAKDOWN_CACHE) == metrics._PERF_BREAKDOWN_CACHE_MAX_ENTRIES
    # The oldest keys are the ones evicted.
    cached_users = {key[2] for key in metrics._PERF_BREAKDOWN_CACHE}
    assert "user-0" not in cached_users
    assert f"user-{metrics._PERF_BREAKDOWN_CACHE_MAX_ENTRIES + 9}" in cached_users
