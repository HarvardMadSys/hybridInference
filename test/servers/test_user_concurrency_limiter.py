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


# ------------------------------ metrics ---------------------------------


def _read_counter(counter, **labels) -> float:
    """Read the float value of a Counter or Gauge with given labels."""
    return counter.labels(**labels)._value.get()


@pytest.mark.asyncio
async def test_metrics_granted_increments_acquires_and_in_flight():
    from serving.observability.metrics import (
        USER_CONCURRENCY_ACQUIRES_TOTAL,
        USER_CONCURRENCY_IN_FLIGHT,
    )

    lim = UserConcurrencyLimiter(LIMITS)

    granted_before = _read_counter(
        USER_CONCURRENCY_ACQUIRES_TOTAL, role="free", outcome="granted"
    )
    in_flight_before = _read_counter(USER_CONCURRENCY_IN_FLIGHT, role="free")

    assert await lim.try_acquire("metric-user-1", "free", is_admin=False) is True

    granted_after = _read_counter(
        USER_CONCURRENCY_ACQUIRES_TOTAL, role="free", outcome="granted"
    )
    in_flight_after = _read_counter(USER_CONCURRENCY_IN_FLIGHT, role="free")

    assert granted_after - granted_before == 1
    assert in_flight_after - in_flight_before == 1


@pytest.mark.asyncio
async def test_metrics_rejected_increments_rejected_and_acquires():
    from serving.observability.metrics import (
        USER_CONCURRENCY_ACQUIRES_TOTAL,
        USER_CONCURRENCY_REJECTED_TOTAL,
    )

    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "metric-user-2"
    await lim.try_acquire(user_id, "free", is_admin=False)

    rejected_before = _read_counter(USER_CONCURRENCY_REJECTED_TOTAL, role="free")
    rejected_acq_before = _read_counter(
        USER_CONCURRENCY_ACQUIRES_TOTAL, role="free", outcome="rejected"
    )

    assert await lim.try_acquire(user_id, "free", is_admin=False) is False

    rejected_after = _read_counter(USER_CONCURRENCY_REJECTED_TOTAL, role="free")
    rejected_acq_after = _read_counter(
        USER_CONCURRENCY_ACQUIRES_TOTAL, role="free", outcome="rejected"
    )

    assert rejected_after - rejected_before == 1
    assert rejected_acq_after - rejected_acq_before == 1


@pytest.mark.asyncio
async def test_metrics_release_decrements_in_flight():
    from serving.observability.metrics import USER_CONCURRENCY_IN_FLIGHT

    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "metric-user-3"
    await lim.try_acquire(user_id, "free", is_admin=False)

    in_flight_before_release = _read_counter(USER_CONCURRENCY_IN_FLIGHT, role="free")
    lim.release(user_id)
    in_flight_after_release = _read_counter(USER_CONCURRENCY_IN_FLIGHT, role="free")

    assert in_flight_before_release - in_flight_after_release == 1


@pytest.mark.asyncio
async def test_metrics_admin_label_used_when_is_admin():
    from serving.observability.metrics import USER_CONCURRENCY_IN_FLIGHT

    lim = UserConcurrencyLimiter(LIMITS)
    in_flight_before = _read_counter(USER_CONCURRENCY_IN_FLIGHT, role="admin")
    # role="free" but is_admin=True → label should be "admin"
    await lim.try_acquire("admin-user-x", "free", is_admin=True)
    in_flight_after = _read_counter(USER_CONCURRENCY_IN_FLIGHT, role="admin")
    assert in_flight_after - in_flight_before == 1


@pytest.mark.asyncio
async def test_metrics_appear_in_render_latest():
    """The metrics must be registered in the project's REGISTRY so they
    are visible in /metrics scrapes (regression for the Critical issue
    where they were registered against the default global registry only)."""
    from serving.observability.metrics import _ENABLED as _METRICS_ENABLED

    if not _METRICS_ENABLED:
        pytest.skip("Metrics disabled at module import time")

    from serving.observability.metrics import render_latest

    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "scrape-test-user"
    # Drive at least one grant + reject + release so labels exist
    await lim.try_acquire(user_id, "free", is_admin=False)
    await lim.try_acquire(user_id, "free", is_admin=False)  # rejected
    lim.release(user_id)

    output = render_latest()
    assert b"user_concurrency_in_flight" in output
    assert b"user_concurrency_acquires_total" in output
    assert b"user_concurrency_rejected_total" in output
