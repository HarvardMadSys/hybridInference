"""A1 reproduction: auth-cache invalidation race with in-flight fill.

Deterministic ordering with real asyncio primitives:

1. Request A misses the cache, starts the DB read, and pauses after the read
   returns (before cache publication).
2. Request B revokes the key; CachedOperationalStore invalidates auth caches.
3. Request A resumes and publishes its (older) row.
4. A subsequent authentication attempt reads the cache.

Current code loses the race: A's set lands after B's delete_pattern, so the
next lookup gets the revoked row for the remaining TTL. The fix makes
publication generation-fenced, so A's stale snapshot is dropped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.cache import CachedOperationalStore, InMemoryCache

VALID = {"user_id": "u1", "role": "user", "status": "active"}
REVOKED_STATE = {"user_id": "u1", "role": "user", "status": "revoked"}


@pytest.fixture
def inner():
    store = MagicMock()
    store.get_auth_context_by_key_hash = AsyncMock(return_value=dict(VALID))
    store.get_auth_context_lightweight = AsyncMock(return_value=dict(VALID))
    store.revoke_key = AsyncMock()
    return store


@pytest.fixture
def cache():
    return InMemoryCache()


@pytest.fixture
def cached(inner, cache):
    return CachedOperationalStore(inner, cache)


async def test_inflight_fill_does_not_revive_revoked_auth_context(inner, cache, cached):
    key_hash = "race-key-hash"
    ck = cached._auth_key(key_hash)

    # Step 1+2: the store read returns, but A pauses before publication.
    fill_event = asyncio.Event()
    release_event = asyncio.Event()
    original_read = inner.get_auth_context_by_key_hash.return_value

    async def paused_read(key_hash_inner):
        # Read completed: capture the snapshot the DB returned.
        fill_event.set()
        await release_event.wait()
        return original_read

    inner.get_auth_context_by_key_hash = AsyncMock(side_effect=paused_read)

    fill_task = asyncio.create_task(cached.get_auth_context_by_key_hash(key_hash))
    await asyncio.wait_for(fill_event.wait(), timeout=5)

    # Step 3: revoke completes invalidation while A is still paused.
    await cached.revoke_key("u1")
    assert await cache.get(ck) is None

    # Step 4: A resumes and tries to publish the older row.
    release_event.set()
    await asyncio.wait_for(fill_task, timeout=5)

    # Current code: A's set landed after the revoke's delete_pattern, so the
    # next lookup gets the revoked row for the remaining TTL. The fix drops it.
    assert await cache.get(ck) is None

    # Step 5: a subsequent authentication attempt sees the revoked state.
    inner.get_auth_context_by_key_hash = AsyncMock(return_value=dict(REVOKED_STATE))
    result = await cached.get_auth_context_by_key_hash(key_hash)
    assert result == REVOKED_STATE


async def test_inflight_lightweight_fill_does_not_revive_revoked_state(inner, cache, cached):
    key_hash = "lightweight-race"
    ck = cached._auth_light_key(key_hash)

    fill_event = asyncio.Event()
    release_event = asyncio.Event()
    original_read = inner.get_auth_context_lightweight.return_value

    async def paused_read(key_hash_inner):
        fill_event.set()
        await release_event.wait()
        return original_read

    inner.get_auth_context_lightweight = AsyncMock(side_effect=paused_read)

    fill_task = asyncio.create_task(cached.get_auth_context_lightweight(key_hash))
    await asyncio.wait_for(fill_event.wait(), timeout=5)

    await cached.revoke_key("u1")
    assert await cache.get(ck) is None

    release_event.set()
    await asyncio.wait_for(fill_task, timeout=5)

    assert await cache.get(ck) is None

    inner.get_auth_context_lightweight = AsyncMock(return_value=dict(REVOKED_STATE))
    result = await cached.get_auth_context_lightweight(key_hash)
    assert result == REVOKED_STATE
