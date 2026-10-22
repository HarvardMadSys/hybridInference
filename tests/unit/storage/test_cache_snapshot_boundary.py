"""Snapshot boundary for cached operational rows (A3).

Each cached read must return an independent deep copy of the stored value, so
a consumer that mutates one returned row cannot corrupt the cache and pollute
a later read of the same key -- including the nested mutable ``preferences``
dict that auth rows carry.
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
    store.get_user_by_id = AsyncMock()
    store.get_auth_context_by_key_hash = AsyncMock()
    store.get_auth_context_lightweight = AsyncMock()
    store.get_key_owner_for_audit = AsyncMock()
    return store


@pytest.fixture
def cached(inner, cache):
    return CachedOperationalStore(inner, cache)


async def test_mutating_a_returned_user_row_does_not_corrupt_later_reads(cache, inner, cached):
    inner.get_user_by_id.return_value = {
        "id": "u1",
        "name": "alice",
        "preferences": {"theme": "dark"},
    }

    first = await cached.get_user_by_id("u1")
    # Consumer mutates both the top level and the nested preferences dict.
    first["name"] = "mutated"
    first["preferences"]["theme"] = "light"

    second = await cached.get_user_by_id("u1")
    # The cache still holds the original values.
    assert second["name"] == "alice"
    assert second["preferences"]["theme"] == "dark"


async def test_health_check_returns_immutable_bool(cache, inner, cached):
    inner.health_check = AsyncMock(return_value=True)
    assert await cached.health_check() is True
    assert await cached.health_check() is True


async def test_auth_context_nested_preferences_not_aliased(cache, inner, cached):
    row = {"user_id": "u1", "role": "admin", "preferences": {"disabled_models": ["m1"]}}
    inner.get_auth_context_by_key_hash.return_value = row

    first = await cached.get_auth_context_by_key_hash("h1")
    first["preferences"]["disabled_models"].append("m2")
    first["preferences"]["disabled_models"] = ["hijacked"]

    second = await cached.get_auth_context_by_key_hash("h1")
    assert second["preferences"]["disabled_models"] == ["m1"]


async def test_lightweight_row_not_aliased(cache, inner, cached):
    inner.get_auth_context_lightweight.return_value = {"user_id": "u1", "role": "admin"}
    first = await cached.get_auth_context_lightweight("hk")
    first["role"] = "user"
    second = await cached.get_auth_context_lightweight("hk")
    assert second["role"] == "admin"


async def test_audit_row_not_aliased(cache, inner, cached):
    inner.get_key_owner_for_audit.return_value = {
        "user_id": "u1",
        "role": "admin",
        "key_status": "active",
        "key_expired": False,
        "user_status": "active",
        "expires_in_sec": 300,
    }
    first = await cached.get_key_owner_for_audit("kh")
    first["role"] = "hacker"
    second = await cached.get_key_owner_for_audit("kh")
    assert second["role"] == "admin"
