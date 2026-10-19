"""Focused cache and percentile-shape tests for admin performance metrics.

Covers the cached metric endpoints: the fixed-window
``/admin/performance-metrics`` (and its startup preparation with
``/admin/ttft-scatter``) and the filter-keyed ``/admin/recent-requests/performance``
breakdown. The shared refreshing cache itself is covered in
``test_admin_refreshing_cache.py``, and the stored results and covering index
against Postgres in ``tests/integration/test_admin_performance_views_postgres.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.schemas_admin import (
    AdminPerformanceMetricsResponse,
    AdminRequestPerfBreakdownResponse,
    AdminTtftScatterResponse,
)
from serving.servers.routers.admin import metrics


class _MemoryStore:
    """Stands in for ``admin_view_results``: these tests have no database."""

    def __init__(self) -> None:
        self.rows: dict = {}

    async def read(self, key):
        return self.rows.get(key)

    async def write(self, key, value, computed_at):
        self.rows[key] = (value, computed_at)


@pytest.fixture(autouse=True)
def _reset_performance_caches(monkeypatch):
    """Keep the module-level cached results isolated between tests."""
    for cache in (metrics.PERFORMANCE_METRICS, metrics.TTFT_SCATTER):
        cache.clear()
        monkeypatch.setattr(cache, "store", _MemoryStore())
    monkeypatch.setattr(metrics.perf_metrics_index, "ensure_index", AsyncMock(return_value=True))
    yield
    metrics.PERFORMANCE_METRICS.clear()
    metrics.TTFT_SCATTER.clear()


def _db_logger() -> MagicMock:
    logger = MagicMock()
    logger.pool = object()
    return logger


def _response(count: int = 0) -> AdminPerformanceMetricsResponse:
    """A response told apart by *count*, as the cache returns copies."""
    window = metrics.AdminPerformanceMetricsWindow.model_validate(
        {
            "key": "1h",
            "label": "Last 1 hour",
            "window_minutes": 60,
            **{
                name: {"count": count, "histogram": []}
                for name in ("prompt_tokens", "completion_tokens", "ttft_ms", "throughput_tps")
            },
        }
    )
    return AdminPerformanceMetricsResponse(
        generated_at=datetime.now(timezone.utc), windows=[window]
    )


def _count(response: AdminPerformanceMetricsResponse) -> int:
    return response.windows[0].prompt_tokens.count


def _age(cache, seconds: float) -> None:
    """Make *cache*'s result *seconds* older than its freshness window."""
    cache._computed_at -= cache.fresh_seconds + seconds


@pytest.mark.asyncio
async def test_performance_metrics_cache_reuses_fresh_result(monkeypatch):
    db_logger = _db_logger()
    loader = AsyncMock(return_value=_response(1))
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    first = await metrics._get_cached_performance_metrics(db_logger)
    second = await metrics._get_cached_performance_metrics(db_logger)

    assert _count(first) == _count(second) == 1
    assert not first.refreshing
    assert loader.await_count == 1


@pytest.mark.asyncio
async def test_performance_metrics_refresh_returns_at_once_and_says_it_is_refreshing(
    monkeypatch,
):
    db_logger = _db_logger()
    release = asyncio.Event()

    async def _load(_db_logger):
        if loader.await_count == 1:
            return _response(1)
        await release.wait()
        return _response(2)

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    await metrics._get_cached_performance_metrics(db_logger)
    during = await metrics._get_cached_performance_metrics(db_logger, refresh=True)
    assert (_count(during), during.refreshing) == (1, True)

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    after = await metrics._get_cached_performance_metrics(db_logger)
    assert (_count(after), after.refreshing) == (2, False)
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_performance_metrics_stale_result_is_served_while_it_refreshes(monkeypatch):
    db_logger = _db_logger()
    release = asyncio.Event()

    async def _load(_db_logger):
        if loader.await_count == 1:
            return _response(1)
        await release.wait()
        return _response(2)

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    await metrics._get_cached_performance_metrics(db_logger)
    _age(metrics.PERFORMANCE_METRICS, 1)

    # The stale result comes back at once; the refresh runs behind it.
    assert _count(await metrics._get_cached_performance_metrics(db_logger)) == 1
    assert _count(await metrics._get_cached_performance_metrics(db_logger)) == 1
    await asyncio.sleep(0)
    assert loader.await_count == 2  # one refresh for both stale reads
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert _count(await metrics._get_cached_performance_metrics(db_logger)) == 2
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_performance_metrics_with_no_result_yet_is_an_empty_refreshing_response(
    monkeypatch,
):
    """A first computation longer than the wait answers empty, not after the proxy gave up."""
    db_logger = _db_logger()
    release = asyncio.Event()

    async def _load(_db_logger):
        await release.wait()
        return _response(1)

    monkeypatch.setattr(metrics, "_load_performance_metrics", AsyncMock(side_effect=_load))
    monkeypatch.setattr(metrics.PERFORMANCE_METRICS, "first_wait_seconds", 0.01)

    pending = await metrics._get_cached_performance_metrics(db_logger)
    assert (pending.generated_at, pending.windows, pending.refreshing) == (None, [], True)

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert _count(await metrics._get_cached_performance_metrics(db_logger)) == 1


@pytest.mark.asyncio
async def test_performance_metrics_concurrent_misses_share_one_load(monkeypatch):
    db_logger = _db_logger()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _load(_db_logger):
        started.set()
        await release.wait()
        return _response(1)

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(metrics, "_load_performance_metrics", loader)

    first_task = asyncio.create_task(metrics._get_cached_performance_metrics(db_logger))
    await started.wait()
    second_task = asyncio.create_task(metrics._get_cached_performance_metrics(db_logger))
    await asyncio.sleep(0)

    assert loader.await_count == 1
    release.set()
    assert _count(await first_task) == _count(await second_task) == 1
    assert loader.await_count == 1


@pytest.mark.asyncio
async def test_preparation_builds_the_index_then_fills_both_views_one_after_the_other(
    monkeypatch,
):
    db_logger = _db_logger()
    order: list[str] = []
    in_flight = 0

    def _step(name, response=None):
        async def _run(*_args):
            nonlocal in_flight
            in_flight += 1
            assert in_flight == 1, "preparation must not run two heavy steps at once"
            order.append(name)
            await asyncio.sleep(0)
            in_flight -= 1
            return response

        return AsyncMock(side_effect=_run)

    perf = _step("perf", _response(1))
    scatter = _step(
        "scatter", AdminTtftScatterResponse(generated_at=datetime.now(timezone.utc), models=[])
    )
    monkeypatch.setattr(metrics.perf_metrics_index, "ensure_index", _step("index", True))
    monkeypatch.setattr(metrics, "_load_performance_metrics", perf)
    monkeypatch.setattr(metrics, "_load_ttft_scatter", scatter)

    await metrics.warm_performance_caches(db_logger)

    assert order == ["index", "perf", "scatter"]
    # Requests after the warm-up are served from memory.
    await metrics._get_cached_performance_metrics(db_logger)
    assert perf.await_count == 1


@pytest.mark.asyncio
async def test_preparation_goes_on_without_the_index_when_its_build_fails(monkeypatch, caplog):
    db_logger = _db_logger()
    monkeypatch.setattr(
        metrics.perf_metrics_index,
        "ensure_index",
        AsyncMock(side_effect=RuntimeError("disk full")),
    )
    perf = AsyncMock(return_value=_response(1))
    monkeypatch.setattr(metrics, "_load_performance_metrics", perf)
    monkeypatch.setattr(
        metrics,
        "_load_ttft_scatter",
        AsyncMock(
            return_value=AdminTtftScatterResponse(
                generated_at=datetime.now(timezone.utc), models=[]
            )
        ),
    )

    await metrics.warm_performance_caches(db_logger)

    assert "build failed" in caplog.text
    assert perf.await_count == 1


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


_NO_FILTERS: dict[str, object] = {
    "user_id": None,
    "session_id": None,
    "model_id": None,
    "request_type": None,
}


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
        (7, None, None, None, None): _breakdown(7),
        (30, None, None, None, None): _breakdown(30),
        (7, "alice", None, None, None): _breakdown(7),
        (7, None, "sess-1", None, None): _breakdown(7),
        (7, None, None, "glm", None): _breakdown(7),
        (7, None, None, None, "chat"): _breakdown(7),
    }

    async def _load(_db_logger, *, days, user_id, session_id, model_id, request_type):
        return by_key[(days, user_id, session_id, model_id, request_type)]

    loader = AsyncMock(side_effect=_load)
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    async def _get(**kwargs):
        return await metrics._get_cached_request_perf_breakdown(
            db_logger, **{**{"days": 7, **_NO_FILTERS}, **kwargs}
        )

    assert await _get() is by_key[(7, None, None, None, None)]
    assert await _get(days=30) is by_key[(30, None, None, None, None)]
    assert await _get(user_id="alice") is by_key[(7, "alice", None, None, None)]
    assert await _get(session_id="sess-1") is by_key[(7, None, "sess-1", None, None)]
    assert await _get(model_id="glm") is by_key[(7, None, None, "glm", None)]
    assert await _get(request_type="chat") is by_key[(7, None, None, None, "chat")]
    assert loader.await_count == 6

    # Each of those six keys is now cached independently.
    assert await _get(days=30) is by_key[(30, None, None, None, None)]
    assert await _get() is by_key[(7, None, None, None, None)]
    assert loader.await_count == 6


@pytest.mark.asyncio
async def test_breakdown_empty_filter_strings_share_the_unfiltered_key(monkeypatch):
    db_logger = _db_logger()
    response = _breakdown()
    loader = AsyncMock(return_value=response)
    monkeypatch.setattr(metrics, "_load_request_perf_breakdown", loader)

    await metrics._get_cached_request_perf_breakdown(db_logger, days=7, **_NO_FILTERS)
    await metrics._get_cached_request_perf_breakdown(
        db_logger, days=7, user_id="", session_id="", model_id="", request_type=""
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
            db_logger,
            days=7,
            user_id=f"user-{i}",
            session_id=None,
            model_id=None,
            request_type=None,
        )

    assert len(metrics._PERF_BREAKDOWN_CACHE) == metrics._PERF_BREAKDOWN_CACHE_MAX_ENTRIES
    # The oldest keys are the ones evicted.
    cached_users = {key[2] for key in metrics._PERF_BREAKDOWN_CACHE}
    assert "user-0" not in cached_users
    assert f"user-{metrics._PERF_BREAKDOWN_CACHE_MAX_ENTRIES + 9}" in cached_users
