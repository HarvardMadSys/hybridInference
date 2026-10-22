"""Bounded traffic observation state for traffic classification.

Tracks minimal temporal evidence needed by the traffic classifier:
- response completion and next request timestamps (for idle-time cadence)
- bounded request-shape timestamps in a short sliding window

Design:
- Bounded cardinality (max entries per identity, max total entries)
- TTL-based lazy eviction
- No persistent profiling
- Stable identity grouping (authenticated user identity only)
- Bystander isolation (anonymous callers are not grouped)
"""

from __future__ import annotations

import hashlib
import math
import time
from bisect import bisect_left, insort
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_SHAPE_REPEAT_WINDOW_SECONDS = 600.0
_MAX_SHAPE_OBSERVATIONS = 100
_MAX_IN_FLIGHT_REQUESTS_PER_IDENTITY = 128


@dataclass
class _IdentityEntry:
    """Observation state for one identity."""

    last_request_ts: float | None = None
    last_response_completed_ts: float | None = None
    in_flight_requests: dict[int, float] = field(default_factory=dict)
    in_flight_latest_completion_ts: float | None = None
    response_timing_disabled: bool = False
    request_count: int = 0
    shape_counts: OrderedDict[str, list[float]] = field(default_factory=OrderedDict)
    session_id: str | None = None


class TrafficObservationState:
    """Bounded tracker for traffic observation evidence.

    Tracks per-identity temporal evidence needed by the classifier. Cadence is
    measured from a completed response to the next request; start-to-start time
    is not used because it includes model serving latency. Shape repetition is
    limited to a short sliding window.
    Uses LRU eviction when capacity is reached.

    Thread-safety: asyncio single-threaded; no internal lock needed.
    """

    def __init__(
        self,
        max_identities: int = 2_000,
        max_shapes_per_identity: int = 32,
        ttl_seconds: float = 3600.0,  # 1 hour
        clock: Callable[[], float] | None = None,
    ):
        if not isinstance(max_identities, int) or isinstance(max_identities, bool):
            raise TypeError("max_identities must be a positive integer")
        if max_identities <= 0:
            raise ValueError("max_identities must be a positive integer")
        if not isinstance(max_shapes_per_identity, int) or isinstance(
            max_shapes_per_identity, bool
        ):
            raise TypeError("max_shapes_per_identity must be a positive integer")
        if max_shapes_per_identity <= 0:
            raise ValueError("max_shapes_per_identity must be a positive integer")
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")

        self._max_identities = max_identities
        self._max_shapes_per_identity = max_shapes_per_identity
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.monotonic
        self._next_request_id = 0
        # OrderedDict for LRU eviction
        self._identities: OrderedDict[str, _IdentityEntry] = OrderedDict()

    @staticmethod
    def _identity_key(kind: str, value: str) -> str:
        """Return a bounded, non-sensitive key for an identity value."""
        # Request bodies can legally carry lone UTF-16 surrogates after JSON
        # decoding.  The value is only used as an in-process hash input, so
        # preserve those code points deterministically instead of letting
        # strict UTF-8 encoding turn a client-declared session into a 500.
        digest = hashlib.sha256(
            f"{kind}\0{value}".encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        return f"{kind}:{digest}"

    def _resolve_identity_key(
        self,
        user_id: str | None,
    ) -> str | None:
        """Resolve a stable identity key.

        Only an authenticated user identity is accepted. The tracker does not
        accept client or peer IPs: an unresolved proxy/NAT peer can represent
        unrelated callers, and this feature intentionally does not own the
        trusted-provenance contract from #1036.
        """
        if user_id:
            return self._identity_key("user", user_id)
        return None

    def record_request(
        self,
        user_id: str | None,
        shape_hash: str | None = None,
        session_id: str | None = None,
        observed_at: float | None = None,
    ) -> dict[str, float | int | bool | None]:
        """Record a request and return observation evidence.

        Returns a dict with:
        - inter_arrival_ms: idle time since the last response completed, or None
          when completion timing is unavailable
        - shape_repeat_count: how many times this shape has been seen
        - session_continuity: whether session matches previous request (or None
          when no comparable prior session exists)
        - request_count: number of requests recorded for this identity
        - tracked: whether an authenticated identity was available for this
          observation
        - observed_at: monotonic timestamp associated with this observation
        - request_id: opaque in-process correlation key for tracked responses
        """
        current_time = self._clock()
        request_time = current_time if observed_at is None else observed_at
        key = self._resolve_identity_key(user_id)

        if key is None:
            return {
                "inter_arrival_ms": None,
                "shape_repeat_count": None,
                "session_continuity": None,
                "request_count": 0,
                "tracked": False,
                "observed_at": request_time,
                "request_id": None,
            }

        entry = self._identities.get(key)

        # Expire the requested identity before using its historical evidence.
        # This keeps stale activity from influencing a new request without an
        # O(n) full-map scan on every hot-path call.
        if (
            entry is not None
            and entry.last_request_ts is not None
            and current_time - entry.last_request_ts >= self._ttl_seconds
        ):
            del self._identities[key]
            entry = None

        # Evict if at capacity and this is a new identity
        if entry is None and len(self._identities) >= self._max_identities:
            self._evict_one()

        if entry is None:
            entry = _IdentityEntry()
            self._identities[key] = entry
        else:
            self._identities.move_to_end(key)

        # Start-to-start time includes model serving latency, so it is not a
        # useful estimate of human think time. Only a known response completion
        # can support this signal.
        inter_arrival_ms: float | None = None
        if (
            not entry.response_timing_disabled
            and not entry.in_flight_requests
            and entry.last_response_completed_ts is not None
        ):
            elapsed = request_time - entry.last_response_completed_ts
            if elapsed >= 0:
                inter_arrival_ms = elapsed * 1000

        # Correlate completion with one request. If one identity has more
        # overlapping requests than the bounded tracker can retain, stop
        # producing timing evidence for that entry instead of guessing.
        request_id: int | None = None
        if not entry.response_timing_disabled:
            if len(entry.in_flight_requests) >= _MAX_IN_FLIGHT_REQUESTS_PER_IDENTITY:
                entry.response_timing_disabled = True
                entry.in_flight_requests.clear()
                entry.in_flight_latest_completion_ts = None
                entry.last_response_completed_ts = None
            else:
                request_id = self._next_request_id
                self._next_request_id += 1
                entry.in_flight_requests[request_id] = request_time
                entry.last_response_completed_ts = None

        # Update last request timestamp
        is_latest_observation = (
            entry.last_request_ts is None or request_time >= entry.last_request_ts
        )
        if is_latest_observation:
            entry.last_request_ts = request_time
            # A completion marker is consumed by the first later arrival.
            entry.last_response_completed_ts = None
        entry.request_count += 1

        # Keep only recent observations. The helper already returns an opaque
        # structural digest, so hashing it again adds cost without improving
        # the privacy boundary.
        shape_repeat_count: int | None = None
        if shape_hash:
            shape_key = shape_hash
            if shape_key not in entry.shape_counts:
                # Remove the least recently seen shape if at capacity.
                if len(entry.shape_counts) >= self._max_shapes_per_identity:
                    entry.shape_counts.popitem(last=False)
                entry.shape_counts[shape_key] = []
            observations = entry.shape_counts[shape_key]
            cutoff = request_time - _SHAPE_REPEAT_WINDOW_SECONDS
            first_recent = bisect_left(observations, cutoff)
            if first_recent:
                del observations[:first_recent]
            insort(observations, request_time)
            if len(observations) > _MAX_SHAPE_OBSERVATIONS:
                del observations[:-_MAX_SHAPE_OBSERVATIONS]
            entry.shape_counts.move_to_end(shape_key)
            shape_repeat_count = len(observations) - bisect_left(observations, cutoff)

        # Check session continuity without retaining the caller-provided
        # session identifier in process memory.
        session_continuity: bool | None = None
        if session_id:
            session_key = self._identity_key("session", session_id)
            if entry.session_id == session_key:
                session_continuity = True
            elif entry.session_id is not None:
                session_continuity = False
            if is_latest_observation:
                # A sessionless request is an intentional break in explicit
                # session continuity. Do not let an older marker make a later
                # return to that session look uninterrupted.
                entry.session_id = session_key
        elif is_latest_observation:
            entry.session_id = None

        return {
            "inter_arrival_ms": inter_arrival_ms,
            "shape_repeat_count": shape_repeat_count,
            "session_continuity": session_continuity,
            "request_count": entry.request_count,
            "tracked": True,
            "observed_at": request_time,
            "request_id": request_id,
        }

    def record_response_completed(
        self,
        user_id: str | None,
        request_id: int | None,
        observed_at: float | None = None,
    ) -> bool:
        """Record a response completion for its matching request only.

        Timing remains unavailable while any overlapping request is in flight.
        Once the overlap group completes, its latest completion anchors the
        next idle interval. A caller should pass the gateway's monotonic
        response-completion timestamp.
        """
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            return False
        key = self._resolve_identity_key(user_id)
        if key is None:
            return False
        entry = self._identities.get(key)
        if entry is None or entry.response_timing_disabled:
            return False
        completed_at = self._clock() if observed_at is None else observed_at
        request_time = entry.in_flight_requests.get(request_id)
        if request_time is None or completed_at < request_time:
            return False
        del entry.in_flight_requests[request_id]
        if (
            entry.in_flight_latest_completion_ts is None
            or completed_at > entry.in_flight_latest_completion_ts
        ):
            entry.in_flight_latest_completion_ts = completed_at
        if not entry.in_flight_requests:
            entry.last_response_completed_ts = entry.in_flight_latest_completion_ts
            entry.in_flight_latest_completion_ts = None
        return True

    def preview_request(
        self,
        user_id: str | None,
        shape_hash: str | None = None,
        session_id: str | None = None,
        observed_at: float | None = None,
    ) -> dict[str, float | int | bool | None]:
        """Return prospective evidence without mutating tracker state.

        This read-only operation lets callers inspect prospective evidence
        without changing the tracker's history. It performs no dispatch,
        admission, or persistence work.
        """
        now = self._clock()
        request_time = now if observed_at is None else observed_at
        key = self._resolve_identity_key(user_id)

        if key is None:
            return {
                "inter_arrival_ms": None,
                "shape_repeat_count": None,
                "session_continuity": None,
                "request_count": 0,
                "tracked": False,
                "observed_at": request_time,
                "request_id": None,
            }

        entry = self._identities.get(key)
        if (
            entry is not None
            and entry.last_request_ts is not None
            and now - entry.last_request_ts >= self._ttl_seconds
        ):
            entry = None

        if entry is None:
            return {
                "inter_arrival_ms": None,
                "shape_repeat_count": 1 if shape_hash else None,
                "session_continuity": None,
                "request_count": 1,
                "tracked": True,
                "observed_at": request_time,
                "request_id": None,
            }

        inter_arrival_ms: float | None = None
        if (
            not entry.response_timing_disabled
            and not entry.in_flight_requests
            and entry.last_response_completed_ts is not None
        ):
            elapsed = request_time - entry.last_response_completed_ts
            if elapsed >= 0:
                inter_arrival_ms = elapsed * 1000

        shape_repeat_count: int | None = None
        if shape_hash:
            shape_key = shape_hash
            observations = entry.shape_counts.get(shape_key, [])
            cutoff = request_time - _SHAPE_REPEAT_WINDOW_SECONDS
            shape_repeat_count = len(observations) - bisect_left(observations, cutoff) + 1

        session_continuity: bool | None = None
        if session_id:
            session_key = self._identity_key("session", session_id)
            if entry.session_id == session_key:
                session_continuity = True
            elif entry.session_id is not None:
                session_continuity = False

        return {
            "inter_arrival_ms": inter_arrival_ms,
            "shape_repeat_count": shape_repeat_count,
            "session_continuity": session_continuity,
            "request_count": entry.request_count + 1,
            "tracked": True,
            "observed_at": request_time,
            "request_id": None,
        }

    def _evict_one(self) -> None:
        """Evict the least recently used identity."""
        if self._identities:
            self._identities.popitem(last=False)

    def expire_old_entries(self, now: float | None = None) -> int:
        """Remove entries older than TTL. Returns count of evicted entries."""
        if now is None:
            now = self._clock()
        expired = 0
        keys_to_remove = [
            key
            for key, entry in self._identities.items()
            if (
                entry.last_request_ts is not None
                and (now - entry.last_request_ts) >= self._ttl_seconds
            )
        ]
        for key in keys_to_remove:
            del self._identities[key]
            expired += 1
        return expired

    def get_identity_count(self) -> int:
        """Return current number of tracked identities."""
        return len(self._identities)

    def reset(self) -> None:
        """Clear all state (for testing)."""
        self._identities.clear()


# Global singleton instance (one per process)
_traffic_observation_state = TrafficObservationState()


def get_traffic_observation_state() -> TrafficObservationState:
    """Return the global TrafficObservationState singleton."""
    return _traffic_observation_state


def reset_traffic_observation_state() -> None:
    """Reset global state (for testing)."""
    global _traffic_observation_state
    _traffic_observation_state = TrafficObservationState()
