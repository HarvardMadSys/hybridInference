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
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is True
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is False


@pytest.mark.asyncio
async def test_release_frees_a_slot():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    await lim.try_acquire(user_id, "free", is_admin=False)
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is False
    lim.release(user_id)
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is True


@pytest.mark.asyncio
async def test_release_unknown_user_is_idempotent():
    lim = UserConcurrencyLimiter(LIMITS)
    # Must not raise when releasing a user we never saw
    lim.release("never-seen")


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets():
    lim = UserConcurrencyLimiter(LIMITS)
    granted_a, _, _ = await lim.try_acquire("user-A", "free", is_admin=False)
    assert granted_a is True
    # user-A is at cap, but user-B should still succeed
    granted_b, _, _ = await lim.try_acquire("user-B", "free", is_admin=False)
    assert granted_b is True


@pytest.mark.asyncio
async def test_pro_user_gets_three_slots():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "pro-1"
    for _ in range(3):
        granted, _, _ = await lim.try_acquire(user_id, "pro", is_admin=False)
        assert granted is True
    granted, _, _ = await lim.try_acquire(user_id, "pro", is_admin=False)
    assert granted is False


@pytest.mark.asyncio
async def test_admin_user_gets_ten_slots():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "admin-1"
    for _ in range(10):
        # role "free" but is_admin=True → admin cap
        granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=True)
        assert granted is True
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=True)
    assert granted is False


@pytest.mark.asyncio
async def test_capacity_is_sticky_after_creation():
    """Once a slot is created with a capacity, role changes don't resize it."""
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-1"
    # First acquire creates the slot at free=1
    await lim.try_acquire(user_id, "free", is_admin=False)
    # Subsequent acquires with role="pro" still see capacity=1
    granted, _, _ = await lim.try_acquire(user_id, "pro", is_admin=False)
    assert granted is False


@pytest.mark.asyncio
async def test_try_acquire_rejection_reports_sticky_cap_not_current_role():
    """Regression: when a user's role changes between requests, the 429
    response must report the cap that is *actually* enforced (the sticky slot
    cap) — not the higher cap that would apply to the new role.

    Sequence:
      1. Slot created for "user-sticky" with role="free"  → capacity=1.
      2. Slot is saturated (in_use == 1).
      3. try_acquire called again with role="pro" (e.g., role upgraded in DB).
      4. Should be rejected with capacity=1 (free cap), role_label="free" —
         the slot's sticky values — not capacity=3 / role_label="pro".
    """
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "user-sticky"

    # Step 1 + 2: create and saturate a free slot.
    granted, cap, label = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is True
    assert cap == 1
    assert label == "free"

    # Step 3: role "upgraded" to pro — but the slot is already sticky at free=1.
    granted, cap, label = await lim.try_acquire(user_id, "pro", is_admin=False)

    # Step 4: rejection must reflect the *sticky* cap, not the new role's cap.
    assert granted is False, "slot at capacity should be rejected"
    assert cap == 1, f"expected sticky capacity=1 (free), got {cap}"
    assert label == "free", f"expected sticky role_label='free', got '{label}'"


@pytest.mark.asyncio
async def test_concurrent_acquires_respect_capacity():
    """Even with many concurrent tasks, capacity is not exceeded."""
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "pro-1"  # capacity 3

    raw = await asyncio.gather(
        *[lim.try_acquire(user_id, "pro", is_admin=False) for _ in range(20)]
    )
    # Unpack the (granted, capacity, role_label) tuples
    granted_flags = [r[0] for r in raw]
    # Exactly 3 should win
    assert granted_flags.count(True) == 3
    assert granted_flags.count(False) == 17


# Prometheus-backed metric assertions removed: the prometheus stack was
# dropped from the project; the metric symbols are now no-op shims and there
# is no REGISTRY to scrape. Limiter behavior is covered by the tests above.
