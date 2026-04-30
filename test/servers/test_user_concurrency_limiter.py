"""Unit tests for UserConcurrencyLimiter and _UserSlot."""

import asyncio

import pytest

from serving.servers.concurrency import UserConcurrencyLimiter, _UserSlot

LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}


# ----------------------------- _UserSlot --------------------------------


def test_user_slot_acquire_until_capacity():
    slot = _UserSlot(capacity=2, role="pro")
    assert slot.try_acquire() is True
    assert slot.try_acquire() is True
    assert slot.try_acquire() is False  # at capacity
    assert slot.in_use == 2


def test_user_slot_release_frees_capacity():
    slot = _UserSlot(capacity=1, role="free")
    assert slot.try_acquire() is True
    assert slot.try_acquire() is False
    slot.release()
    assert slot.try_acquire() is True


def test_user_slot_release_clamped_at_zero():
    slot = _UserSlot(capacity=1, role="free")
    # release with no acquire must not go negative
    slot.release()
    slot.release()
    assert slot.in_use == 0


# ------------------------ UserConcurrencyLimiter ------------------------


def test_limit_for_returns_role_capacity():
    lim = UserConcurrencyLimiter(LIMITS)
    assert lim.limit_for("free", is_admin=False) == 1
    assert lim.limit_for("pro", is_admin=False) == 3
    assert lim.limit_for("internal", is_admin=False) == 10
    assert lim.limit_for("admin", is_admin=False) == 10


def test_limit_for_admin_flag_overrides_role():
    lim = UserConcurrencyLimiter(LIMITS)
    # is_admin=True wins even when role is free
    assert lim.limit_for("free", is_admin=True) == 10
    assert lim.limit_for("anything", is_admin=True) == 10


def test_limit_for_unknown_role_falls_back_to_free():
    lim = UserConcurrencyLimiter(LIMITS)
    assert lim.limit_for("unknown_role", is_admin=False) == 1
    assert lim.limit_for("", is_admin=False) == 1


@pytest.mark.asyncio
async def test_try_acquire_grants_until_capacity_then_rejects():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    # free → 1 slot
    assert await lim.try_acquire(user_id, "free", is_admin=False) is True
    assert await lim.try_acquire(user_id, "free", is_admin=False) is False


@pytest.mark.asyncio
async def test_release_frees_a_slot():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    await lim.try_acquire(user_id, "free", is_admin=False)
    assert await lim.try_acquire(user_id, "free", is_admin=False) is False
    lim.release(user_id)
    assert await lim.try_acquire(user_id, "free", is_admin=False) is True


@pytest.mark.asyncio
async def test_release_unknown_user_is_idempotent():
    lim = UserConcurrencyLimiter(LIMITS)
    # Must not raise when releasing a user we never saw
    lim.release("never-seen")


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets():
    lim = UserConcurrencyLimiter(LIMITS)
    assert await lim.try_acquire("user-A", "free", is_admin=False) is True
    # user-A is at cap, but user-B should still succeed
    assert await lim.try_acquire("user-B", "free", is_admin=False) is True


@pytest.mark.asyncio
async def test_pro_user_gets_three_slots():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "pro-1"
    for _ in range(3):
        assert await lim.try_acquire(user_id, "pro", is_admin=False) is True
    assert await lim.try_acquire(user_id, "pro", is_admin=False) is False


@pytest.mark.asyncio
async def test_admin_user_gets_ten_slots():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "admin-1"
    for _ in range(10):
        # role "free" but is_admin=True → admin cap
        assert await lim.try_acquire(user_id, "free", is_admin=True) is True
    assert await lim.try_acquire(user_id, "free", is_admin=True) is False


@pytest.mark.asyncio
async def test_capacity_is_sticky_after_creation():
    """Once a slot is created with a capacity, role changes don't resize it."""
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    # First acquire creates the slot at free=1
    await lim.try_acquire(user_id, "free", is_admin=False)
    # Subsequent acquires with role="pro" still see capacity=1
    assert await lim.try_acquire(user_id, "pro", is_admin=False) is False


@pytest.mark.asyncio
async def test_concurrent_acquires_respect_capacity():
    """Even with many concurrent tasks, capacity is not exceeded."""
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "pro-1"  # capacity 3

    results = await asyncio.gather(
        *[lim.try_acquire(user_id, "pro", is_admin=False) for _ in range(20)]
    )
    # Exactly 3 should win
    assert results.count(True) == 3
    assert results.count(False) == 17
