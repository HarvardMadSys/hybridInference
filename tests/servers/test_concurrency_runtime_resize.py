"""Tests for runtime-resizable UserConcurrencyLimiter."""

from __future__ import annotations

import pytest

from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    static_limits_provider,
)


@pytest.mark.asyncio
async def test_provider_called_each_acquire():
    """The limiter consults the provider on every acquire."""
    calls = {"n": 0}

    async def provider() -> dict[str, int]:
        calls["n"] += 1
        return {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    limiter = UserConcurrencyLimiter(provider)
    await limiter.try_acquire("u1", "free", False)
    await limiter.try_acquire("u1", "free", False)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_increase_resizes_existing_slot():
    """Bumping the cap raises an existing slot's capacity on next acquire."""
    limits = {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    async def provider() -> dict[str, int]:
        return dict(limits)  # snapshot per call

    limiter = UserConcurrencyLimiter(provider)
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert granted and cap == 1

    # Second acquire under cap=1 must fail.
    granted, _, _ = await limiter.try_acquire("u1", "free", False)
    assert not granted

    # Bump the cap; next acquire resizes the slot, then succeeds.
    limits["free"] = 3
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert granted
    assert cap == 3


@pytest.mark.asyncio
async def test_decrease_does_not_kill_in_flight_but_blocks_new():
    """Lowering the cap below current in_use leaves in-flight requests alone
    but rejects further acquires until the user drains."""
    limits = {"free": 3, "pro": 3, "internal": 10, "admin": 10}

    async def provider() -> dict[str, int]:
        return dict(limits)

    limiter = UserConcurrencyLimiter(provider)
    for _ in range(3):
        granted, _, _ = await limiter.try_acquire("u1", "free", False)
        assert granted

    # Drop cap to 1 while in_use=3.
    limits["free"] = 1

    # New acquires must fail while in_use > new cap.
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert not granted
    assert cap == 1

    # Release until in_use is below the new cap.
    limiter.release("u1")
    limiter.release("u1")
    limiter.release("u1")
    granted, cap, _ = await limiter.try_acquire("u1", "free", False)
    assert granted
    assert cap == 1


@pytest.mark.asyncio
async def test_provider_error_falls_back_to_registry_defaults():
    """If the provider raises, the limiter uses registry defaults."""

    async def boom() -> dict[str, int]:
        raise RuntimeError("db offline")

    limiter = UserConcurrencyLimiter(boom)
    # Free default is 3 (per RUNTIME_SETTINGS_REGISTRY).
    granted, cap, label = await limiter.try_acquire("u1", "free", False)
    assert granted
    assert cap == 3
    assert label == "free"


@pytest.mark.asyncio
async def test_static_helper_constructs_async_provider():
    """The static_limits_provider helper wraps a plain dict."""
    p = static_limits_provider({"free": 2, "pro": 3, "internal": 10, "admin": 10})
    snapshot = await p()
    assert snapshot == {"free": 2, "pro": 3, "internal": 10, "admin": 10}


@pytest.mark.asyncio
async def test_admin_role_uses_admin_cap():
    async def provider() -> dict[str, int]:
        return {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    limiter = UserConcurrencyLimiter(provider)
    granted, cap, label = await limiter.try_acquire("a1", "free", True)
    assert granted
    assert cap == 10
    assert label == "admin"


@pytest.mark.asyncio
async def test_unknown_role_falls_back_to_free_cap():
    async def provider() -> dict[str, int]:
        return {"free": 1, "pro": 3, "internal": 10, "admin": 10}

    limiter = UserConcurrencyLimiter(provider)
    granted, cap, label = await limiter.try_acquire("u1", "mystery", False)
    assert granted
    assert cap == 1
    assert label == "free"
