"""RouteWise workload cost-envelope estimator.

``L`` and ``U`` are workload-level request-cost percentiles used by the quota
shadow-price curve. They must be calibrated from real observations: a snapshot
is only returned once the pool has accumulated samples. There is intentionally
no seed fallback because operating on a fabricated envelope would violate the
paper's assumption that ``[L, U]`` is derived from the workload itself.

``snapshot`` sits on the per-request routing path, so its cost must not grow
with traffic. Three things keep it bounded: the sample window is capped
(``max_samples``), both percentiles come out of a single sort, and the result
is memoized for ``cache_ttl_sec`` of observation time.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


class EnvelopeNotCalibratedError(RuntimeError):
    """Raised when RouteWise refuses to operate without a calibrated envelope."""


def _percentile_from_sorted(ordered: list[float], p: float) -> float:
    """Return the linearly interpolated percentile of an already sorted list."""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
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

    ``max_samples`` caps how many samples one pool retains inside the window,
    the way ``ProviderProfile.max_samples`` caps the latency window. Without a
    cap the window holds one sample per request for its whole duration, so both
    memory and the cost of the ``snapshot`` sort grow with traffic. ``0`` keeps
    the unbounded behavior.

    ``cache_ttl_sec`` memoizes ``snapshot`` for that many seconds of the caller's
    own clock. Percentiles over a window measured in hours do not move between
    two requests a few milliseconds apart, so recomputing them per request buys
    nothing. ``0`` disables the cache and recomputes every call. An uncalibrated
    (``None``) result is never held across a new observation, so a cold pool
    still calibrates on the first sample rather than after the TTL.
    """

    lower_percentile: float = 10.0
    upper_percentile: float = 90.0
    window_sec: float = 24 * 3600.0
    min_samples: int = 1
    max_samples: int = 0
    cache_ttl_sec: float = 0.0
    _samples: dict[str, deque[tuple[float, float]]] = field(default_factory=dict)
    # pool -> (observation time the entry was computed at, result)
    _cache: dict[str, tuple[float, CostEnvelopeSnapshot | None]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def observe(self, pool: str, cost_usd: float, *, now: float | None = None) -> None:
        """Add one cheapest API-equivalent cost sample."""
        if cost_usd <= 0:
            return
        ts = time.time() if now is None else now
        with self._lock:
            samples = self._samples.get(pool)
            if samples is None:
                maxlen = int(self.max_samples) if self.max_samples > 0 else None
                samples = self._samples[pool] = deque(maxlen=maxlen)
            samples.append((ts, float(cost_usd)))
            self._prune_locked(pool, ts)
            # Holding a cached "uncalibrated" answer across a new sample would
            # delay a cold pool's first calibration by the whole TTL; holding a
            # calibrated one is exactly the staleness the TTL is there to allow.
            cached = self._cache.get(pool)
            if cached is not None and cached[1] is None:
                del self._cache[pool]

    def snapshot(self, pool: str, *, now: float | None = None) -> CostEnvelopeSnapshot | None:
        """Return current ``L/U`` for *pool*, or ``None`` if uncalibrated."""
        ts = time.time() if now is None else now
        ttl = float(self.cache_ttl_sec)
        with self._lock:
            if ttl > 0.0:
                cached = self._cache.get(pool)
                # A caller that moves its clock backwards (tests replaying a
                # fixed timeline) must not read an entry computed ahead of it.
                if cached is not None and 0.0 <= ts - cached[0] < ttl:
                    return cached[1]
            self._prune_locked(pool, ts)
            values = [cost for _t, cost in self._samples.get(pool, ())]
            result = self._envelope_for(values)
            if ttl > 0.0:
                self._cache[pool] = (ts, result)
        return result

    def _envelope_for(self, values: list[float]) -> CostEnvelopeSnapshot | None:
        """Return the envelope for one window's costs, sorting them once."""
        if len(values) < max(int(self.min_samples), 1):
            return None
        ordered = sorted(values)
        upper = _percentile_from_sorted(ordered, self.upper_percentile)
        if upper <= 0:
            return None
        lower = _percentile_from_sorted(ordered, self.lower_percentile)
        if lower <= 0 or lower >= upper:
            # Paper floor fallback: keep 0 < L < U with a bounded U/L ratio so
            # the quota shadow-price curve neither flattens (L == U) nor turns
            # into a near-step function (L ~ 0).
            lower = upper * 1e-3
        return CostEnvelopeSnapshot(
            lower=lower,
            upper=upper,
            sample_count=len(ordered),
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
