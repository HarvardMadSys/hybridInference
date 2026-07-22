from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from serving.config.weight_overrides import WeightOverrideResolver


@pytest.mark.asyncio
async def test_get_for_model_caches_store_rows_until_invalidated():
    store = AsyncMock()
    store.list_weight_overrides_for_model.return_value = [
        {"endpoint_id": "m:local", "weight": 0.25},
        {"endpoint_id": "m:remote", "weight": 2},
    ]
    resolver = WeightOverrideResolver(store, ttl=60.0)

    first = await resolver.get_for_model("m")
    second = await resolver.get_for_model("m")

    assert first == {"m:local": 0.25, "m:remote": 2.0}
    assert second == first
    store.list_weight_overrides_for_model.assert_awaited_once_with("m")


@pytest.mark.asyncio
async def test_invalidate_model_clears_one_cached_entry():
    store = AsyncMock()
    store.list_weight_overrides_for_model.side_effect = [
        [{"endpoint_id": "m:local", "weight": 1}],
        [{"endpoint_id": "n:local", "weight": 2}],
        [{"endpoint_id": "m:local", "weight": 3}],
    ]
    resolver = WeightOverrideResolver(store, ttl=60.0)

    assert await resolver.get_for_model("m") == {"m:local": 1.0}
    assert await resolver.get_for_model("n") == {"n:local": 2.0}

    resolver.invalidate_model("m")

    assert await resolver.get_for_model("m") == {"m:local": 3.0}
    assert await resolver.get_for_model("n") == {"n:local": 2.0}
    assert store.list_weight_overrides_for_model.await_count == 3


@pytest.mark.asyncio
async def test_clear_model_removes_sync_snapshot_and_fences_inflight_fetch():
    release_fetch = asyncio.get_running_loop().create_future()

    async def list_for_model(_model_id: str):
        await release_fetch
        return [{"endpoint_id": "m:stale", "weight": 9}]

    store = AsyncMock()
    store.list_all_weight_overrides.return_value = [
        {"model_id": "m", "endpoint_id": "m:visible", "weight": 2}
    ]
    store.list_weight_overrides_for_model.side_effect = list_for_model
    resolver = WeightOverrideResolver(store)
    await resolver.load_all()
    task = asyncio.create_task(resolver.get_for_model("m"))
    await asyncio.sleep(0)

    resolver.clear_model("m")
    release_fetch.set_result(None)

    assert await task == {"m:stale": 9.0}
    assert resolver.get_snapshot_for_model("m") == {}


@pytest.mark.asyncio
async def test_invalidate_cache_clears_all_entries():
    store = AsyncMock()
    store.list_weight_overrides_for_model.side_effect = [
        [{"endpoint_id": "m:local", "weight": 1}],
        [{"endpoint_id": "n:local", "weight": 2}],
        [{"endpoint_id": "m:local", "weight": 3}],
        [{"endpoint_id": "n:local", "weight": 4}],
    ]
    resolver = WeightOverrideResolver(store, ttl=60.0)

    await resolver.get_for_model("m")
    await resolver.get_for_model("n")
    resolver.invalidate_cache()

    assert await resolver.get_for_model("m") == {"m:local": 3.0}
    assert await resolver.get_for_model("n") == {"n:local": 4.0}


@pytest.mark.asyncio
async def test_cache_entry_expires_after_ttl(monkeypatch):
    now = 100.0
    monkeypatch.setattr("serving.config.weight_overrides.time.monotonic", lambda: now)
    store = AsyncMock()
    store.list_weight_overrides_for_model.side_effect = [
        [{"endpoint_id": "m:local", "weight": 1}],
        [{"endpoint_id": "m:local", "weight": 2}],
    ]
    resolver = WeightOverrideResolver(store, ttl=10.0)

    assert await resolver.get_for_model("m") == {"m:local": 1.0}
    now = 111.0

    assert await resolver.get_for_model("m") == {"m:local": 2.0}


@pytest.mark.asyncio
async def test_cache_timestamp_is_recorded_after_store_fetch(monkeypatch):
    now = 100.0

    def monotonic() -> float:
        return now

    async def list_for_model(_model_id: str):
        nonlocal now
        now = 109.0
        return [{"endpoint_id": "m:local", "weight": 1}]

    monkeypatch.setattr("serving.config.weight_overrides.time.monotonic", monotonic)
    store = AsyncMock()
    store.list_weight_overrides_for_model.side_effect = list_for_model
    resolver = WeightOverrideResolver(store, ttl=10.0)

    assert await resolver.get_for_model("m") == {"m:local": 1.0}
    now = 118.0
    assert await resolver.get_for_model("m") == {"m:local": 1.0}
    store.list_weight_overrides_for_model.assert_awaited_once_with("m")


@pytest.mark.asyncio
async def test_load_all_refreshes_snapshot_from_store():
    store = AsyncMock()
    store.list_all_weight_overrides.return_value = [
        {"model_id": "m", "endpoint_id": "m:local", "weight": 0.5},
        {"model_id": "m", "endpoint_id": "m:remote", "weight": 2},
        {"model_id": "n", "endpoint_id": "n:remote", "weight": 3},
    ]
    resolver = WeightOverrideResolver(store)

    assert await resolver.load_all() is True

    assert resolver.get_snapshot_for_model("m") == {"m:local": 0.5, "m:remote": 2.0}
    assert resolver.get_snapshot_for_model("n") == {"n:remote": 3.0}


@pytest.mark.asyncio
async def test_load_all_reports_only_content_changes_independent_of_row_order():
    store = AsyncMock()
    first = [
        {"model_id": "m", "endpoint_id": "m:local", "weight": 0.5},
        {"model_id": "m", "endpoint_id": "m:remote", "weight": 2},
    ]
    store.list_all_weight_overrides.side_effect = [
        first,
        list(reversed(first)),
        [{"model_id": "m", "endpoint_id": "m:local", "weight": 0.75}],
    ]
    resolver = WeightOverrideResolver(store)

    assert await resolver.load_all() is True
    assert await resolver.load_all() is False
    assert await resolver.load_all() is True


@pytest.mark.asyncio
async def test_unchanged_load_all_still_fences_stale_per_model_fetch():
    release_fetch = None

    async def list_for_model(_model_id: str):
        if release_fetch is None:
            raise AssertionError("release future was not initialized")
        await release_fetch
        return [{"endpoint_id": "m:remote", "weight": 2}]

    store = AsyncMock()
    store.list_all_weight_overrides.return_value = []
    store.list_weight_overrides_for_model.side_effect = list_for_model
    resolver = WeightOverrideResolver(store)
    assert await resolver.load_all() is False
    release_fetch = asyncio.get_running_loop().create_future()

    task = asyncio.create_task(resolver.get_for_model("m"))
    await asyncio.sleep(0)
    assert await resolver.load_all() is False
    release_fetch.set_result(None)

    assert await task == {"m:remote": 2.0}
    assert resolver.get_snapshot_for_model("m") == {}


@pytest.mark.asyncio
async def test_stale_get_for_model_does_not_overwrite_newer_local_snapshot():
    release_fetch = None

    async def list_for_model(_model_id: str):
        nonlocal release_fetch
        future = release_fetch
        if future is None:
            raise AssertionError("release future was not initialized")
        await future
        return [{"endpoint_id": "m:remote", "weight": 1}]

    store = AsyncMock()
    store.list_weight_overrides_for_model.side_effect = list_for_model
    resolver = WeightOverrideResolver(store)
    release_fetch = __import__("asyncio").get_running_loop().create_future()

    task = __import__("asyncio").create_task(resolver.get_for_model("m"))
    await __import__("asyncio").sleep(0)
    resolver.set_override("m", "m:remote", 4)
    release_fetch.set_result(None)

    assert await task == {"m:remote": 1.0}
    assert resolver.get_snapshot_for_model("m") == {"m:remote": 4.0}


@pytest.mark.asyncio
async def test_clear_model_fences_inflight_full_snapshot_refresh():
    started = asyncio.Event()
    release = asyncio.Event()

    async def list_all():
        started.set()
        await release.wait()
        return [{"model_id": "m", "endpoint_id": "m:stale", "weight": 9}]

    store = AsyncMock()
    store.list_all_weight_overrides.side_effect = list_all
    resolver = WeightOverrideResolver(store)
    resolver.set_override("m", "m:visible", 2)

    task = asyncio.create_task(resolver.load_all())
    await started.wait()
    resolver.clear_model("m")
    release.set()

    assert await task is False
    assert resolver.get_snapshot_for_model("m") == {}
