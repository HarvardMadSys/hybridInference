"""Tests for RouteWise ConcurrencyManager (K=0 binary gate)."""

from __future__ import annotations

import threading

import pytest

from routing.routewise.concurrency import ConcurrencyManager


@pytest.mark.unit
class TestConcurrencyManager:
    def test_initial_available(self):
        """Full capacity available at start."""
        mgr = ConcurrencyManager(4)
        assert mgr.available == 4
        assert mgr.active == 0
        assert mgr.limit == 4

    def test_try_acquire_reduces_available(self):
        """Each acquire decrements available by 1."""
        mgr = ConcurrencyManager(4)
        assert mgr.try_acquire() is True
        assert mgr.available == 3
        assert mgr.active == 1
        assert mgr.try_acquire() is True
        assert mgr.available == 2
        assert mgr.active == 2

    def test_try_acquire_at_capacity_returns_false(self):
        """Binary gate rejects when all slots are occupied."""
        mgr = ConcurrencyManager(2)
        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is False
        assert mgr.active == 2
        assert mgr.available == 0

    def test_release_increases_available(self):
        """Release returns a slot to the pool."""
        mgr = ConcurrencyManager(3)
        mgr.try_acquire()
        mgr.try_acquire()
        assert mgr.available == 1
        mgr.release()
        assert mgr.available == 2
        assert mgr.active == 1

    def test_release_at_zero_no_underflow(self):
        """Releasing with no active slots is a safe no-op."""
        mgr = ConcurrencyManager(4)
        assert mgr.active == 0
        mgr.release()  # Should not underflow.
        assert mgr.active == 0
        assert mgr.available == 4

    def test_update_limit_preserves_active_slots(self):
        """Live limit updates keep in-flight reservations accounted."""
        mgr = ConcurrencyManager(2)
        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is True

        mgr.update_limit(3)
        assert mgr.limit == 3
        assert mgr.active == 2
        assert mgr.available == 1
        assert mgr.try_acquire() is True

        mgr.update_limit(2)
        assert mgr.limit == 2
        assert mgr.active == 3
        assert mgr.available == 0
        assert mgr.try_acquire() is False

        stats = mgr.get_stats()
        assert stats["total_acquired"] == 3
        assert stats["total_rejected"] == 1
        assert stats["peak_active"] == 3

    def test_update_limit_rejects_invalid_limit(self):
        """Runtime updates use the same validation as construction."""
        mgr = ConcurrencyManager(2)

        with pytest.raises(ValueError, match="concurrency limit must be >= 1"):
            mgr.update_limit(0)

        assert mgr.limit == 2

    def test_congestion_price_zero_when_available(self):
        """Lambda = 0 when at least one slot is free."""
        mgr = ConcurrencyManager(3)
        assert mgr.get_congestion_price() == 0.0
        mgr.try_acquire()
        mgr.try_acquire()
        assert mgr.get_congestion_price() == 0.0  # Still 1 slot left.

    def test_congestion_price_inf_when_full(self):
        """Lambda = inf when all slots are occupied."""
        mgr = ConcurrencyManager(2)
        mgr.try_acquire()
        mgr.try_acquire()
        assert mgr.get_congestion_price() == float("inf")

    def test_concurrent_acquire_release(self):
        """Thread safety: concurrent acquires never exceed the limit."""
        limit = 4
        mgr = ConcurrencyManager(limit)
        acquired_count = 0
        lock = threading.Lock()

        def worker():
            nonlocal acquired_count
            if mgr.try_acquire():
                with lock:
                    acquired_count += 1
                # Verify invariant holds during concurrent execution.
                assert mgr.active <= limit

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly `limit` should have succeeded.
        assert acquired_count == limit
        assert mgr.active == limit
        assert mgr.available == 0

        # Release all and verify.
        for _ in range(limit):
            mgr.release()
        assert mgr.active == 0
        assert mgr.available == limit

    def test_stats_tracking(self):
        """Observability counters track acquire/reject/peak correctly."""
        mgr = ConcurrencyManager(2)

        # Acquire 2 (succeed), attempt 1 more (rejected).
        mgr.try_acquire()
        mgr.try_acquire()
        mgr.try_acquire()  # Rejected.

        stats = mgr.get_stats()
        assert stats["limit"] == 2
        assert stats["active"] == 2
        assert stats["available"] == 0
        assert stats["total_acquired"] == 2
        assert stats["total_rejected"] == 1
        assert stats["peak_active"] == 2

        # Release one, acquire again.
        mgr.release()
        mgr.try_acquire()

        stats = mgr.get_stats()
        assert stats["active"] == 2
        assert stats["total_acquired"] == 3
        assert stats["total_rejected"] == 1
        assert stats["peak_active"] == 2  # Peak unchanged.
