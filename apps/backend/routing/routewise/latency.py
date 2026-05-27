"""Real-time latency profiling for Layer 2.

This module provides:
- ``ProviderProfile``: time-windowed latency and error tracking per endpoint
  with empirical CDF computation (INFINITY failure mode).

Reference: experiment/strategies/online_latency_router.py.
"""

from __future__ import annotations

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

    def mean_with_errors_sec(
        self,
        current_time: float,
        *,
        error_penalty_ms: float,
    ) -> float | None:
        """Return success mean with failed attempts as synthetic penalty samples."""
        self._prune(current_time)
        samples_ms = [v for _, v in self._samples]
        error_count = sum(1 for _, e in self._events if e is not None)
        total = len(samples_ms) + error_count
        if total == 0:
            return None
        return (sum(samples_ms) + error_count * float(error_penalty_ms)) / total / 1000.0

    def sample_count(self, current_time: float) -> int:
        """Return number of latency samples in the current window.

        Args:
            current_time: Reference time for window pruning.

        Returns:
            Count of successful latency samples within the window.
        """
        cutoff = current_time - self.window_sec
        return sum(1 for t, _ in self._samples if t >= cutoff)

    def total_count(self, current_time: float) -> int:
        """Return successful latency samples plus failed attempts in the window."""
        self._prune(current_time)
        successes = len(self._samples)
        errors = sum(1 for _, e in self._events if e is not None)
        return successes + errors

    def _prune(self, current_time: float) -> None:
        """Remove samples outside the time window."""
        cutoff = current_time - self.window_sec
        self._samples = [(t, v) for t, v in self._samples if t >= cutoff]
        self._events = [(t, e) for t, e in self._events if t >= cutoff]

    def _get_latency_samples_sec(self, current_time: float) -> list[float]:
        """Get latency samples in seconds within the current window."""
        cutoff = current_time - self.window_sec
        return [v / 1000.0 for t, v in self._samples if t >= cutoff]
