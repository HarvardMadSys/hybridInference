from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from serving.config.disabled_providers import DisabledProviderResolver


@pytest.mark.asyncio
async def test_load_all_populates_snapshot_from_store():
    store = AsyncMock()
    store.list_disabled_providers.return_value = [
        {"provider": "openrouter"},
        {"provider": "chutes"},
    ]
    resolver = DisabledProviderResolver(store)

    assert await resolver.load_all() is True

    assert resolver.is_disabled("openrouter")
    assert resolver.is_disabled("chutes")
    assert not resolver.is_disabled("zai")
    assert resolver.list_disabled() == frozenset({"openrouter", "chutes"})


@pytest.mark.asyncio
async def test_set_and_clear_update_snapshot_without_store_roundtrip():
    store = AsyncMock()
    store.list_disabled_providers.return_value = []
    resolver = DisabledProviderResolver(store)
    await resolver.load_all()

    resolver.set_disabled("zai")
    assert resolver.is_disabled("zai")

    resolver.clear_disabled("zai")
    assert not resolver.is_disabled("zai")


@pytest.mark.asyncio
async def test_load_all_replaces_previous_snapshot():
    store = AsyncMock()
    store.list_disabled_providers.side_effect = [
        [{"provider": "zai"}],
        [{"provider": "chutes"}],
    ]
    resolver = DisabledProviderResolver(store)

    assert await resolver.load_all() is True
    assert resolver.is_disabled("zai")

    assert await resolver.load_all() is True
    assert not resolver.is_disabled("zai")
    assert resolver.is_disabled("chutes")


@pytest.mark.asyncio
async def test_load_all_reports_unchanged_snapshot_independent_of_row_order():
    store = AsyncMock()
    store.list_disabled_providers.side_effect = [
        [{"provider": "zai"}, {"provider": "chutes"}],
        [{"provider": "chutes"}, {"provider": "zai"}],
        [],
    ]
    resolver = DisabledProviderResolver(store)

    assert await resolver.load_all() is True
    assert await resolver.load_all() is False
    assert await resolver.load_all() is True
