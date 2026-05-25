from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from serving.config.model_concurrency import ModelConcurrencyResolver


class _Store:
    def __init__(self):
        self.get_model_concurrency_exemption = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_not_exempt_when_no_row_exists():
    store = _Store()
    resolver = ModelConcurrencyResolver(store, ttl=30.0)

    assert await resolver.is_exempt("glm-4.7") is False


@pytest.mark.asyncio
async def test_exempt_when_row_present():
    store = _Store()
    store.get_model_concurrency_exemption.return_value = {"model_id": "glm-4.7"}
    resolver = ModelConcurrencyResolver(store, ttl=30.0)

    assert await resolver.is_exempt("glm-4.7") is True


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_store_read():
    store = _Store()
    store.get_model_concurrency_exemption.return_value = {"model_id": "glm-4.7"}
    resolver = ModelConcurrencyResolver(store, ttl=30.0)

    await resolver.is_exempt("glm-4.7")
    await resolver.is_exempt("glm-4.7")

    store.get_model_concurrency_exemption.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalidate_model_forces_fresh_read():
    store = _Store()
    store.get_model_concurrency_exemption.side_effect = [
        {"model_id": "glm-4.7"},
        None,
    ]
    resolver = ModelConcurrencyResolver(store, ttl=30.0)

    first = await resolver.is_exempt("glm-4.7")
    resolver.invalidate_model("glm-4.7")
    second = await resolver.is_exempt("glm-4.7")

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_invalidate_cache_clears_all_entries():
    store = _Store()
    store.get_model_concurrency_exemption.return_value = {"model_id": "glm-4.7"}
    resolver = ModelConcurrencyResolver(store, ttl=30.0)

    await resolver.is_exempt("glm-4.7")
    resolver.invalidate_cache()
    await resolver.is_exempt("glm-4.7")

    assert store.get_model_concurrency_exemption.await_count == 2
