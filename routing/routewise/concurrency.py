"""Production concurrency slot manager for S_C (K=0 binary gate).

Manages concurrency-limited subscription slots with a simple binary gate:
admit if slots are available, reject otherwise.  No queue (K=0), no eviction,
no value-density tracking.

The K=0 design is the simplest correct model for concurrency-limited
subscriptions.  The upgrade path to K>0 (CAPQ-style queue with eviction)
is clean because K=0 is the degenerate case of K>0.

Congestion pricing follows the binary model:
- ``lambda = 0`` when at least one slot is available.
- ``lambda = inf`` when all slots are occupied.

Thread safety: all reads and writes of mutable state (``_active`` and the
observability counters) are protected by a ``threading.Lock``.  Critical
sections are short (no awaits held), matching the ``BaseRouter._lock``
pattern used elsewhere in the codebase.
"""

from __future__ import annotations

import threading

from serving.utils.logging import get_logger

logger = get_logger(__name__)


class ConcurrencyManager:
    """Production concurrency slot manager for S_C (K=0 binary gate).

    Args:
        config: ``RouteWiseConfig`` providing ``concurrency_limit``.
    """

    def __init__(self, config) -> None:
        self._limit: int = config.concurrency_limit
        self._active: int = 0
        self._lock = threading.Lock()
        # Observability counters.
        self._total_acquired: int = 0
        self._total_rejected: int = 0
        self._peak_active: int = 0

    @property
    def limit(self) -> int:
        """Maximum concurrent slots (immutable after init)."""
        return self._limit

    @property
    def active(self) -> int:
        """Currently occupied slots."""
        with self._lock:
            return self._active

    @property
    def available(self) -> int:
        """Slots available for immediate admission."""
        with self._lock:
            return max(0, self._limit - self._active)

    def try_acquire(self) -> bool:
        """Atomically acquire one slot. Returns False if at capacity."""
        with self._lock:
            if self._active < self._limit:
                self._active += 1
                self._peak_active = max(self._peak_active, self._active)
                self._total_acquired += 1
                return True
            self._total_rejected += 1
            return False

    def release(self) -> None:
        """Release one slot. Guards against underflow."""
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def get_congestion_price(self) -> float:
        """K=0 binary congestion price: 0.0 when available, inf when full."""
        with self._lock:
            if self._active < self._limit:
                return 0.0
            return float("inf")

    def get_stats(self) -> dict[str, int]:
        """Return observability snapshot.

        Returns:
            Dict with keys: limit, active, available, total_acquired,
            total_rejected, peak_active.
        """
        with self._lock:
            return {
                "limit": self._limit,
                "active": self._active,
                "available": max(0, self._limit - self._active),
                "total_acquired": self._total_acquired,
                "total_rejected": self._total_rejected,
                "peak_active": self._peak_active,
            }
