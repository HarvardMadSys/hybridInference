"""Real-time latency profiling for the RouteWise latency layer.

This module provides:
- ``ProviderProfile``: time-windowed latency and error tracking per endpoint
  with empirical CDF computation (errors count as missed deadlines).

Every summary except the threshold-dependent CDF is answered from running
aggregates rather than a scan of the window. ``_latency_estimate`` asks each
candidate endpoint for its mean on every routing decision, so a scan there
costs ``O(candidates * window samples)`` per request.
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
    # Running aggregates over exactly the outcomes currently in ``_events``.
    # A "timed success" (no error, positive TTFT) feeds the count and the sum;
    # an error feeds the error count; a success recorded without a usable TTFT
    # is in neither, which is what the scan-based implementation counted too.
    _success_count: int = field(default=0, init=False, repr=False)
    _success_sum_ms: float = field(default=0.0, init=False, repr=False)
    _error_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_samples = max(int(self.max_samples), 1)
        self._events = deque(maxlen=self.max_samples)

    def _add_to_aggregates(self, event: tuple[float, float, str | None]) -> None:
        _ts, ttft_ms, error_type = event
        if error_type is not None:
            self._error_count += 1
        elif ttft_ms > 0:
            self._success_count += 1
            self._success_sum_ms += ttft_ms

    def _remove_from_aggregates(self, event: tuple[float, float, str | None]) -> None:
        _ts, ttft_ms, error_type = event
        if error_type is not None:
            self._error_count -= 1
        elif ttft_ms > 0:
            self._success_count -= 1
            self._success_sum_ms -= ttft_ms
        # Re-anchor on empty so repeated add/subtract cycles cannot accumulate
        # floating-point drift in the running sum.
        if self._success_count <= 0:
            self._success_count = 0
            self._success_sum_ms = 0.0

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
        events = self._events
        # ``append`` on a full deque silently drops the oldest outcome, so the
        # aggregates have to let go of it first.
        if events.maxlen is not None and len(events) == events.maxlen:
            self._remove_from_aggregates(events[0])
        event = (timestamp, ttft_ms, error_type)
        events.append(event)
        self._add_to_aggregates(event)

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
        success_count = self._success_count
        if success_count == 0:
            return 0.0
        error_count = self._error_count
        # Only the share below the threshold depends on the threshold, so this
        # is the one summary that still has to look at the samples themselves.
        success_within = sum(
            1
            for _ts, ttft_ms, error_type in self._events
            if error_type is None and ttft_ms > 0 and ttft_ms / 1000.0 <= threshold_sec
        )

        f_success = success_within / success_count
        # Denominator counts real outcomes (timed successes + errors); a success
        # recorded with a non-positive TTFT is not a real outcome and must not
        # inflate it, which would otherwise deflate the CDF.
        success_rate = 1.0 - (error_count / (success_count + error_count))
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
        return self._error_count / len(self._events)

    def mean_with_errors_sec(
        self,
        current_time: float,
        *,
        error_penalty_ms: float,
    ) -> float | None:
        """Return success mean with failed attempts as synthetic penalty samples."""
        self._prune(current_time)
        total = self._success_count + self._error_count
        if total == 0:
            return None
        penalized = self._success_sum_ms + self._error_count * float(error_penalty_ms)
        return penalized / total / 1000.0

    def mean_ttft_sec(self, current_time: float) -> float:
        """Return mean successful TTFT in seconds within the current window."""
        self._prune(current_time)
        if self._success_count == 0:
            return float("inf")
        return self._success_sum_ms / self._success_count / 1000.0

    def sample_count(self, current_time: float) -> int:
        """Return number of latency samples in the current window.

        Args:
            current_time: Reference time for window pruning.

        Returns:
            Count of successful latency samples within the window.
        """
        self._prune(current_time)
        return self._success_count

    def total_count(self, current_time: float) -> int:
        """Return successful latency samples plus failed attempts in the window."""
        self._prune(current_time)
        return self._success_count + self._error_count

    def last_event_time(self, current_time: float) -> float | None:
        """Return the newest retained event timestamp, or None when empty."""
        self._prune(current_time)
        if not self._events:
            return None
        return self._events[-1][0]

    def _prune(self, current_time: float) -> None:
        """Remove samples outside the time window."""
        cutoff = current_time - self.window_sec
        # Outcomes are appended in observation-time order.  Bootstrap callers
        # should replay history oldest-to-newest so this remains O(evicted).
        events = self._events
        while events and events[0][0] < cutoff:
            self._remove_from_aggregates(events.popleft())
