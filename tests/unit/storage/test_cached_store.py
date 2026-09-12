"""Unit tests for CachedOperationalStore and InMemoryCache.

Covers TTL behavior, cache hits/misses, and write-through invalidation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.storage.cache import (
    _AUTH_CONTEXT_TTL,
    CachedOperationalStore,
    InMemoryCache,
)


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache()


@pytest.fixture
def inner_store() -> MagicMock:
    """Mock underlying OperationalStore."""
    store = MagicMock()
    store.health_check = AsyncMock(return_value=True)
    store.get_user_by_id = AsyncMock(return_value={"id": "u1", "email": "a@b.com"})
    store.get_auth_context_by_key_hash = AsyncMock(return_value={"id": 1, "user_id": "u1"})
    store.get_auth_context_lightweight = AsyncMock(
        return_value={"user_id": "u1", "email": "a@b.com", "role": "admin"}
    )
    store.get_key_owner_for_audit = AsyncMock(
        return_value={
            "user_id": "u1",
            "role": "admin",
            "key_status": "revoked",
            "key_expired": False,
            "user_status": "active",
        }
    )
    store.query_users_over_daily_threshold = AsyncMock(return_value=[("u1", "free", 1.5)])
    store.update_user_fields = AsyncMock()
    store.update_user_last_login = AsyncMock()
    store.delete_user = AsyncMock()
    store.approve_user = AsyncMock()
    store.reject_user = AsyncMock()
    store.update_key = AsyncMock()
    store.revoke_key = AsyncMock()
    store.regenerate_key = AsyncMock(return_value="old-pfx")
    store.mark_user_email_verified = AsyncMock()
    return store


@pytest.fixture
def cached(inner_store, cache) -> CachedOperationalStore:
    return CachedOperationalStore(inner_store, cache)


# ------------------------------------------------------------------
# InMemoryCache
# ------------------------------------------------------------------


class TestInMemoryCache:
    """Tests for the InMemoryCache backend."""

    async def test_get_miss(self, cache):
        assert await cache.get("nonexistent") is None

    async def test_set_and_get(self, cache):
        await cache.set("key", "value", ttl=60)
        assert await cache.get("key") == "value"

    async def test_ttl_expiry(self, cache):
        with patch("serving.storage.cache.time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            await cache.set("key", "value", ttl=10)

            mock_time.return_value = 1005.0
            assert await cache.get("key") == "value"  # within TTL

            mock_time.return_value = 1011.0
            assert await cache.get("key") is None  # expired

    async def test_delete(self, cache):
        await cache.set("key", "value", ttl=60)
        await cache.delete("key")
        assert await cache.get("key") is None

    async def test_delete_nonexistent(self, cache):
        await cache.delete("nonexistent")  # no error

    async def test_delete_pattern(self, cache):
        await cache.set("auth:hash1", "v1", ttl=60)
        await cache.set("auth:hash2", "v2", ttl=60)
        await cache.set("user:u1", "v3", ttl=60)

        await cache.delete_pattern("auth:*")

        assert await cache.get("auth:hash1") is None
        assert await cache.get("auth:hash2") is None
        assert await cache.get("user:u1") == "v3"  # untouched


# ------------------------------------------------------------------
# CachedOperationalStore — cache hits
# ------------------------------------------------------------------


class TestCacheHits:
    """Verify that repeated reads hit the cache, not the inner store."""

    async def test_get_user_by_id_caches(self, cached, inner_store):
        await cached.get_user_by_id("u1")
        await cached.get_user_by_id("u1")

        inner_store.get_user_by_id.assert_awaited_once_with("u1")

    async def test_get_auth_context_caches(self, cached, inner_store):
        await cached.get_auth_context_by_key_hash("h1")
        await cached.get_auth_context_by_key_hash("h1")

        inner_store.get_auth_context_by_key_hash.assert_awaited_once_with("h1")

    async def test_get_auth_context_lightweight_caches(self, cached, inner_store):
        await cached.get_auth_context_lightweight("h1")
        await cached.get_auth_context_lightweight("h1")

        inner_store.get_auth_context_lightweight.assert_awaited_once_with("h1")

    async def test_get_key_owner_for_audit_caches(self, cached, inner_store):
        await cached.get_key_owner_for_audit("h1")
        await cached.get_key_owner_for_audit("h1")

        inner_store.get_key_owner_for_audit.assert_awaited_once_with("h1")

    async def test_audit_entry_is_not_cached_past_the_key_deadline(
        self, cached, inner_store, cache
    ):
        """A key expiring inside the TTL shortens the entry to that window.

        Expiry is the one state here that changes without a write, so nothing
        invalidates on it. Cached for the full TTL, a row resolved seconds
        before the deadline would go on reporting a live credential after it
        had lapsed — and, being reported live, would put its owner back into
        ``api_logs.user_id``.
        """
        inner_store.get_key_owner_for_audit.return_value = {
            "user_id": "u1",
            "role": "admin",
            "key_status": "active",
            "key_expired": False,
            "expires_in_sec": 4.0,
            "user_status": "active",
        }
        recorded: list[int] = []
        original_set = cache.set

        async def _record_ttl(key: str, value: Any, ttl: int) -> None:
            recorded.append(ttl)
            await original_set(key, value, ttl)

        cache.set = _record_ttl

        await cached.get_key_owner_for_audit("h1")

        assert recorded == [4]

    async def test_audit_entry_trusts_the_row_over_a_local_clock(self, cached, inner_store, cache):
        """A live row whose deadline has locally passed gets the shortest life.

        The two can disagree across the query: the database calls the key live
        moments before ``expires_at``, and by the time this process subtracts,
        the remainder is already negative. The row's flag is the answer being
        cached, so a negative remainder must not be read as "expired, cache it
        fully" — that hands the full TTL to the one row the cap exists for.
        """
        inner_store.get_key_owner_for_audit.return_value = {
            "user_id": "u1",
            "role": "admin",
            "key_status": "active",
            "key_expired": False,
            "expires_in_sec": -2.0,
            "user_status": "active",
        }
        recorded: list[int] = []
        original_set = cache.set

        async def _record_ttl(key: str, value: Any, ttl: int) -> None:
            recorded.append(ttl)
            await original_set(key, value, ttl)

        cache.set = _record_ttl

        await cached.get_key_owner_for_audit("h1")

        assert recorded == [1]

    async def test_audit_entry_keeps_the_normal_ttl_once_the_row_says_expired(
        self, cached, inner_store, cache
    ):
        """An expired row is a stable answer, so it keeps the normal TTL."""
        inner_store.get_key_owner_for_audit.return_value = {
            "user_id": "u1",
            "role": "admin",
            "key_status": "active",
            "key_expired": True,
            "expires_in_sec": -86400.0,
            "user_status": "active",
        }
        recorded: list[int] = []
        original_set = cache.set

        async def _record_ttl(key: str, value: Any, ttl: int) -> None:
            recorded.append(ttl)
            await original_set(key, value, ttl)

        cache.set = _record_ttl

        await cached.get_key_owner_for_audit("h1")

        assert recorded == [_AUTH_CONTEXT_TTL]

    async def test_audit_entry_keeps_the_normal_ttl_for_a_distant_deadline(
        self, cached, inner_store, cache
    ):
        """The cap only ever shortens: a far-off expiry changes nothing."""
        inner_store.get_key_owner_for_audit.return_value = {
            "user_id": "u1",
            "role": "admin",
            "key_status": "active",
            "key_expired": False,
            "expires_in_sec": 2592000.0,
            "user_status": "active",
        }
        recorded: list[int] = []
        original_set = cache.set

        async def _record_ttl(key: str, value: Any, ttl: int) -> None:
            recorded.append(ttl)
            await original_set(key, value, ttl)

        cache.set = _record_ttl

        await cached.get_key_owner_for_audit("h1")

        assert recorded == [_AUTH_CONTEXT_TTL]

    async def test_get_key_owner_for_audit_is_cleared_by_a_key_write(
        self, cached, inner_store, cache
    ):
        """A revoked key's audit entry must not outlive the next key write.

        It caches rows the auth lookups never see — including dead credentials
        — so it is the entry most likely to be stale, and its key lives under
        the ``auth_light:`` namespace precisely so every existing invalidation
        clears it.
        """
        await cached.get_key_owner_for_audit("h1")
        await cached.revoke_key("h1")
        await cached.get_key_owner_for_audit("h1")

        assert inner_store.get_key_owner_for_audit.await_count == 2

    async def test_approval_decisions_clear_the_audit_entry(self, cached, inner_store):
        """Approving or rejecting a user re-reads the audit identity.

        The auth lookups filter on an active user, so a pending account was
        never in this cache before ``get_key_owner_for_audit`` -- which caches
        a row whatever state the account is in, and would otherwise keep
        labelling rejection rows with the pre-decision state for a TTL.
        """
        for decide in (
            lambda: cached.approve_user("u1", admin_id="admin-1"),
            lambda: cached.reject_user("u1", admin_id="admin-1", reason="spam"),
        ):
            # Populate first, then count only the reads after the decision --
            # the previous iteration leaves the entry cached, so counting from
            # a populate that may itself be a hit would prove nothing.
            await cached.get_key_owner_for_audit("h1")
            inner_store.get_key_owner_for_audit.reset_mock()

            await decide()
            await cached.get_key_owner_for_audit("h1")

            assert inner_store.get_key_owner_for_audit.await_count == 1

    async def test_health_check_caches(self, cached, inner_store):
        await cached.health_check()
        await cached.health_check()

        inner_store.health_check.assert_awaited_once()

    async def test_query_users_over_daily_threshold_passes_through(self, cached, inner_store):
        thresholds = {"free": 1.0}

        result = await cached.query_users_over_daily_threshold(thresholds)

        assert result == [("u1", "free", 1.5)]
        inner_store.query_users_over_daily_threshold.assert_awaited_once_with(thresholds)


# ------------------------------------------------------------------
# CachedOperationalStore — cache misses (None results not cached)
# ------------------------------------------------------------------


class TestCacheMisses:
    """Verify that None results are not cached."""

    async def test_user_not_found_not_cached(self, cached, inner_store):
        inner_store.get_user_by_id.return_value = None

        await cached.get_user_by_id("missing")
        await cached.get_user_by_id("missing")

        assert inner_store.get_user_by_id.await_count == 2

    async def test_auth_context_not_found_not_cached(self, cached, inner_store):
        inner_store.get_auth_context_by_key_hash.return_value = None

        await cached.get_auth_context_by_key_hash("bad-hash")
        await cached.get_auth_context_by_key_hash("bad-hash")

        assert inner_store.get_auth_context_by_key_hash.await_count == 2


# ------------------------------------------------------------------
# CachedOperationalStore — write-through invalidation
# ------------------------------------------------------------------


class TestWriteInvalidation:
    """Verify that writes invalidate related cache entries."""

    async def test_update_user_fields_invalidates_user(self, cached, inner_store):
        await cached.get_user_by_id("u1")  # populate cache
        await cached.update_user_fields("u1", role="admin")

        # Next read should hit inner store again
        await cached.get_user_by_id("u1")
        assert inner_store.get_user_by_id.await_count == 2

    async def test_update_user_last_login_invalidates(self, cached, inner_store):
        await cached.get_user_by_id("u1")
        await cached.update_user_last_login("u1")
        await cached.get_user_by_id("u1")
        assert inner_store.get_user_by_id.await_count == 2

    async def test_delete_user_invalidates_user_and_auth(self, cached, inner_store):
        await cached.get_user_by_id("u1")
        await cached.get_auth_context_by_key_hash("h1")

        await cached.delete_user("u1", admin_ip="1.2.3.4", admin_id="a1")

        # Both caches should be cleared
        await cached.get_user_by_id("u1")
        await cached.get_auth_context_by_key_hash("h1")
        assert inner_store.get_user_by_id.await_count == 2
        assert inner_store.get_auth_context_by_key_hash.await_count == 2

    async def test_approve_user_invalidates(self, cached, inner_store):
        await cached.get_user_by_id("u1")
        await cached.approve_user("u1", admin_id="a1")
        await cached.get_user_by_id("u1")
        assert inner_store.get_user_by_id.await_count == 2

    async def test_reject_user_invalidates(self, cached, inner_store):
        await cached.get_user_by_id("u1")
        await cached.reject_user("u1", admin_id="a1", reason="spam")
        await cached.get_user_by_id("u1")
        assert inner_store.get_user_by_id.await_count == 2

    async def test_mark_email_verified_invalidates_user_and_auth(self, cached, inner_store):
        # Populate user and auth caches with the pre-verification (stale) rows.
        await cached.get_user_by_id("u1")
        await cached.get_auth_context_by_key_hash("h1")
        await cached.get_auth_context_lightweight("h1")

        await cached.mark_user_email_verified("u1")

        # All three caches must be cleared so the next read reflects verification.
        await cached.get_user_by_id("u1")
        await cached.get_auth_context_by_key_hash("h1")
        await cached.get_auth_context_lightweight("h1")
        assert inner_store.get_user_by_id.await_count == 2
        assert inner_store.get_auth_context_by_key_hash.await_count == 2
        assert inner_store.get_auth_context_lightweight.await_count == 2

    async def test_update_key_invalidates_auth(self, cached, inner_store):
        await cached.get_auth_context_by_key_hash("h1")
        await cached.update_key("u1", quota_daily_cost_usd=200.0)
        await cached.get_auth_context_by_key_hash("h1")
        assert inner_store.get_auth_context_by_key_hash.await_count == 2

    async def test_revoke_key_invalidates_auth(self, cached, inner_store):
        await cached.get_auth_context_by_key_hash("h1")
        await cached.revoke_key("u1")
        await cached.get_auth_context_by_key_hash("h1")
        assert inner_store.get_auth_context_by_key_hash.await_count == 2

    async def test_regenerate_key_invalidates_auth(self, cached, inner_store):
        await cached.get_auth_context_by_key_hash("h1")
        old = await cached.regenerate_key("u1", new_key_hash="h2", new_key_prefix="p2")
        assert old == "old-pfx"
        await cached.get_auth_context_by_key_hash("h1")
        assert inner_store.get_auth_context_by_key_hash.await_count == 2


# ------------------------------------------------------------------
# Pass-through methods
# ------------------------------------------------------------------


class TestPassthrough:
    """Verify that non-cached methods delegate directly."""

    async def test_get_user_by_email_not_cached(self, cached, inner_store):
        inner_store.get_user_by_email = AsyncMock(return_value={"id": "u1"})
        await cached.get_user_by_email("a@b.com")
        await cached.get_user_by_email("a@b.com")
        assert inner_store.get_user_by_email.await_count == 2

    async def test_lifecycle_delegates(self, cached, inner_store):
        inner_store.initialize = AsyncMock()
        inner_store.cleanup = AsyncMock()

        await cached.initialize()
        await cached.cleanup()

        inner_store.initialize.assert_awaited_once()
        inner_store.cleanup.assert_awaited_once()
