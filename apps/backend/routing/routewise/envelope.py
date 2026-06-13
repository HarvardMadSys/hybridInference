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
    """Sliding-window percentile estimator for workload request costs.

    ``min_samples`` gates calibration: below it ``snapshot`` returns ``None``
    (callers already treat that as "skip quota candidates"). The default of 1
    means any observed sample calibrates -- the floor fallback below keeps the
    curve well-formed even for degenerate windows. Raise it per model
    (``envelope_min_samples``) to trade startup friction for percentile
    stability on low-traffic pools.
    """

    lower_percentile: float = 10.0
    upper_percentile: float = 90.0
    window_sec: float = 24 * 3600.0
    min_samples: int = 1
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
        if len(values) < max(int(self.min_samples), 1):
            return None
        upper = _percentile(values, self.upper_percentile)
        if upper <= 0:
            return None
        lower = _percentile(values, self.lower_percentile)
        if lower <= 0 or lower >= upper:
            # Paper floor fallback: keep 0 < L < U with a bounded U/L ratio so
            # the quota shadow-price curve neither flattens (L == U) nor turns
            # into a near-step function (L ~ 0).
            lower = upper * 1e-3
        return CostEnvelopeSnapshot(
            lower=lower,
            upper=upper,
            sample_count=len(values),
        )

    def sample_count(self, pool: str, *, now: float | None = None) -> int:
        """Return the in-window sample count for *pool* (even when uncalibrated)."""
        ts = time.time() if now is None else now
        with self._lock:
            self._prune_locked(pool, ts)
            return len(self._samples.get(pool, ()))

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
