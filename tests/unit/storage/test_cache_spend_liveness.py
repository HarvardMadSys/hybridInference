"""Live daily-spend authorization must never be served from the auth cache.

The #1608 carve-out: verifying an API key may cache the *identity* (auth
context / user row) for a short TTL, but the per-user daily cost spent must be
read live from the wrapped store on every balance/auth-gate lookup. A stale
spend figure would under- or over-authorize a request based on cached cost
that the wallet has since moved.

This locks in the guarantee that CachedOperationalStore always delegates
get_user_cost_today to the inner store (never caches it) and returns the exact
value the store reported.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.cache import CachedOperationalStore, InMemoryCache


@pytest.fixture
def cache():
    return InMemoryCache()


@pytest.fixture
def inner():
    store = MagicMock()
    store.get_user_cost_today = AsyncMock(return_value=12.5)
    return store


@pytest.fixture
def cached(inner, cache):
    return CachedOperationalStore(inner, cache)


async def test_spend_lookup_is_delegated_every_time(cache, inner, cached):
    # Warm a user's identity in the auth cache to simulate a prior lookup.
    inner.get_auth_context_by_key_hash = AsyncMock(return_value={"user_id": "u1"})
    await cached.get_auth_context_by_key_hash("k1")

    # Spend must be read live, not from the identity cache.
    assert await cached.get_user_cost_today("u1") == 12.5
    assert inner.get_user_cost_today.await_count == 1
    assert await cached.get_user_cost_today("u1") == 12.5
    # Every call reaches the store: two lookups, two delegate calls.
    assert inner.get_user_cost_today.await_count == 2


async def test_spend_reflects_store_change_cached_identity_stays(cache, inner, cached):
    inner.get_auth_context_by_key_hash = AsyncMock(return_value={"user_id": "u1"})
    await cached.get_auth_context_by_key_hash("k1")

    inner.get_user_cost_today.return_value = 12.5
    assert await cached.get_user_cost_today("u1") == 12.5

    # Wallet moves while identity stays cached -- spend must update.
    inner.get_user_cost_today.return_value = 99.0
    assert await cached.get_user_cost_today("u1") == 99.0
