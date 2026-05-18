"""RouteWise workload cost-envelope estimator.

``L`` and ``U`` are request-cost scale estimates used by the quota shadow
price curve.  They are calibrated on the same cold-cache route-time cost
assumption used by the first production RouteWise body router.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


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
    source: str


@dataclass
class CostEnvelopeEstimator:
    """Sliding-window percentile estimator for workload request costs."""

    lower_seed: float = 0.001
    upper_seed: float = 0.500
    lower_percentile: float = 10.0
    upper_percentile: float = 90.0
    window_sec: float = 24 * 3600.0
    min_samples: int = 20
    min_ratio: float = 10.0
    _samples: dict[str, deque[tuple[float, float]]] = field(
        default_factory=lambda: defaultdict(deque)
    )

    def observe(self, pool: str, cost_usd: float, *, now: float | None = None) -> None:
        """Add one cheapest API-equivalent cost sample."""
        if cost_usd <= 0:
            return
        ts = time.time() if now is None else now
        samples = self._samples[pool]
        samples.append((ts, float(cost_usd)))
        self._prune(pool, ts)

    def snapshot(self, pool: str, *, now: float | None = None) -> CostEnvelopeSnapshot:
        """Return current ``L/U`` for *pool*."""
        ts = time.time() if now is None else now
        self._prune(pool, ts)
        values = [cost for _t, cost in self._samples.get(pool, ())]
        if len(values) < self.min_samples:
            return CostEnvelopeSnapshot(
                lower=max(float(self.lower_seed), 1e-12),
                upper=max(float(self.upper_seed), float(self.lower_seed), 1e-12),
                sample_count=len(values),
                source="seed",
            )
        lower = max(_percentile(values, self.lower_percentile), 1e-12)
        upper = max(_percentile(values, self.upper_percentile), lower)
        if self.min_ratio > 1.0:
            upper = max(upper, lower * self.min_ratio)
        return CostEnvelopeSnapshot(
            lower=lower,
            upper=upper,
            sample_count=len(values),
            source="observed",
        )

    def _prune(self, pool: str, now: float) -> None:
        samples = self._samples.get(pool)
        if not samples:
            return
        cutoff = now - self.window_sec
        while samples and samples[0][0] < cutoff:
            samples.popleft()
