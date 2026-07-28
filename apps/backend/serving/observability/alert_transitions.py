"""Edge detection that gives the gateway's threshold alerts a resolved state.

Every ``alert_slack`` call site today fires only: a rule computes its metric,
returns silently while the metric is healthy, and posts while it is breached,
relying on a cooldown to suppress repeats. That is workable for one-shot chat
messages but not for the alert control plane, whose incidents open on ``firing``
and close on ``resolved`` — a fire-only producer would open incidents that never
close, hold principal quota until it is exhausted, and then have real outages
suppressed.

This module supplies the missing half. Rules keep computing "is the metric
breached right now?" and hand that to :meth:`ThresholdTransitionTracker.observe`,
which returns a transition only on an edge. Two problems that a naive
``breached != was_breached`` check gets wrong are handled here:

*Flapping.* A metric sitting on its threshold would otherwise emit an endless
firing/resolved pair per evaluation. ``clear_after_sec`` requires the metric to
stay healthy for a settling period before the incident closes; any breach during
that period cancels the pending close without re-firing.

*Silence.* Rules are driven by request records, so a breach followed by traffic
stopping altogether is never re-evaluated and the incident would stay open
forever. :meth:`sweep` closes incidents whose last observation is older than
``stale_after_sec``, which the caller runs on a timer.

Both behaviours suit a *metric* — a quantity recomputed from a rolling window of
traffic. They are wrong for a *state* alert such as an open circuit breaker or a
disconnected store, which reports a condition the process already tracks: there
is exactly one healthy edge ever, so waiting for a settling period means the
incident never closes, and silence is not evidence of recovery — sweeping one
would report the outage as over while it is still happening. Construct those
with ``clear_after_sec=0`` and ``stale_after_sec=None``.

The tracker is deliberately in-process, matching where the existing cooldown
state lives. Cross-process convergence is the control plane's job: it dedupes by
fingerprint, so two workers observing the same edge produce one incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Transition = Literal["firing", "resolved"]

# A breach must stay clear this long before its incident closes. Chosen to
# exceed a single evaluation interval so a metric oscillating around its
# threshold settles instead of emitting a transition pair per sample.
DEFAULT_CLEAR_AFTER_SEC = 120.0

# A firing key not observed for this long is closed by ``sweep``. Must exceed
# the rule's own window so an idle-but-healthy period is not mistaken for one
# where the rule simply had nothing to evaluate.
DEFAULT_STALE_AFTER_SEC = 900.0


@dataclass
class _KeyState:
    """Per-fingerprint firing state and the clocks that close it."""

    last_observed_at: float
    #: When the metric was first seen healthy after the breach; ``None`` while
    #: it is still breached. Reset by any breach so a flap cannot close early.
    clear_started_at: float | None = None


@dataclass
class ThresholdTransitionTracker:
    """Turn repeated threshold evaluations into ``firing``/``resolved`` edges.

    Args:
        clear_after_sec: How long a breach must stay clear before resolving.
            ``0`` resolves on the first healthy observation.
        stale_after_sec: How long a firing key may go unobserved before
            :meth:`sweep` resolves it. ``None`` disables staleness closing,
            which is only correct when the caller evaluates on a timer rather
            than on traffic.
    """

    clear_after_sec: float = DEFAULT_CLEAR_AFTER_SEC
    stale_after_sec: float | None = DEFAULT_STALE_AFTER_SEC
    _firing: dict[str, _KeyState] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.clear_after_sec < 0:
            raise ValueError("clear_after_sec must not be negative")
        if self.stale_after_sec is not None and self.stale_after_sec <= 0:
            raise ValueError("stale_after_sec must be positive when set")

    def is_firing(self, key: str) -> bool:
        """Whether ``key`` currently has an open incident."""
        return key in self._firing

    def observe(self, key: str, *, breached: bool, now: float) -> Transition | None:
        """Record one evaluation and return the edge it crossed, if any.

        Returns ``"firing"`` the first time a healthy key breaches,
        ``"resolved"`` once a firing key has been clear for ``clear_after_sec``,
        and ``None`` for every evaluation that does not change the state — which
        is the overwhelming majority, so callers can send only on a transition.
        """
        state = self._firing.get(key)

        if breached:
            if state is None:
                self._firing[key] = _KeyState(last_observed_at=now)
                return "firing"
            # Still breached: refresh liveness and cancel any pending close, so
            # a metric that dips below its threshold and comes back does not
            # resolve and immediately re-fire.
            state.last_observed_at = now
            state.clear_started_at = None
            return None

        if state is None:
            # Healthy and no incident open: the common case, and the one the
            # rules used to express by returning silently.
            return None

        state.last_observed_at = now
        if state.clear_started_at is None:
            state.clear_started_at = now
        if now - state.clear_started_at >= self.clear_after_sec:
            del self._firing[key]
            return "resolved"
        return None

    def sweep(self, now: float) -> list[str]:
        """Resolve firing keys whose rule has stopped producing evaluations.

        Rules are driven by request records, so a breach followed by silence —
        traffic stopping, the process draining — leaves an incident that
        ``observe`` will never be called to close. Returns the keys closed, for
        the caller to emit resolutions for.
        """
        if self.stale_after_sec is None:
            return []
        stale = [
            key
            for key, state in self._firing.items()
            if now - state.last_observed_at >= self.stale_after_sec
        ]
        for key in stale:
            del self._firing[key]
        return sorted(stale)

    def rearm(self, key: str, now: float) -> None:
        """Put a resolved key back into firing after its resolution was not sent.

        ``observe`` and ``sweep`` clear the key before the caller has a delivery
        result, so a transient sink failure would otherwise lose the only
        resolution that key will ever produce and leave its incident open with
        nothing able to close it. Re-arming makes the next healthy observation
        try again; a discrete state alert with no further observations stays
        open, which is where it was before this module existed.
        """
        self._firing[key] = _KeyState(last_observed_at=now, clear_started_at=None)

    def forget(self, key: str) -> None:
        """Drop state without emitting a transition (for shutdown or reload)."""
        self._firing.pop(key, None)
