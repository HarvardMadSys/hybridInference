"""Tests for role-quota methods wired through CachedOperationalStore.

The cache surface uses CacheBackend (self._cache) with delete_pattern()
for invalidation — there is no _key_cache dict. After apply_role_quota,
all auth:* and auth_light:* cache entries are cleared, mirroring
the pattern used by update_key / revoke_key / regenerate_key.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.storage.cache import CachedOperationalStore, InMemoryCache


@pytest.mark.asyncio
async def test_apply_role_quota_invalidates_key_cache_and_returns_count():
    inner = AsyncMock()
    inner.apply_role_quota.return_value = 7

    cache = InMemoryCache()
    # Actual constructor: __init__(self, store, cache)
    store = CachedOperationalStore(store=inner, cache=cache)

    # Seed the cache with stale auth entries (simulate cached key rows that
    # have stale quota_daily_cost_usd after a bulk apply).
    await cache.set("auth:hash-1", {"quota_daily_cost_usd": Decimal("100.00")}, ttl=30)
    await cache.set("auth_light:hash-2", {"quota_daily_cost_usd": Decimal("100.00")}, ttl=30)

    n = await store.apply_role_quota("pro", Decimal("250.00"))

    assert n == 7
    inner.apply_role_quota.assert_awaited_once_with("pro", Decimal("250.00"))
    # Coarse invalidation: all auth cache entries cleared after bulk write.
    assert await cache.get("auth:hash-1") is None
    assert await cache.get("auth_light:hash-2") is None


@pytest.mark.asyncio
async def test_count_delegates_to_inner():
    inner = AsyncMock()
    inner.count_active_keys_for_role.return_value = (3, 2)

    cache = InMemoryCache()
    store = CachedOperationalStore(store=inner, cache=cache)

    result = await store.count_active_keys_for_role("pro")
    assert result == (3, 2)
    inner.count_active_keys_for_role.assert_awaited_once_with("pro")
