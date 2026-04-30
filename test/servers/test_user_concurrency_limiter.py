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


# ------------------------------ metrics ---------------------------------


def _read_counter(metric_name: str, **labels) -> float:
    """Read the float value of a metric from the project's REGISTRY.

    Uses the public ``CollectorRegistry.get_sample_value`` API instead of the
    private ``._value.get()`` attribute, which can break across
    prometheus_client versions or when metrics are in no-op mode.
    """
    from serving.observability.metrics import REGISTRY

    if REGISTRY is None:
        # METRICS_ENABLED=0 — no real metrics; return 0 so delta assertions pass.
        return 0.0
    value = REGISTRY.get_sample_value(metric_name, labels)
    return value if value is not None else 0.0


@pytest.mark.asyncio
async def test_metrics_granted_increments_acquires_and_in_flight():
    lim = UserConcurrencyLimiter(LIMITS)

    granted_before = _read_counter(
        "user_concurrency_acquires_total", role="free", outcome="granted"
    )
    in_flight_before = _read_counter("user_concurrency_in_flight", role="free")

    granted, _, _ = await lim.try_acquire("metric-user-1", "free", is_admin=False)
    assert granted is True

    granted_after = _read_counter("user_concurrency_acquires_total", role="free", outcome="granted")
    in_flight_after = _read_counter("user_concurrency_in_flight", role="free")

    assert granted_after - granted_before == 1
    assert in_flight_after - in_flight_before == 1


@pytest.mark.asyncio
async def test_metrics_rejected_increments_rejected_and_acquires():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "metric-user-2"
    await lim.try_acquire(user_id, "free", is_admin=False)

    rejected_before = _read_counter("user_concurrency_rejected_total", role="free")
    rejected_acq_before = _read_counter(
        "user_concurrency_acquires_total", role="free", outcome="rejected"
    )

    granted, _, _ = await lim.try_acquire(user_id, "free", is_admin=False)
    assert granted is False

    rejected_after = _read_counter("user_concurrency_rejected_total", role="free")
    rejected_acq_after = _read_counter(
        "user_concurrency_acquires_total", role="free", outcome="rejected"
    )

    assert rejected_after - rejected_before == 1
    assert rejected_acq_after - rejected_acq_before == 1


@pytest.mark.asyncio
async def test_metrics_release_decrements_in_flight():
    lim = UserConcurrencyLimiter(LIMITS)
    user_id = "metric-user-3"
    await lim.try_acquire(user_id, "free", is_admin=False)

    in_flight_before_release = _read_counter("user_concurrency_in_flight", role="free")
    lim.release(user_id)
    in_flight_after_release = _read_counter("user_concurrency_in_flight", role="free")

    assert in_flight_before_release - in_flight_after_release == 1


@pytest.mark.asyncio
async def test_metrics_admin_label_used_when_is_admin():
    lim = UserConcurrencyLimiter(LIMITS)
    in_flight_before = _read_counter("user_concurrency_in_flight", role="admin")
    # role="free" but is_admin=True → label should be "admin"
    await lim.try_acquire("admin-user-x", "free", is_admin=True)
    in_flight_after = _read_counter("user_concurrency_in_flight", role="admin")
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
