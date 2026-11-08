"""Snapshot boundary for cached operational rows (A3).

Each cached read must return an independent deep copy of the stored value, so
a consumer that mutates one returned row cannot corrupt the cache and pollute
a later read of the same key -- including the nested mutable ``preferences``
dict that auth rows carry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from serving.storage import cache_backend
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.cache_backend import _snapshot


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


# ---------------------------------------------------------------------------
# The copy itself: every mutable container, never a deepcopy of a database row
# ---------------------------------------------------------------------------


def _row() -> dict:
    return {
        "user_id": "u1",
        "quota_daily_cost_usd": Decimal("5.000000"),
        "created_at": datetime(2026, 11, 7, tzinfo=UTC),
        "key_uuid": UUID(int=7),
        "allowed_models": ["m1", "m2"],
        "preferences": {"disabled_models": ["m3"], "locale": {"value": "en"}},
        "routes": [{"id": "r1", "weights": [1, 2]}],
        "verified": True,
        "note": None,
    }


def test_a_snapshot_shares_no_mutable_object_with_the_row():
    row = _row()
    copy = _snapshot(row)

    assert copy == row
    for path in (
        ("allowed_models",),
        ("preferences",),
        ("preferences", "disabled_models"),
        ("preferences", "locale"),
        ("routes",),
        ("routes", 0),
        ("routes", 0, "weights"),
    ):
        original, copied = row, copy
        for step in path:
            original, copied = original[step], copied[step]
        assert copied is not original, path


def test_a_snapshot_of_a_database_row_never_deep_copies(monkeypatch):
    """deepcopy cost every cache hit ~20 us a row, and 0.51 s for one stored layout."""

    def _refuse(_value, *_args):
        raise AssertionError("deepcopy on the cache-hit path")

    monkeypatch.setattr(cache_backend, "deepcopy", _refuse)
    row = _row()
    copy = _snapshot(row)
    # Immutable leaves are shared rather than rebuilt.
    assert copy["quota_daily_cost_usd"] is row["quota_daily_cost_usd"]
    assert copy["created_at"] is row["created_at"]


def test_a_value_of_another_kind_is_still_deep_copied():
    nested = {"tags": {"a", "b"}}
    copy = _snapshot(nested)
    copy["tags"].add("c")
    assert nested["tags"] == {"a", "b"}
