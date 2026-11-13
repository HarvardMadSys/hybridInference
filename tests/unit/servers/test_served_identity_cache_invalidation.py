"""Revocation and metadata updates must invalidate the cached entitlement.

The served-identity entitlement rides in ``api_keys.metadata``, which the
auth lookup caches for 30 s under the key's account tag. An operator who
grants, changes or revokes the entitlement must take effect at once, not
after the cache TTL: the entitlement decides whether a response header
discloses the serving endpoint.
"""

from __future__ import annotations

from typing import Any

import pytest

from serving.storage.cache import CachedOperationalStore


class _Row(dict):
    pass


class _Inner:
    def __init__(self) -> None:
        self.updated: list[dict[str, Any]] = []
        self.revoked: list[str] = []

    async def get_auth_context_by_key_hash(self, key_hash: str) -> Any:
        return None

    async def get_auth_context_lightweight(self, key_hash: str) -> Any:
        return None

    async def get_key_owner_for_audit(self, key_hash: str) -> Any:
        return None

    async def update_key(self, user_id: str, **fields: Any) -> None:
        self.updated.append({"user_id": user_id, **fields})

    async def revoke_key(self, user_id: str, *, hard_delete: bool = False) -> None:
        self.revoked.append(user_id)

    async def invalidate_auth_caches(self) -> None:
        return None


class _Cache:
    def __init__(self) -> None:
        self.deleted_patterns: list[str] = []
        self.deleted_tags: list[str] = []

    async def delete_pattern(self, pattern: str) -> None:
        self.deleted_patterns.append(pattern)

    async def delete_tag(self, tag: str) -> None:
        self.deleted_tags.append(tag)

    async def get(self, key: str) -> Any:
        return None

    async def set_if_generation(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None

    async def current_generation(self) -> int:
        return 0


@pytest.fixture
def store() -> tuple[CachedOperationalStore, _Inner, _Cache]:
    inner = _Inner()
    cache = _Cache()
    return CachedOperationalStore(inner, cache), inner, cache  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_granting_the_entitlement_invalidates_the_cached_auth_context(store):
    """An admin setting the entitlement must not wait for the cache TTL."""
    cached, inner, cache = store
    await cached.update_key(
        "user-1",
        metadata={"diagnostic_entitlements": ["served_identity"]},
    )
    assert inner.updated == [
        {"user_id": "user-1", "metadata": {"diagnostic_entitlements": ["served_identity"]}}
    ]
    # The cached auth entry for that account is evicted, so the next request
    # re-reads the row and sees the new metadata.
    assert cache.deleted_tags == [cached._user_auth_tag("user-1")]


@pytest.mark.asyncio
async def test_revoking_the_entitlement_invalidates_the_cached_auth_context(store):
    """Withdrawing the entitlement takes effect at once."""
    cached, _inner, cache = store
    await cached.update_key("user-1", metadata={"diagnostic_entitlements": []})
    assert cache.deleted_tags == [cached._user_auth_tag("user-1")]


@pytest.mark.asyncio
async def test_revoking_the_key_invalidates_the_cached_auth_context(store):
    """A revoked key must not keep its entitlement through the cache."""
    cached, inner, cache = store
    await cached.revoke_key("user-1")
    assert inner.revoked == ["user-1"]
    assert cache.deleted_tags == [cached._user_auth_tag("user-1")]


def test_the_auth_lookup_reads_the_key_metadata_it_caches():
    """The cached row must carry the metadata the entitlement is derived from."""
    import inspect

    from serving.storage import postgres_operational

    source = inspect.getsource(postgres_operational)
    assert "k.metadata AS key_metadata" in source
    # And the column is part of the projection the auth lookups use.
    assert "_AUTH_KEY_COLUMNS" in source
