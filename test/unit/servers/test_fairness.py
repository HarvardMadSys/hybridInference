"""Unit tests for the VTC fairness scheduler.

Tests cover:
- Counter lift behaviour: idle system, active system
- Scheduling order: lowest CURRENT counter user is dispatched first,
  including after a counter update from on_request_finish
- Immediate dispatch when queue is empty and capacity is available
- on_request_finish counter updates
- Idle-user re-join preserves accumulated counter (no free credits)
- Timeout handling and queue cleanup
- Multi-model isolation
- No rate_limiter configured (pass-through ordering only)
- Abstract base class interface
- O(n) pick correctness after counter changes mid-queue
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.fairness import (
    FairnessScheduler,
    VTCFairnessScheduler,
    _VTCModelState,
    _WaitingEntry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_unlimited_rate_limiter() -> MagicMock:
    """Return a mock rate_limiter that always grants tokens immediately."""
    rl = MagicMock()
    rl.buckets = {}
    rl.try_consume_tokens = AsyncMock(return_value=(True, 999_999.0))
    return rl


def _make_empty_rate_limiter() -> MagicMock:
    """Return a mock rate_limiter that never grants tokens."""
    rl = MagicMock()
    rl.buckets = {}
    rl.try_consume_tokens = AsyncMock(return_value=(False, 60.0))
    return rl


def _make_scheduler(rate_limiter=None) -> VTCFairnessScheduler:
    return VTCFairnessScheduler(rate_limiter=rate_limiter)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class TestFairnessSchedulerInterface:
    def test_is_abstract(self):
        """FairnessScheduler cannot be instantiated directly."""
        with pytest.raises(TypeError):
            FairnessScheduler()  # type: ignore[abstract]

    def test_vtc_is_concrete_subclass(self):
        s = VTCFairnessScheduler()
        assert isinstance(s, FairnessScheduler)


# ---------------------------------------------------------------------------
# Counter lift — idle system
# ---------------------------------------------------------------------------


class TestCounterLiftIdleSystem:
    @pytest.mark.asyncio
    async def test_new_user_starts_at_zero_when_system_idle(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        success, meta = await s.acquire("model-a", "alice", 100)
        assert success
        state = s._get_state("model-a")
        # Counter starts at 0, on_request_finish not yet called
        assert state.counters.get("alice", 0.0) == 0.0

    @pytest.mark.asyncio
    async def test_rejoin_after_idle_lifted_to_last_active_counter(self):
        """After all users go idle, last_active_counter is preserved.
        A re-joining user should not get a free ride from counter = 0.
        """
        rl = _make_unlimited_rate_limiter()
        s = _make_scheduler(rl)
        model = "model-b"

        await s.acquire(model, "alice", 50)
        await s.on_request_finish(model, "alice", 30, 20)  # cost = 50

        state = s._get_state(model)
        assert state.counters["alice"] == pytest.approx(50.0)
        assert state.last_active_counter == pytest.approx(50.0)

        # Force alice out of active_users to simulate her being idle
        state.active_users.discard("alice")

        # alice re-joins; counter should NOT fall below last_active_counter
        await s.acquire(model, "alice", 10)
        assert state.counters["alice"] >= state.last_active_counter

    @pytest.mark.asyncio
    async def test_brand_new_user_lifted_to_last_active_counter(self):
        """A brand-new user joining after the system went idle gets lifted."""
        rl = _make_unlimited_rate_limiter()
        s = _make_scheduler(rl)
        model = "model-c"

        await s.acquire(model, "alice", 100)
        await s.on_request_finish(model, "alice", 60, 40)  # cost = 100

        state = s._get_state(model)
        assert state.last_active_counter == pytest.approx(100.0)

        # bob joins a completely idle system
        await s.acquire(model, "bob", 10)
        assert state.counters["bob"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Counter lift — active system
# ---------------------------------------------------------------------------


class TestCounterLiftActiveSystem:
    @pytest.mark.asyncio
    async def test_new_user_lifted_to_min_active_counter(self):
        """A new user joining an active system is lifted to the minimum counter."""
        rl = _make_unlimited_rate_limiter()
        s = _make_scheduler(rl)
        model = "model-d"

        await s.acquire(model, "alice", 10)
        await s.acquire(model, "bob", 10)

        state = s._get_state(model)
        state.counters["alice"] = 200.0
        state.counters["bob"] = 100.0
        state.active_users = {"alice", "bob"}

        # charlie joins; both alice and bob are active
        # charlie should be lifted to min(200, 100) = 100
        await s.acquire(model, "charlie", 10)
        assert state.counters["charlie"] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_user_with_higher_counter_not_reduced(self):
        """If a user's stored counter exceeds the lift target, it stays unchanged."""
        rl = _make_unlimited_rate_limiter()
        s = _make_scheduler(rl)
        model = "model-e"

        await s.acquire(model, "alice", 10)

        state = s._get_state(model)
        state.counters["alice"] = 500.0
        state.active_users = {"alice"}
        state.counters["bob"] = 600.0
        # bob is NOT in active_users → counter-lift logic will run

        await s.acquire(model, "bob", 10)
        # bob counter (600) > min_active (500) → stays 600
        assert state.counters["bob"] == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# Immediate dispatch (fast path)
# ---------------------------------------------------------------------------


class TestImmediateDispatch:
    @pytest.mark.asyncio
    async def test_immediate_when_queue_empty_and_capacity_available(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        success, meta = await s.acquire("m", "alice", 100)
        assert success
        assert meta["fairness"] == "immediate"

    @pytest.mark.asyncio
    async def test_immediate_without_rate_limiter(self):
        """No rate_limiter → always fast-path immediate dispatch."""
        s = _make_scheduler(rate_limiter=None)
        success, meta = await s.acquire("m", "alice", 999)
        assert success
        assert meta["fairness"] == "immediate"

    @pytest.mark.asyncio
    async def test_tokens_consumed_reported_in_metadata(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        success, meta = await s.acquire("m", "alice", 42)
        assert success
        assert meta["tokens_consumed"] == 42


# ---------------------------------------------------------------------------
# on_request_finish counter update
# ---------------------------------------------------------------------------


class TestOnRequestFinish:
    @pytest.mark.asyncio
    async def test_counter_incremented_by_token_sum(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        model = "m"
        await s.acquire(model, "alice", 10)
        await s.on_request_finish(model, "alice", 30, 20)  # cost = 50
        state = s._get_state(model)
        assert state.counters["alice"] == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_multiple_completions_accumulate(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        model = "m"
        await s.acquire(model, "alice", 10)
        await s.on_request_finish(model, "alice", 100, 50)  # 150
        await s.acquire(model, "alice", 10)
        await s.on_request_finish(model, "alice", 200, 100)  # +300
        state = s._get_state(model)
        assert state.counters["alice"] == pytest.approx(450.0)

    @pytest.mark.asyncio
    async def test_zero_tokens_on_error_does_not_crash(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        model = "m"
        await s.acquire(model, "alice", 10)
        await s.on_request_finish(model, "alice", 0, 0)
        state = s._get_state(model)
        assert state.counters["alice"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_finish_without_prior_acquire_does_not_crash(self):
        """on_request_finish is robust even when called without a matching acquire."""
        s = _make_scheduler(_make_unlimited_rate_limiter())
        await s.on_request_finish("m", "ghost_user", 10, 10)
        state = s._get_state("m")
        assert state.counters.get("ghost_user", 0.0) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Scheduling order — correctness of O(n) pick using CURRENT counters
# ---------------------------------------------------------------------------


class TestSchedulingOrder:
    @pytest.mark.asyncio
    async def test_lower_counter_user_dispatched_first(self):
        """The user with the lower VTC counter should be dispatched first
        when capacity becomes available.
        """
        grant_state = {"enabled": False, "n": 0}

        async def _try_consume(model_id: str, tokens: int):
            if grant_state["enabled"] and grant_state["n"] < 1:
                grant_state["n"] += 1
                return True, 999_999.0
            return False, 0.0

        rl = MagicMock()
        rl.buckets = {}
        rl.try_consume_tokens = AsyncMock(side_effect=_try_consume)

        s = _make_scheduler(rl)
        model = "sched-test"
        state = s._get_state(model)

        # Pre-populate: both users active with different counters
        state.counters["alice"] = 500.0
        state.counters["bob"] = 100.0
        state.active_users = {"alice", "bob"}

        dispatched_order: list[str] = []

        async def _request(user_id: str):
            ok, _ = await s.acquire(model, user_id, 10, timeout=3.0)
            if ok:
                dispatched_order.append(user_id)

        task_a = asyncio.create_task(_request("alice"))
        task_b = asyncio.create_task(_request("bob"))

        # Both queue since capacity is off
        await asyncio.sleep(0.2)

        # Grant exactly one slot
        grant_state["enabled"] = True

        await asyncio.gather(task_a, task_b, return_exceptions=True)

        assert "bob" in dispatched_order, "bob (counter=100) should be dispatched"

    @pytest.mark.asyncio
    async def test_counter_update_affects_future_dispatch_order(self):
        """Core correctness test: after A's first request finishes and their
        counter increases, A's second queued request should yield priority
        to user B who now has a lower counter.

        With a static heap this would fail (stale snapshot), but with O(n)
        scan on current counters it must pass.
        """
        call_count = {"n": 0}

        async def _try_consume(model_id: str, tokens: int):
            # Grant every other call so we can control dispatch order
            call_count["n"] += 1
            return True, 999.0

        rl = MagicMock()
        rl.buckets = {}
        rl.try_consume_tokens = AsyncMock(side_effect=_try_consume)

        s = _make_scheduler(rl)
        model = "order-test"
        state = s._get_state(model)

        # Set up: alice=50, bob=200. Both active, both have a pending request.
        state.counters["alice"] = 50.0
        state.counters["bob"] = 200.0
        state.active_users = {"alice", "bob"}

        # Manually inject a second pending request for alice
        event_alice2 = asyncio.Event()
        entry_alice2 = _WaitingEntry(
            user_id="alice",
            estimated_tokens=10,
            event=event_alice2,
        )
        state._waiting_queues["alice"] = deque([entry_alice2])

        event_bob = asyncio.Event()
        entry_bob = _WaitingEntry(
            user_id="bob",
            estimated_tokens=10,
            event=event_bob,
        )
        state._waiting_queues["bob"] = deque([entry_bob])

        # Simulate: alice's first request just finished with a high cost (500)
        # → alice.counter = 50 + 500 = 550  >  bob.counter = 200
        await state.on_request_finish("alice", 400, 100)  # cost = 500

        assert state.counters["alice"] == pytest.approx(550.0)

        # Now the watcher should pick bob (counter=200) over alice (counter=550)
        next_user = state._pick_next_user()
        assert next_user == "bob", (
            f"Expected bob (counter=200) to be picked, got {next_user} "
            f"(alice counter={state.counters['alice']:.0f})"
        )

    @pytest.mark.asyncio
    async def test_equal_counters_both_dispatched(self):
        """When counters are equal both requests should eventually succeed."""
        rl = _make_unlimited_rate_limiter()
        s = _make_scheduler(rl)
        model = "eq-test"
        state = s._get_state(model)
        state.counters["alice"] = 100.0
        state.counters["bob"] = 100.0

        ok_a, _ = await s.acquire(model, "alice", 10)
        ok_b, _ = await s.acquire(model, "bob", 10)
        assert ok_a and ok_b


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_meta(self):
        s = _make_scheduler(_make_empty_rate_limiter())
        success, meta = await s.acquire("m", "alice", 100, timeout=0.05)
        assert not success
        assert "error" in meta
        assert "retry_after" in meta

    @pytest.mark.asyncio
    async def test_timeout_cleans_up_from_queue(self):
        s = _make_scheduler(_make_empty_rate_limiter())
        state = s._get_state("m")
        await s.acquire("m", "alice", 100, timeout=0.05)
        # All per-user queues should be cleaned up after timeout
        live = sum(1 for q in state._waiting_queues.values() for e in q if not e.timed_out)
        assert live == 0


# ---------------------------------------------------------------------------
# Multi-model isolation
# ---------------------------------------------------------------------------


class TestMultiModelIsolation:
    @pytest.mark.asyncio
    async def test_counters_independent_per_model(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())

        await s.acquire("model-x", "alice", 10)
        await s.on_request_finish("model-x", "alice", 100, 50)  # counter=150

        await s.acquire("model-y", "alice", 10)
        state_y = s._get_state("model-y")
        assert state_y.counters.get("alice", 0.0) == pytest.approx(0.0)

        state_x = s._get_state("model-x")
        assert state_x.counters["alice"] == pytest.approx(150.0)

    @pytest.mark.asyncio
    async def test_active_users_independent_per_model(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())

        await s.acquire("model-x", "alice", 10)
        state_y = s._get_state("model-y")
        assert "alice" not in state_y.active_users

    @pytest.mark.asyncio
    async def test_get_status_per_model(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        await s.acquire("model-x", "alice", 10)

        status = s.get_status("model-y")
        assert status["state"] == "no_requests_seen"

        status_x = s.get_status("model-x")
        assert "counters" in status_x


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_status_reflects_counters(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        model = "status-test"
        await s.acquire(model, "alice", 10)
        await s.on_request_finish(model, "alice", 20, 30)

        status = s.get_status(model)
        assert status["counters"]["alice"] == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_get_all_status_lists_all_models(self):
        s = _make_scheduler(_make_unlimited_rate_limiter())
        await s.acquire("m1", "alice", 10)
        await s.acquire("m2", "bob", 10)

        all_status = s.get_all_status()
        assert "m1" in all_status
        assert "m2" in all_status

    def test_unknown_model_returns_no_requests_seen(self):
        s = _make_scheduler()
        status = s.get_status("unknown-model")
        assert status["state"] == "no_requests_seen"

    @pytest.mark.asyncio
    async def test_waiting_queue_shows_current_counter(self):
        """Status should show the current VTC counter, not the arrival snapshot."""
        rl = _make_empty_rate_limiter()
        s = _make_scheduler(rl)
        model = "status-live"
        state = s._get_state(model)

        # Pre-set counter and enqueue manually
        state.counters["alice"] = 300.0
        state.active_users = {"alice"}
        event = asyncio.Event()
        entry = _WaitingEntry(user_id="alice", estimated_tokens=10, event=event)
        state._waiting_queues["alice"] = deque([entry])

        status = state.get_status()
        waiting = status["waiting_queue"]
        assert len(waiting) == 1
        assert waiting[0]["current_vtc_counter"] == pytest.approx(300.0)

        # Update counter — status should reflect the new value
        state.counters["alice"] = 700.0
        status2 = state.get_status()
        assert status2["waiting_queue"][0]["current_vtc_counter"] == pytest.approx(700.0)


# ---------------------------------------------------------------------------
# _VTCModelState._pick_next_user directly
# ---------------------------------------------------------------------------


class TestPickNextUser:
    @pytest.mark.asyncio
    async def test_pick_returns_none_when_empty(self):
        state = _VTCModelState("m", None)
        assert state._pick_next_user() is None

    @pytest.mark.asyncio
    async def test_pick_lowest_counter(self):
        state = _VTCModelState("m", None)
        state.counters = {"alice": 500.0, "bob": 100.0, "carol": 300.0}
        for uid in ("alice", "bob", "carol"):
            event = asyncio.Event()
            state._waiting_queues[uid] = deque(
                [_WaitingEntry(user_id=uid, estimated_tokens=10, event=event)]
            )
        assert state._pick_next_user() == "bob"

    @pytest.mark.asyncio
    async def test_pick_skips_timed_out_queues(self):
        state = _VTCModelState("m", None)
        state.counters = {"alice": 100.0, "bob": 200.0}

        # alice's only entry is timed out
        event_a = asyncio.Event()
        dead = _WaitingEntry(user_id="alice", estimated_tokens=10, event=event_a)
        dead.timed_out = True
        state._waiting_queues["alice"] = deque([dead])

        event_b = asyncio.Event()
        live = _WaitingEntry(user_id="bob", estimated_tokens=10, event=event_b)
        state._waiting_queues["bob"] = deque([live])

        # bob should be picked even though alice has a lower counter,
        # because alice's queue is entirely dead
        assert state._pick_next_user() == "bob"

    @pytest.mark.asyncio
    async def test_last_active_counter_updated_on_leave(self):
        rl = _make_unlimited_rate_limiter()
        state = _VTCModelState("m", rl)

        await state.acquire("alice", 10, timeout=5.0)
        await state.on_request_finish("alice", 40, 10)  # cost=50

        assert state.last_active_counter == pytest.approx(50.0)
        assert "alice" not in state.active_users
