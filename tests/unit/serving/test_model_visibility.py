from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from serving.config.model_visibility import ModelVisibilityResolver


class _Store:
    def __init__(self):
        self.get_model_visibility_override = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_returns_default_role_when_no_override_exists():
    store = _Store()
    resolver = ModelVisibilityResolver(store, ttl=30.0)

    result = await resolver.get_effective_required_role("glm-4.7", "internal")

    assert result == "internal"


@pytest.mark.asyncio
async def test_returns_override_when_present():
    store = _Store()
    store.get_model_visibility_override.return_value = {
        "model_id": "glm-4.7",
        "required_role": "free",
    }
    resolver = ModelVisibilityResolver(store, ttl=30.0)

    result = await resolver.get_effective_required_role("glm-4.7", "internal")

    assert result == "free"


@pytest.mark.asyncio
async def test_invalidate_model_forces_fresh_read():
    store = _Store()
    store.get_model_visibility_override.side_effect = [
        {"model_id": "glm-4.7", "required_role": "free"},
        {"model_id": "glm-4.7", "required_role": "admin"},
    ]
    resolver = ModelVisibilityResolver(store, ttl=30.0)

    first = await resolver.get_effective_required_role("glm-4.7", "internal")
    resolver.invalidate_model("glm-4.7")
    second = await resolver.get_effective_required_role("glm-4.7", "internal")

    assert first == "free"
    assert second == "admin"


@pytest.mark.asyncio
async def test_missing_override_after_invalidation_returns_default_again():
    store = _Store()
    store.get_model_visibility_override.side_effect = [
        {"model_id": "glm-4.7", "required_role": "free"},
        None,
    ]
    resolver = ModelVisibilityResolver(store, ttl=30.0)

    first = await resolver.get_effective_required_role("glm-4.7", "internal")
    resolver.invalidate_model("glm-4.7")
    second = await resolver.get_effective_required_role("glm-4.7", "internal")

    assert first == "free"
    assert second == "internal"


@pytest.mark.asyncio
async def test_invalid_persisted_override_falls_back_to_admin():
    store = _Store()
    store.get_model_visibility_override.return_value = {
        "model_id": "glm-4.7",
        "required_role": "definitely-not-a-role",
    }
    resolver = ModelVisibilityResolver(store, ttl=30.0)

    result = await resolver.get_effective_required_role("glm-4.7", "free")

    assert result == "admin"


@pytest.mark.asyncio
async def test_invalidation_fences_inflight_stale_visibility_read():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def get_override(_model_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return {"model_id": "glm-4.7", "required_role": "free"}
        return None

    store = _Store()
    store.get_model_visibility_override.side_effect = get_override
    resolver = ModelVisibilityResolver(store, ttl=30.0)

    task = asyncio.create_task(resolver.get_effective_required_role("glm-4.7", "internal"))
    await started.wait()
    resolver.invalidate_model("glm-4.7")
    release.set()

    assert await task == "internal"
    assert await resolver.get_effective_required_role("glm-4.7", "internal") == "internal"
    assert store.get_model_visibility_override.await_count == 2
