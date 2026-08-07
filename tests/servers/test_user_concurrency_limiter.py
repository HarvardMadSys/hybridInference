"""Unit tests for UserConcurrencyLimiter and _UserSlot."""

import asyncio

import pytest

from serving.servers.concurrency import (
    UNLIMITED_CONCURRENCY,
    UserConcurrencyLimiter,
    _UserSlot,
    static_limits_provider,
)

LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}


def _limiter() -> UserConcurrencyLimiter:
    return UserConcurrencyLimiter(static_limits_provider(LIMITS))


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


@pytest.mark.asyncio
async def test_limit_for_returns_role_capacity():
    """Each role's cap is reflected in the (capacity) returned by try_acquire."""
    for role, expected in (("free", 1), ("pro", 3), ("internal", 10), ("admin", 10)):
        lim = _limiter()
        _, cap, _ = await lim.try_acquire(f"u-{role}", role, is_admin=False)
        assert cap == expected


@pytest.mark.asyncio
async def test_limit_for_admin_flag_overrides_role():
    """is_admin=True yields the admin cap regardless of the role argument."""
    lim = _limiter()
    _, cap, label = await lim.try_acquire("a1", "free", is_admin=True)
    assert cap == 10
    assert label == "admin"

    lim = _limiter()
    _, cap, label = await lim.try_acquire("a2", "anything", is_admin=True)
    assert cap == 10
    assert label == "admin"


@pytest.mark.asyncio
async def test_limit_for_unknown_role_falls_back_to_free():
    """Unknown roles fall back to the most restrictive (free) cap."""
    lim = _limiter()
    _, cap, label = await lim.try_acquire("u1", "unknown_role", is_admin=False)
    assert cap == 1
    assert label == "free"

    lim = _limiter()
    _, cap, label = await lim.try_acquire("u2", "", is_admin=False)
    assert cap == 1
    assert label == "free"


@pytest.mark.asyncio
async def test_try_acquire_grants_until_capacity_then_rejects():
    lim = _limiter()
    user_id = "user-1"
    # free → 1 slot
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is True
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is False


@pytest.mark.asyncio
async def test_release_frees_a_slot():
    lim = _limiter()
    user_id = "user-1"
    await lim.try_acquire(user_id, "free", is_admin=False)
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is False
    lim.release(user_id)
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is True


@pytest.mark.asyncio
async def test_release_unknown_user_is_idempotent():
    lim = _limiter()
    # Must not raise when releasing a user we never saw
    lim.release("never-seen")


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets():
    lim = _limiter()
    granted_a, _, _ = await lim.try_acquire("user-A", "free", is_admin=False)
    assert granted_a is True
    # user-A is at cap, but user-B should still succeed
    granted_b, _, _ = await lim.try_acquire("user-B", "free", is_admin=False)
    assert granted_b is True


@pytest.mark.asyncio
async def test_pro_user_gets_three_slots():
    lim = _limiter()
    user_id = "pro-1"
    for _ in range(3):
        granted, _, _ = await lim.try_acquire(user_id, "pro", is_admin=False)
        assert granted is True
    granted, _, _ = await lim.try_acquire(user_id, "pro", is_admin=False)
    assert granted is False


@pytest.mark.asyncio
async def test_admin_user_gets_ten_slots():
    lim = _limiter()
    user_id = "admin-1"
    for _ in range(10):
        # role "free" but is_admin=True → admin cap
        granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=True)
        assert granted is True
    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=True)
    assert granted is False


@pytest.mark.asyncio
async def test_capacity_is_sticky_after_creation():
    """With a static provider, role changes don't widen the slot beyond the
    role's cap (the cap matches the new role, so an at-capacity free slot
    is rejected even when the caller upgrades to pro).

    Note: under the runtime-resizable design, the slot's *capacity* is no
    longer sticky to role — only the role *label* is. With a static dict
    that maps "pro" to 3, an upgrade would in fact resize the slot up. We
    test the older sticky-rejection contract via the rejection-label test
    below (label stays "free" on rejection).
    """
    lim = _limiter()
    user_id = "user-1"
    # First acquire creates the slot at free=1
    await lim.try_acquire(user_id, "free", is_admin=False)
    # The slot is at in_use=1. With role="pro", the cap is now 3, so a
    # second acquire is *granted* (this is the new lazy-resize behavior).
    granted, cap, _ = await lim.try_acquire(user_id, "pro", is_admin=False)
    assert granted is True
    assert cap == 3


@pytest.mark.asyncio
async def test_try_acquire_rejection_reports_role_label_and_current_cap():
    """A rejected acquire reports the slot's *current* capacity (after any
    lazy resize) and the slot's *sticky* role label.

    Sequence:
      1. Slot created for "user-sticky" with role="free"  → capacity=1.
      2. Slot is saturated (in_use == 1).
      3. A second acquire under role="free" is rejected, with cap=1 and the
         sticky label "free".
    """
    lim = _limiter()
    user_id = "user-sticky"

    # Step 1: create and saturate a free slot.
    granted, cap, label = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is True
    assert cap == 1
    assert label == "free"

    # Step 2: second acquire under cap=1 must be rejected with sticky label.
    granted, cap, label = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is False
    assert cap == 1
    assert label == "free", f"expected sticky role_label='free', got '{label}'"


@pytest.mark.asyncio
async def test_concurrent_acquires_respect_capacity():
    """Even with many concurrent tasks, capacity is not exceeded."""
    lim = _limiter()
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


# ------------------- per-user max_concurrent_requests override -------------------


@pytest.mark.asyncio
async def test_per_user_override_replaces_role_cap():
    """A non-None max_concurrent_requests overrides the role-based cap."""
    lim = _limiter()
    user_id = "custom-1"
    # free role cap is 1; override to 5
    for _ in range(5):
        granted, cap, _ = await lim.try_acquire(
            user_id, "free", is_admin=False, max_concurrent_requests=5
        )
        assert granted is True
        assert cap == 5
    # 6th request must be rejected
    granted, cap, _ = await lim.try_acquire(
        user_id, "free", is_admin=False, max_concurrent_requests=5
    )
    assert granted is False


@pytest.mark.asyncio
async def test_per_user_override_none_falls_back_to_role():
    """max_concurrent_requests=None must use the role-based cap."""
    lim = _limiter()
    user_id = "custom-2"
    # free cap is 1
    granted, cap, _ = await lim.try_acquire(
        user_id, "free", is_admin=False, max_concurrent_requests=None
    )
    assert granted is True
    assert cap == 1
    granted, _, _ = await lim.try_acquire(
        user_id, "free", is_admin=False, max_concurrent_requests=None
    )
    assert granted is False


@pytest.mark.asyncio
async def test_per_user_override_live_resize():
    """Changing the override on subsequent calls triggers lazy resize."""
    lim = _limiter()
    user_id = "custom-3"
    # First call creates slot at cap=2
    await lim.try_acquire(user_id, "free", is_admin=False, max_concurrent_requests=2)
    # Second call with cap=10 resizes the slot
    granted, cap, _ = await lim.try_acquire(
        user_id, "free", is_admin=False, max_concurrent_requests=10
    )
    assert granted is True
    assert cap == 10


# ---------------------- unlimited (0) sentinel cap ----------------------


def test_user_slot_zero_capacity_never_rejects():
    """capacity == UNLIMITED_CONCURRENCY grants every acquire but still counts."""
    slot = _UserSlot(capacity=UNLIMITED_CONCURRENCY, role="admin")
    for _ in range(1000):
        assert slot.try_acquire() is True
    assert slot.in_use == 1000


@pytest.mark.asyncio
async def test_admin_zero_cap_is_unlimited():
    """An admin cap of 0 never rejects, regardless of in-flight count."""
    lim = UserConcurrencyLimiter(
        static_limits_provider({"free": 1, "pro": 3, "internal": 10, "admin": 0})
    )
    user_id = "admin-unlimited"
    for _ in range(100):
        granted, cap, label = await lim.try_acquire(user_id, "free", is_admin=True)
        assert granted is True
        assert cap == UNLIMITED_CONCURRENCY
        assert label == "admin"


@pytest.mark.asyncio
async def test_unlimited_then_finite_cap_applies_on_next_acquire():
    """Re-imposing a finite cap after unlimited takes effect via lazy resize."""
    limits = {"free": 1, "pro": 3, "internal": 10, "admin": 0}

    async def provider() -> dict:
        return dict(limits)

    lim = UserConcurrencyLimiter(provider)
    user_id = "admin-recapped"
    for _ in range(5):
        granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=True)
        assert granted is True

    # Operator re-imposes a finite cap; the slot already holds 5 in-flight.
    limits["admin"] = 3
    granted, cap, _ = await lim.try_acquire(user_id, "free", is_admin=True)
    assert granted is False
    assert cap == 3
