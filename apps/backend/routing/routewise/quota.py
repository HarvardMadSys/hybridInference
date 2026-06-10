"""Quota pools for RouteWise S_Q routing.

Each route-level ``quota:`` block becomes one pool, keyed by ``quota_pool``.
The router only sees the :class:`QuotaPool` interface; whether the truth
source is a provider usage API or a local counter is an implementation
detail:

- :class:`SnapshotQuotaPool` -- provider-snapshot-backed. The provider
  dashboard (via :class:`~routing.routewise.quota_snapshot.ProviderQuotaSnapshotStore`)
  is the truth source; local consumption is an optimistic increment between
  refreshes.
- :class:`LocalQuotaPool` -- locally-accounted for providers without a usage
  API. Supports a timezone-aware ``daily`` reset window and a ``rolling``
  sliding window (e.g. a Claude-style 5-hour quota).

The shadow-price math itself lives in
:func:`effective_cost.quota_shadow_price_usd`, which reads ``L/U`` from the
workload :class:`CostEnvelopeEstimator`; pools only own depletion accounting.

Quota is counted in **requests** (not tokens), matching the online knapsack
formulation in the paper: each request routed to S_Q consumes one slot.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from .candidates import QuotaPolicy, QuotaSource
    from .quota_snapshot import ProviderQuotaSnapshotStore

logger = get_logger(__name__)


class QuotaPool(Protocol):
    """Router-facing interface for one quota resource pool."""

    @property
    def ready(self) -> bool:
        """Whether the pool has usable accounting state right now."""
        ...

    @property
    def limit(self) -> int:
        """Configured request limit for one window."""
        ...

    @property
    def remaining(self) -> int:
        """Requests remaining in the current window (non-negative)."""
        ...

    @property
    def used_fraction(self) -> float:
        """Fraction of the window's quota consumed, clamped to [0, 1]."""
        ...

    def consume(self) -> bool:
        """Atomically consume one request slot; False when exhausted."""
        ...


class LocalQuotaPool:
    """Locally-accounted quota pool for providers without a usage API.

    Args:
        policy: Route-level quota rule (limit + reset window).
        time_source: Injectable clock for rolling-window tests.
    """

    def __init__(
        self,
        policy: QuotaPolicy,
        *,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self._limit: int = policy.limit
        self._time = time_source
        self._lock = threading.Lock()
        self._rolling: deque[float] | None = None
        if policy.window.type == "rolling":
            self._rolling = deque()
            self._window_sec = float(policy.window.duration_sec)
        else:
            self._used_in_window: int = 0
            self._reset_tz = ZoneInfo(policy.window.timezone)
            self._last_reset_date: date = datetime.now(tz=self._reset_tz).date()

    @property
    def ready(self) -> bool:
        """Local accounting is always available."""
        return True

    @property
    def limit(self) -> int:
        """Configured request limit for one window."""
        return self._limit

    @property
    def remaining(self) -> int:
        """Requests remaining in the current window (non-negative)."""
        with self._lock:
            return max(0, self._limit - self._used_locked())

    @property
    def used(self) -> int:
        """Requests consumed in the current window."""
        with self._lock:
            return self._used_locked()

    @property
    def used_fraction(self) -> float:
        """Fraction of the window's quota consumed, clamped to [0, 1]."""
        with self._lock:
            return min(max(self._used_locked() / self._limit, 0.0), 1.0)

    def consume(self) -> bool:
        """Atomically consume one request slot; False when exhausted."""
        with self._lock:
            if self._used_locked() >= self._limit:
                return False
            if self._rolling is not None:
                self._rolling.append(self._time())
            else:
                self._used_in_window += 1
            return True

    def _used_locked(self) -> int:
        if self._rolling is not None:
            cutoff = self._time() - self._window_sec
            while self._rolling and self._rolling[0] <= cutoff:
                self._rolling.popleft()
            return len(self._rolling)
        today = datetime.now(tz=self._reset_tz).date()
        if today > self._last_reset_date:
            logger.info(
                "LocalQuotaPool daily reset: %s -> %s (used=%d)",
                self._last_reset_date,
                today,
                self._used_in_window,
            )
            self._used_in_window = 0
            self._last_reset_date = today
        return self._used_in_window


class SnapshotQuotaPool:
    """Provider-snapshot-backed quota pool.

    The provider's reported usage is the truth source; ``consume`` is an
    optimistic local increment reconciled at the next snapshot refresh. The
    pool is not ready until the first snapshot lands, so candidates are
    skipped rather than priced off invented state.
    """

    def __init__(
        self,
        store: ProviderQuotaSnapshotStore,
        source: QuotaSource,
        *,
        policy: QuotaPolicy,
    ) -> None:
        self._store = store
        self.source = source
        self.policy = policy
        self._limit_mismatch_warned = False

    @property
    def ready(self) -> bool:
        """Whether a provider snapshot has been observed yet."""
        return self._snapshot() is not None

    @property
    def limit(self) -> int:
        """Provider-reported limit when available, else the configured one."""
        snapshot = self._snapshot()
        if snapshot is None:
            return self.policy.limit
        return int(snapshot.limit)

    @property
    def remaining(self) -> int:
        """Requests remaining per the latest snapshot (0 when not ready)."""
        snapshot = self._snapshot()
        return 0 if snapshot is None else snapshot.remaining

    @property
    def used_fraction(self) -> float:
        """Used fraction per the latest snapshot (1.0 when not ready)."""
        snapshot = self._snapshot()
        return 1.0 if snapshot is None else snapshot.used_fraction

    def consume(self) -> bool:
        """Optimistically consume one slot against the snapshot store."""
        return self._store.consume(self.source)

    def _snapshot(self):
        snapshot = self._store.get(self.source)
        if (
            snapshot is not None
            and not self._limit_mismatch_warned
            and abs(snapshot.limit - self.policy.limit) >= 1
        ):
            logger.warning(
                "Quota pool for %s: provider reports limit=%s but route config "
                "declares limit=%d; using the provider-reported limit.",
                self.source,
                snapshot.limit,
                self.policy.limit,
            )
            self._limit_mismatch_warned = True
        return snapshot
