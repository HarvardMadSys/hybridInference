"""Real-time latency profiling for the RouteWise latency layer.

This module provides:
- ``ProviderProfile``: time-windowed latency and error tracking per endpoint
  with empirical CDF computation (errors count as missed deadlines).

Every summary is answered from state kept as outcomes enter and leave the
window rather than from a scan of it. ``_latency_estimate`` asks each candidate
endpoint for its mean on every routing decision, and the hedge planner asks for
CDF values at every checkpoint of every slow request, on the event loop and
under the router's commit lock: a scan there costs ``O(window samples)`` per
question, with up to 5000 samples per endpoint.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass, field

# A prefill model (TTFT = a + b * prompt size) is fitted only once this many
# timed successes with a known prompt size are in the window...
PREFILL_FIT_MIN_SAMPLES = 30
# ...and their prompt sizes spread this much (standard deviation, thousands of
# tokens). With prompts all of a size there is no slope to see, and none to
# need: a request like them is answered like them.
PREFILL_FIT_MIN_SPREAD_KTOK = 0.25

# (timestamp, ttft_ms, error_type | None, prompt_tokens)
Event = tuple[float, float, str | None, int]


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

    # Request outcomes: (timestamp, ttft_ms, error_type | None, prompt_tokens),
    # prompt_tokens 0 when unknown.
    _events: deque[Event] = field(init=False, repr=False)
    # Running aggregates over exactly the outcomes currently in ``_events``.
    # A "timed success" (no error, positive TTFT) feeds the count and the sum;
    # an error feeds the error count. An "untimed success" -- a success with no
    # TTFT to give, such as a non-streaming response, whose wall time is mostly
    # the generation the caller asked for -- feeds only its own count: it is
    # evidence that the endpoint answers, so it keeps the error share honest,
    # but it says nothing about how fast the endpoint starts answering.
    _success_count: int = field(default=0, init=False, repr=False)
    _success_sum_ms: float = field(default=0.0, init=False, repr=False)
    _untimed_success_count: int = field(default=0, init=False, repr=False)
    _error_count: int = field(default=0, init=False, repr=False)
    # The timed successes' TTFTs, in seconds and sorted, so the CDF's count of
    # samples within a threshold is a bisection. Kept in the unit the CDF
    # compares in, computed by the same division, so the answer is exactly the
    # scan's: ``ttft_ms / 1000.0 <= threshold_sec``.
    _sorted_ttft_sec: list[float] = field(init=False, repr=False)
    # Least-squares sums for TTFT against prompt size, over the timed successes
    # whose prompt size is known: prompt in thousands of tokens (k), TTFT in
    # seconds (t). See ``expected_extra_ttft_sec``.
    _fit_n: int = field(default=0, init=False, repr=False)
    _fit_sum_k: float = field(default=0.0, init=False, repr=False)
    _fit_sum_kk: float = field(default=0.0, init=False, repr=False)
    _fit_sum_t: float = field(default=0.0, init=False, repr=False)
    _fit_sum_kt: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_samples = max(int(self.max_samples), 1)
        self._events = deque(maxlen=self.max_samples)
        self._sorted_ttft_sec = []

    def _add_to_aggregates(self, event: Event) -> None:
        _ts, ttft_ms, error_type, prompt_tokens = event
        if error_type is not None:
            self._error_count += 1
        elif ttft_ms > 0:
            self._success_count += 1
            self._success_sum_ms += ttft_ms
            bisect.insort(self._sorted_ttft_sec, ttft_ms / 1000.0)
            if prompt_tokens > 0:
                k, t = prompt_tokens / 1000.0, ttft_ms / 1000.0
                self._fit_n += 1
                self._fit_sum_k += k
                self._fit_sum_kk += k * k
                self._fit_sum_t += t
                self._fit_sum_kt += k * t
        else:
            self._untimed_success_count += 1

    def _remove_from_aggregates(self, event: Event) -> None:
        _ts, ttft_ms, error_type, prompt_tokens = event
        if error_type is not None:
            self._error_count -= 1
        elif ttft_ms > 0:
            self._success_count -= 1
            self._success_sum_ms -= ttft_ms
            sorted_ttft = self._sorted_ttft_sec
            # Equal values are interchangeable, so removing the first one is
            # removing this one.
            index = bisect.bisect_left(sorted_ttft, ttft_ms / 1000.0)
            if index < len(sorted_ttft):
                del sorted_ttft[index]
            if prompt_tokens > 0:
                k, t = prompt_tokens / 1000.0, ttft_ms / 1000.0
                self._fit_n -= 1
                self._fit_sum_k -= k
                self._fit_sum_kk -= k * k
                self._fit_sum_t -= t
                self._fit_sum_kt -= k * t
        else:
            self._untimed_success_count -= 1
        # Re-anchor on empty so repeated add/subtract cycles cannot accumulate
        # floating-point drift in the running sums.
        if self._success_count <= 0:
            self._success_count = 0
            self._success_sum_ms = 0.0
            self._sorted_ttft_sec.clear()
        if self._fit_n <= 0:
            self._fit_n = 0
            self._fit_sum_k = self._fit_sum_kk = self._fit_sum_t = self._fit_sum_kt = 0.0

    def record(
        self,
        timestamp: float,
        ttft_ms: float,
        error_type: str | None = None,
        prompt_tokens: int = 0,
    ) -> None:
        """Record a request outcome.

        Args:
            timestamp: Unix timestamp of the request.
            ttft_ms: Time to first token in milliseconds.  Non-positive values
                are treated as missing TTFT: a success without one is recorded
                as an untimed success.
            error_type: None for success, or error type string.
            prompt_tokens: The request's prompt size, or 0 when unknown. A
                timed success with one feeds the prefill model.
        """
        events = self._events
        # ``append`` on a full deque silently drops the oldest outcome, so the
        # aggregates have to let go of it first.
        if events.maxlen is not None and len(events) == events.maxlen:
            self._remove_from_aggregates(events[0])
        event = (timestamp, ttft_ms, error_type, max(int(prompt_tokens or 0), 0))
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
        success_within = bisect.bisect_right(self._sorted_ttft_sec, threshold_sec)

        # Only timed successes describe the latency distribution, so only they
        # are F_success's denominator. Every outcome -- untimed successes too --
        # describes how often the endpoint answers at all.
        f_success = success_within / success_count
        outcomes = success_count + self._untimed_success_count + error_count
        success_rate = 1.0 - (error_count / outcomes)
        return success_rate * f_success

    def expected_extra_ttft_sec(self, prompt_tokens: int, current_time: float) -> float:
        """Return how much later than usual this endpoint should answer a prompt this long.

        Prefill grows with the prompt, so a request far longer than the ones in
        this window starts answering later than the window's TTFTs say -- on
        whichever endpoint it is sent to. Fitted as TTFT = a + b * prompt over
        the window's timed successes of known prompt size, the answer is
        ``b * (prompt - mean prompt)`` for a prompt longer than the mean, and 0
        for any other, for a slope that is not positive, and while the window
        holds too few such samples, or ones too alike in size, to fit one.
        """
        self._prune(current_time)
        n = self._fit_n
        if prompt_tokens <= 0 or n < PREFILL_FIT_MIN_SAMPLES:
            return 0.0
        mean_k = self._fit_sum_k / n
        excess_k = prompt_tokens / 1000.0 - mean_k
        if excess_k <= 0.0:
            return 0.0
        variance = self._fit_sum_kk / n - mean_k * mean_k
        if not variance >= PREFILL_FIT_MIN_SPREAD_KTOK**2:
            return 0.0
        slope = (self._fit_sum_kt / n - mean_k * (self._fit_sum_t / n)) / variance
        if not (slope > 0.0 and math.isfinite(slope)):
            return 0.0
        return slope * excess_k

    def mean_with_errors_sec(
        self,
        current_time: float,
        *,
        error_penalty_ms: float,
        untimed_success_ms: float | None = None,
    ) -> float | None:
        """Return success mean with failed attempts as synthetic penalty samples.

        An untimed success weighs in at the timed successes' mean -- it is
        assumed to start answering like the requests that were timed -- so it
        dilutes the error penalty without moving the latency estimate. With no
        timed success in the window it weighs in at ``untimed_success_ms`` (the
        caller's prior), or is left out when the caller gives none.
        """
        self._prune(current_time)
        timed = self._success_count
        errors = self._error_count
        untimed = self._untimed_success_count
        if timed:
            success_mean_ms = self._success_sum_ms / timed
        elif untimed and untimed_success_ms is not None:
            success_mean_ms = float(untimed_success_ms)
        else:
            untimed = 0
            success_mean_ms = 0.0
        if not untimed:
            total = timed + errors
            if total == 0:
                return None
            penalized = self._success_sum_ms + errors * float(error_penalty_ms)
            return penalized / total / 1000.0
        successes = timed + untimed
        penalized = successes * success_mean_ms + errors * float(error_penalty_ms)
        return penalized / (successes + errors) / 1000.0

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
        """Return successful latency samples plus failed attempts in the window.

        Untimed successes are not counted: alone they carry no latency evidence.
        """
        self._prune(current_time)
        return self._success_count + self._error_count

    def untimed_success_count(self, current_time: float) -> int:
        """Return successes recorded without a TTFT in the current window."""
        self._prune(current_time)
        return self._untimed_success_count

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
