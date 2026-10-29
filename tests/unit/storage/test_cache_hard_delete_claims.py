"""Hard-delete claim cleanup through the production cache wrapper."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.base import HardDeleteClaim, HardDeleteClaimProvenance
from serving.storage.cache import CachedOperationalStore, InMemoryCache


def _cached_store():
    inner = MagicMock()
    inner.begin_hard_delete_user = AsyncMock()
    inner.release_hard_delete_user_claim = AsyncMock()
    cache = InMemoryCache()
    return CachedOperationalStore(inner, cache), inner, cache


@pytest.mark.asyncio
async def test_cache_invalidation_failure_releases_only_a_new_claim():
    cached, inner, cache = _cached_store()
    claim = HardDeleteClaim("new-token", HardDeleteClaimProvenance.NEW)
    inner.begin_hard_delete_user.return_value = claim
    cache.delete = AsyncMock(side_effect=RuntimeError("cache unavailable"))

    with pytest.raises(RuntimeError, match="cache unavailable"):
        await cached.begin_hard_delete_user("u1")

    inner.release_hard_delete_user_claim.assert_awaited_once_with("u1", "new-token")


@pytest.mark.asyncio
async def test_cache_invalidation_failure_keeps_a_recovered_claim_sticky():
    cached, inner, cache = _cached_store()
    claim = HardDeleteClaim("recovered-token", HardDeleteClaimProvenance.RECOVERED)
    inner.begin_hard_delete_user.return_value = claim
    cache.delete = AsyncMock(side_effect=RuntimeError("cache unavailable"))

    with pytest.raises(RuntimeError, match="cache unavailable"):
        await cached.begin_hard_delete_user("u1")

    inner.release_hard_delete_user_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_releases_claim_that_finishes_after_caller_cancels():
    cached, inner, _cache = _cached_store()
    claim = HardDeleteClaim("late-token", HardDeleteClaimProvenance.NEW)
    claim_started = asyncio.Event()
    finish_claim = asyncio.Event()
    released = asyncio.Event()

    async def begin_claim(_user_id: str):
        claim_started.set()
        await finish_claim.wait()
        return claim

    async def release_claim(_user_id: str, _claim_token: str):
        released.set()

    inner.begin_hard_delete_user.side_effect = begin_claim
    inner.release_hard_delete_user_claim.side_effect = release_claim

    request = asyncio.create_task(cached.begin_hard_delete_user("u1"))
    await asyncio.wait_for(claim_started.wait(), timeout=1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    finish_claim.set()
    await asyncio.wait_for(released.wait(), timeout=1)
    inner.release_hard_delete_user_claim.assert_awaited_once_with("u1", "late-token")


@pytest.mark.asyncio
async def test_cached_store_forwards_explicit_stale_claim_resume():
    cached, inner, _cache = _cached_store()
    inner.resume_user = AsyncMock()

    await cached.resume_user(
        "u1",
        admin_ip="127.0.0.1",
        admin_id="admin",
        reason="operator confirmed workers stopped",
        email="alice@example.com",
        recover_stale_claim=True,
    )

    inner.resume_user.assert_awaited_once_with(
        "u1",
        admin_ip="127.0.0.1",
        admin_id="admin",
        reason="operator confirmed workers stopped",
        email="alice@example.com",
        recover_stale_claim=True,
        # The merged resume_user signature also forwards dev's audit actor.
        actor=None,
    )
