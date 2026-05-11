from __future__ import annotations

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
