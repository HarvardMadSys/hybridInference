"""Tests for cache invalidation correctness under the auth hot path.

Verifies that revoking/regenerating a key immediately invalidates the
cache entry, so the next verify_api_key call sees the revocation even
within the 30s TTL window.  Same for user status changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.cache import CachedOperationalStore, InMemoryCache


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache()


@pytest.fixture
def inner() -> MagicMock:
    store = MagicMock()
    store.get_auth_context_by_key_hash = AsyncMock()
    store.get_auth_context_lightweight = AsyncMock()
    store.get_user_by_id = AsyncMock()
    store.revoke_key = AsyncMock()
    store.regenerate_key = AsyncMock(return_value="old-pfx")
    store.update_key = AsyncMock()
    store.delete_user = AsyncMock()
    store.update_user_fields = AsyncMock()
    store.approve_user = AsyncMock()
    store.reject_user = AsyncMock()
    return store


@pytest.fixture
def cached(inner, cache) -> CachedOperationalStore:
    return CachedOperationalStore(inner, cache)


class TestRevokeKeyInvalidation:
    """Revoking a key must immediately invalidate cached auth context."""

    async def test_revoke_clears_auth_cache(self, cached, inner):
        # Cache a valid auth context
        inner.get_auth_context_by_key_hash.return_value = {"id": 1, "user_id": "u1", "tier": "free"}
        result = await cached.get_auth_context_by_key_hash("active-key-hash")
        assert result is not None

        # Revoke the key
        await cached.revoke_key("u1")

        # After revocation, inner store says key is gone
        inner.get_auth_context_by_key_hash.return_value = None
        result = await cached.get_auth_context_by_key_hash("active-key-hash")

        # Must see None (revoked), NOT the stale cached value
        assert result is None

    async def test_revoke_clears_lightweight_cache(self, cached, inner):
        inner.get_auth_context_lightweight.return_value = {
            "user_id": "u1",
            "email": "a@b.com",
            "role": "admin",
        }
        await cached.get_auth_context_lightweight("key-hash")

        await cached.revoke_key("u1")

        inner.get_auth_context_lightweight.return_value = None
        result = await cached.get_auth_context_lightweight("key-hash")
        assert result is None


class TestRegenerateKeyInvalidation:
    """Regenerating a key must invalidate the OLD key's cached context."""

    async def test_regenerate_clears_old_key_cache(self, cached, inner):
        # Cache auth context for the old key
        inner.get_auth_context_by_key_hash.return_value = {"id": 1, "user_id": "u1", "tier": "free"}
        await cached.get_auth_context_by_key_hash("old-key-hash")

        # Regenerate (replace old key with new)
        await cached.regenerate_key("u1", new_key_hash="new-key-hash", new_key_prefix="new-pfx")

        # Old key should no longer be valid
        inner.get_auth_context_by_key_hash.return_value = None
        result = await cached.get_auth_context_by_key_hash("old-key-hash")
        assert result is None

    async def test_regenerate_new_key_works(self, cached, inner):
        # Cache old key
        inner.get_auth_context_by_key_hash.return_value = {"id": 1, "user_id": "u1"}
        await cached.get_auth_context_by_key_hash("old-hash")

        # Regenerate
        await cached.regenerate_key("u1", new_key_hash="new-hash", new_key_prefix="new-pfx")

        # New key should resolve from inner store
        inner.get_auth_context_by_key_hash.return_value = {"id": 2, "user_id": "u1"}
        result = await cached.get_auth_context_by_key_hash("new-hash")
        assert result["id"] == 2


class TestUpdateKeyInvalidation:
    """Updating key fields (e.g., quota change) must invalidate auth cache."""

    async def test_update_key_clears_auth_cache(self, cached, inner):
        inner.get_auth_context_by_key_hash.return_value = {"id": 1, "quota_daily_cost_usd": 100.0}
        await cached.get_auth_context_by_key_hash("key-hash")

        # Admin changes quota
        await cached.update_key("u1", quota_daily_cost_usd=0.0)

        # Next lookup must see updated value from inner store
        inner.get_auth_context_by_key_hash.return_value = {"id": 1, "quota_daily_cost_usd": 0.0}
        result = await cached.get_auth_context_by_key_hash("key-hash")
        assert result["quota_daily_cost_usd"] == 0.0


class TestDeleteUserInvalidation:
    """Deleting a user must invalidate both user cache and auth cache."""

    async def test_delete_clears_user_cache(self, cached, inner):
        inner.get_user_by_id.return_value = {"id": "u1", "status": "active"}
        await cached.get_user_by_id("u1")

        await cached.delete_user("u1", admin_ip="1.2.3.4", admin_id="a1")

        inner.get_user_by_id.return_value = {"id": "u1", "status": "deleted"}
        result = await cached.get_user_by_id("u1")
        assert result["status"] == "deleted"

    async def test_delete_clears_auth_cache(self, cached, inner):
        inner.get_auth_context_by_key_hash.return_value = {"id": 1, "user_id": "u1"}
        await cached.get_auth_context_by_key_hash("key-hash")

        await cached.delete_user("u1", admin_ip="1.2.3.4", admin_id="a1")

        inner.get_auth_context_by_key_hash.return_value = None
        result = await cached.get_auth_context_by_key_hash("key-hash")
        assert result is None


class TestUserStatusChangeInvalidation:
    """Approve/reject/suspend must invalidate cached user data."""

    async def test_approve_invalidates(self, cached, inner):
        inner.get_user_by_id.return_value = {"id": "u1", "status": "pending_approval"}
        await cached.get_user_by_id("u1")

        await cached.approve_user("u1", admin_id="a1")

        inner.get_user_by_id.return_value = {"id": "u1", "status": "active"}
        result = await cached.get_user_by_id("u1")
        assert result["status"] == "active"

    async def test_reject_invalidates(self, cached, inner):
        inner.get_user_by_id.return_value = {"id": "u1", "status": "pending_approval"}
        await cached.get_user_by_id("u1")

        await cached.reject_user("u1", admin_id="a1", reason="spam")

        inner.get_user_by_id.return_value = {"id": "u1", "status": "rejected"}
        result = await cached.get_user_by_id("u1")
        assert result["status"] == "rejected"

    async def test_suspend_via_update_fields_invalidates(self, cached, inner):
        inner.get_user_by_id.return_value = {"id": "u1", "status": "active"}
        await cached.get_user_by_id("u1")

        await cached.update_user_fields("u1", status="suspended")

        inner.get_user_by_id.return_value = {"id": "u1", "status": "suspended"}
        result = await cached.get_user_by_id("u1")
        assert result["status"] == "suspended"

    async def test_status_change_also_clears_auth_cache(self, cached, inner):
        """Changing status must bust the auth cache, not just the user cache.

        A key cached before the status change would otherwise continue to
        authenticate the suspended user until the TTL expires.
        """
        key_hash = "key-for-suspended-user"
        inner.get_auth_context_by_key_hash.return_value = {
            "user_id": "u1",
            "tier": "free",
            "status": "active",
        }
        # Warm the auth cache
        await cached.get_auth_context_by_key_hash(key_hash)
        assert inner.get_auth_context_by_key_hash.await_count == 1

        # Suspend the user — must purge the auth cache
        inner.get_auth_context_by_key_hash.return_value = None
        await cached.update_user_fields("u1", status="suspended")

        # Next auth lookup must hit the inner store, not the now-stale cache
        await cached.get_auth_context_by_key_hash(key_hash)
        assert inner.get_auth_context_by_key_hash.await_count == 2, (
            "auth cache was not invalidated after status change"
        )

    async def test_role_change_also_clears_auth_cache(self, cached, inner):
        """Promoting a user to admin must bust the auth cache immediately."""
        key_hash = "key-for-promoted-user"
        inner.get_auth_context_by_key_hash.return_value = {
            "user_id": "u2",
            "tier": "free",
            "role": "user",
        }
        await cached.get_auth_context_by_key_hash(key_hash)
        assert inner.get_auth_context_by_key_hash.await_count == 1

        inner.get_auth_context_by_key_hash.return_value = {
            "user_id": "u2",
            "tier": "free",
            "role": "admin",
        }
        await cached.update_user_fields("u2", role="admin")

        await cached.get_auth_context_by_key_hash(key_hash)
        assert inner.get_auth_context_by_key_hash.await_count == 2, (
            "auth cache was not invalidated after role change"
        )

    async def test_non_auth_field_change_does_not_clear_auth_cache(self, cached, inner):
        """Updating a field like preferences must NOT flush the auth cache."""
        key_hash = "key-for-pref-update"
        inner.get_auth_context_by_key_hash.return_value = {"user_id": "u3", "tier": "free"}
        await cached.get_auth_context_by_key_hash(key_hash)
        assert inner.get_auth_context_by_key_hash.await_count == 1

        await cached.update_user_fields("u3", preferences={"theme": "dark"})

        # Auth cache should still be warm — no extra inner call
        await cached.get_auth_context_by_key_hash(key_hash)
        assert inner.get_auth_context_by_key_hash.await_count == 1, (
            "auth cache was unexpectedly flushed for a non-auth field update"
        )
