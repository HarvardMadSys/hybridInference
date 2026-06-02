"""RouteWise workload cost-envelope estimator.

``L`` and ``U`` are workload-level request-cost percentiles used by the quota
shadow-price curve. They must be calibrated from real observations: a snapshot
is only returned once the pool has accumulated samples. There is intentionally
no seed fallback because operating on a fabricated envelope would violate the
paper's assumption that ``[L, U]`` is derived from the workload itself.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


class EnvelopeNotCalibratedError(RuntimeError):
    """Raised when RouteWise refuses to operate without a calibrated envelope."""


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (max(0.0, min(100.0, p)) / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    frac = rank - lower
    return ordered[lower] * (1.0 - frac) + ordered[upper] * frac


@dataclass(frozen=True)
class CostEnvelopeSnapshot:
    """Current ``L/U`` envelope for one RouteWise pool."""

    lower: float
    upper: float
    sample_count: int


@dataclass
class CostEnvelopeEstimator:
    """Sliding-window percentile estimator for workload request costs."""

    lower_percentile: float = 10.0
    upper_percentile: float = 90.0
    window_sec: float = 24 * 3600.0
    _samples: dict[str, deque[tuple[float, float]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def observe(self, pool: str, cost_usd: float, *, now: float | None = None) -> None:
        """Add one cheapest API-equivalent cost sample."""
        if cost_usd <= 0:
            return
        ts = time.time() if now is None else now
        with self._lock:
            samples = self._samples[pool]
            samples.append((ts, float(cost_usd)))
            self._prune_locked(pool, ts)

    def snapshot(self, pool: str, *, now: float | None = None) -> CostEnvelopeSnapshot | None:
        """Return current ``L/U`` for *pool*, or ``None`` if uncalibrated."""
        ts = time.time() if now is None else now
        with self._lock:
            self._prune_locked(pool, ts)
            values = [cost for _t, cost in self._samples.get(pool, ())]
        if not values:
            return None
        lower = max(_percentile(values, self.lower_percentile), 1e-12)
        upper = max(_percentile(values, self.upper_percentile), lower)
        return CostEnvelopeSnapshot(
            lower=lower,
            upper=upper,
            sample_count=len(values),
        )

    def _prune(self, pool: str, now: float) -> None:
        with self._lock:
            self._prune_locked(pool, now)

    def _prune_locked(self, pool: str, now: float) -> None:
        samples = self._samples.get(pool)
        if not samples:
            return
        cutoff = now - self.window_sec
        while samples and samples[0][0] < cutoff:
            samples.popleft()
