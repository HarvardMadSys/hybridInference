"""Real-time latency profiling for Layer 2.

This module provides:
- ``ProviderProfile``: time-windowed latency and error tracking per endpoint
  with empirical CDF computation (INFINITY failure mode).

Reference: experiment/strategies/online_latency_router.py.
"""

from __future__ import annotations

from collections import deque
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
        max_samples: Maximum request outcomes retained per endpoint.
    """

    endpoint_id: str
    window_sec: float = 900.0  # 15 minutes
    max_samples: int = 5000

    # Request outcomes: (timestamp, ttft_ms, error_type | None).
    _events: deque[tuple[float, float, str | None]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_samples = max(int(self.max_samples), 1)
        self._events = deque(maxlen=self.max_samples)

    def record(
        self,
        timestamp: float,
        ttft_ms: float,
        error_type: str | None = None,
    ) -> None:
        """Record a request outcome.

        Args:
            timestamp: Unix timestamp of the request.
            ttft_ms: Time to first token in milliseconds.  Non-positive values
                are treated as missing TTFT.
            error_type: None for success, or error type string.
        """
        self._events.append((timestamp, ttft_ms, error_type))

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
        success_count = 0
        success_within = 0
        error_count = 0
        for _, ttft_ms, error_type in self._events:
            if error_type is not None:
                error_count += 1
            elif ttft_ms > 0:
                success_count += 1
                if ttft_ms / 1000.0 <= threshold_sec:
                    success_within += 1

        if success_count == 0:
            return 0.0

        f_success = success_within / success_count
        success_rate = 1.0 - (error_count / len(self._events))
        return success_rate * f_success

    def error_rate(self, current_time: float) -> float:
        """Compute error rate within the current window.

        Args:
            current_time: Reference time for window pruning.

        Returns:
            Fraction of failed requests in [0, 1].  Returns 0.0 if no events.
        """
        self._prune(current_time)
        if not self._events:
            return 0.0
        error_count = sum(1 for _, _ttft, e in self._events if e is not None)
        return error_count / len(self._events)

    def mean_with_errors_sec(
        self,
        current_time: float,
        *,
        error_penalty_ms: float,
    ) -> float | None:
        """Return success mean with failed attempts as synthetic penalty samples."""
        self._prune(current_time)
        samples_ms = [ttft for _, ttft, e in self._events if e is None and ttft > 0]
        error_count = sum(1 for _, _ttft, e in self._events if e is not None)
        total = len(samples_ms) + error_count
        if total == 0:
            return None
        return (sum(samples_ms) + error_count * float(error_penalty_ms)) / total / 1000.0

    def mean_ttft_sec(self, current_time: float) -> float:
        """Return mean successful TTFT in seconds within the current window."""
        self._prune(current_time)
        samples_ms = [ttft for _, ttft, e in self._events if e is None and ttft > 0]
        if not samples_ms:
            return float("inf")
        return sum(samples_ms) / len(samples_ms) / 1000.0

    def sample_count(self, current_time: float) -> int:
        """Return number of latency samples in the current window.

        Args:
            current_time: Reference time for window pruning.

        Returns:
            Count of successful latency samples within the window.
        """
        self._prune(current_time)
        return sum(1 for _, ttft, e in self._events if e is None and ttft > 0)

    def total_count(self, current_time: float) -> int:
        """Return successful latency samples plus failed attempts in the window."""
        self._prune(current_time)
        successes = sum(1 for _, ttft, e in self._events if e is None and ttft > 0)
        errors = sum(1 for _, _ttft, e in self._events if e is not None)
        return successes + errors

    def _prune(self, current_time: float) -> None:
        """Remove samples outside the time window."""
        cutoff = current_time - self.window_sec
        # Outcomes are appended in observation-time order.  Bootstrap callers
        # should replay history oldest-to-newest so this remains O(evicted).
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
