"""Real-time latency profiling and SWRR sampling for Layer 2.

This module provides:
- ``ProviderProfile``: time-windowed latency and error tracking per endpoint
  with empirical CDF computation (INFINITY failure mode).
- ``SWRRSampler``: smooth weighted round-robin with exponential smoothing
  for LP weight updates.
- ``ShadowHedgeDecision``: record type for shadow hedge computation
  (no actual dispatch in shadow mode).

Reference: experiment/strategies/online_latency_router.py (lines 40-196, 373-455).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class ProviderProfile:
    """Real-time latency profile for an API provider endpoint.

    Maintains a single time-based moving window for latency samples and error
    tracking.  CDF computation uses INFINITY mode: errors count as latency =
    infinity (missed deadline), so ``F(L) = success_rate * F_success(L)``.

    Attributes:
        endpoint_id: Unique identifier for the provider endpoint.
        window_sec: Moving window duration in seconds (default 15 min).
    """

    endpoint_id: str
    window_sec: float = 900.0  # 15 minutes

    # Latency samples: list of (timestamp, ttft_ms).
    # Only successful requests with positive TTFT are stored.
    _samples: list[tuple[float, float]] = field(default_factory=list)

    # Event tracking: list of (timestamp, error_type | None).
    # error_type: None for success, or string like "timeout", "rate_limit", etc.
    _events: list[tuple[float, str | None]] = field(default_factory=list)

    def record(
        self,
        timestamp: float,
        ttft_ms: float,
        error_type: str | None = None,
    ) -> None:
        """Record a request outcome.

        Args:
            timestamp: Unix timestamp of the request.
            ttft_ms: Time to first token in milliseconds (-1 if error).
            error_type: None for success, or error type string.
        """
        self._events.append((timestamp, error_type))
        if error_type is None and ttft_ms > 0:
            self._samples.append((timestamp, ttft_ms))

    def cdf_at(self, threshold_sec: float, current_time: float) -> float:
        """Compute empirical CDF at latency threshold L (INFINITY mode).

        F(L) = success_rate * F_success(L)

        Where F_success(L) is the fraction of successful requests with
        latency <= L, and success_rate accounts for errors as infinite
        latency misses.

        Args:
            threshold_sec: Latency threshold in seconds.
            current_time: Reference time for window pruning.

        Returns:
            CDF value in [0, 1].  Returns 0.0 if no samples.
        """
        self._prune(current_time)

        samples_sec = self._get_latency_samples_sec(current_time)
        if not samples_sec:
            return 0.0

        f_success = sum(1 for s in samples_sec if s <= threshold_sec) / len(samples_sec)
        success_rate = 1.0 - self.error_rate(current_time)
        return success_rate * f_success

    def error_rate(self, current_time: float) -> float:
        """Compute error rate within the current window.

        Args:
            current_time: Reference time for window pruning.

        Returns:
            Fraction of failed requests in [0, 1].  Returns 0.0 if no events.
        """
        cutoff = current_time - self.window_sec
        events = [(t, e) for t, e in self._events if t >= cutoff]
        if not events:
            return 0.0
        error_count = sum(1 for _, e in events if e is not None)
        return error_count / len(events)

    def percentile(self, q: float, current_time: float) -> float:
        """Compute the q-th percentile of latency in seconds.

        Args:
            q: Percentile in [0, 100] (e.g. 50 for median, 99 for p99).
            current_time: Reference time for window pruning.

        Returns:
            Latency in seconds at the q-th percentile.
            Returns inf if no samples.
        """
        samples_sec = self._get_latency_samples_sec(current_time)
        if not samples_sec:
            return float("inf")
        samples_sec.sort()
        idx = (q / 100.0) * (len(samples_sec) - 1)
        lower = int(math.floor(idx))
        upper = min(lower + 1, len(samples_sec) - 1)
        frac = idx - lower
        return samples_sec[lower] * (1.0 - frac) + samples_sec[upper] * frac

    def sample_count(self, current_time: float) -> int:
        """Return number of latency samples in the current window.

        Args:
            current_time: Reference time for window pruning.

        Returns:
            Count of successful latency samples within the window.
        """
        cutoff = current_time - self.window_sec
        return sum(1 for t, _ in self._samples if t >= cutoff)

    def _prune(self, current_time: float) -> None:
        """Remove samples outside the time window."""
        cutoff = current_time - self.window_sec
        self._samples = [(t, v) for t, v in self._samples if t >= cutoff]
        self._events = [(t, e) for t, e in self._events if t >= cutoff]

    def _get_latency_samples_sec(self, current_time: float) -> list[float]:
        """Get latency samples in seconds within the current window."""
        cutoff = current_time - self.window_sec
        return [v / 1000.0 for t, v in self._samples if t >= cutoff]


class SWRRSampler:
    """Smooth Weighted Round-Robin with exponential smoothing.

    Given weights pi = {A: 0.7, B: 0.3}, produces a smooth interleaving:
    A, A, B, A, A, B, A, A, A, B, ...

    Equivalent to probabilistic mixing but with reduced short-term variance.
    Weight updates use exponential smoothing: w = (1-alpha)*old + alpha*new.
    """

    def __init__(self, alpha: float = 0.3) -> None:
        """Initialize SWRR sampler.

        Args:
            alpha: Smoothing factor for weight updates (0 = keep old, 1 = use new).
        """
        self._alpha = alpha
        self._providers: list[str] = []
        self._weights: dict[str, float] = {}
        self._current_weights: dict[str, float] = {}

    def update_weights(self, new_weights: dict[str, float]) -> None:
        """Update target weights with exponential smoothing.

        w_new = (1 - alpha) * w_old + alpha * w_lp

        Providers with weight < 0.001 after smoothing are removed.
        Weights are normalized to sum to 1.

        Args:
            new_weights: New weights from LP solver (should sum to ~1).
        """
        all_providers = set(self._providers) | set(new_weights.keys())
        smoothed: dict[str, float] = {}

        for p in all_providers:
            old_w = self._weights.get(p, 0.0)
            new_w = new_weights.get(p, 0.0)
            smoothed[p] = (1.0 - self._alpha) * old_w + self._alpha * new_w

        # Remove negligible-weight providers.
        self._weights = {p: w for p, w in smoothed.items() if w > 0.001}
        self._providers = list(self._weights.keys())

        # Normalize.
        total = sum(self._weights.values())
        if total > 0:
            self._weights = {p: w / total for p, w in self._weights.items()}

        # Soft reset current weights for existing providers, init new ones.
        new_current: dict[str, float] = {}
        for p in self._providers:
            if p in self._current_weights:
                new_current[p] = self._current_weights[p] * 0.5
            else:
                new_current[p] = 0.0
        self._current_weights = new_current

    def sample(self) -> str | None:
        """Select next provider using SWRR algorithm.

        Add target weights to current weights, pick the provider with
        highest current weight, then subtract total weight from the selected.

        Returns:
            Selected provider name, or None if no providers.
        """
        if not self._providers:
            return None

        # Add target weights.
        for p in self._providers:
            self._current_weights[p] = self._current_weights.get(p, 0.0) + self._weights[p]

        # Pick max.
        selected = max(self._providers, key=lambda p: self._current_weights[p])

        # Subtract total weight.
        total_weight = sum(self._weights.values())
        self._current_weights[selected] -= total_weight

        return selected

    def get_weights(self) -> dict[str, float]:
        """Return current target weights (copy)."""
        return self._weights.copy()


@dataclass
class ShadowHedgeDecision:
    """Record of a shadow hedge computation (no actual dispatch).

    Shadow mode logs hedge decisions for analysis without performing
    actual hedged requests.

    Attributes:
        model_id: Model that triggered this decision.
        primary_endpoint: Endpoint selected by LP/SWRR.
        backup_endpoint: Second-best endpoint (or None if single provider).
        hedge_threshold_sec: Computed threshold for when hedge would trigger.
        reason: One of "no_backup", "backup_slower", "hedge_warranted".
        timestamp: Unix timestamp of the decision.
    """

    model_id: str
    primary_endpoint: str
    backup_endpoint: str | None
    hedge_threshold_sec: float | None
    reason: str  # "no_backup" | "backup_slower" | "hedge_warranted"
    timestamp: float
