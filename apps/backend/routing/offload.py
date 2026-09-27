"""Queue-wait offload: a reserved route for requests their selected route cannot seat.

An admin can designate one route of a fixed-routed model as its *offload route*,
with a wait threshold in seconds. The route is then held back from ordinary
traffic, and :class:`~routing.routers.FixedRouter` uses it in two situations:

- **Queue wait.** Every other attempt the request makes may wait at most the
  threshold for an outbound concurrency slot (``serving.adapters.upstream_limiter``
  queues a request whose provider key is at its limit). If none frees in time
  the request leaves the queue -- nothing has been sent upstream -- and goes to
  the offload route next, ahead of the rest of the fallback order. A wait that
  ends at the limiter's own acquire timeout counts the same way: the request
  queued and never got a slot.
- **Last resort.** When no other route is admissible at selection time, or every
  other route has failed, the offload route is tried like any fallback.

The offload attempt itself queues normally, with no threshold of its own: there
is nowhere left to offload it to.

What makes this safe to do on a queue wait is that the wait happens *before*
dispatch. The request has not reached the provider, so abandoning its place in
line releases nothing upstream and cannot duplicate a generation; the limiter
exempts the refusal from endpoint health for the same reason. An endpoint the
gateway does not queue for -- a local inference server, or any endpoint while the
limiter is disabled -- never triggers an offload by waiting.

This module holds the parts that are independent of one router's control flow:
the policy value, the read-only source routers consult, and the per-request
fallback order. The router owns dispatch, admission and accounting.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from serving.adapters.upstream_limiter import UpstreamSaturated

if TYPE_CHECKING:
    from collections.abc import Iterable

    from serving.adapters.base import BaseAdapter

__all__ = [
    "OFFLOAD_LAST_RESORT",
    "OFFLOAD_QUEUE_WAIT",
    "FallbackOrder",
    "OffloadPolicy",
    "OffloadPolicySource",
    "ended_in_gateway_queue",
]

#: ``_routing["offload"]`` when an earlier attempt waited out its queue budget.
OFFLOAD_QUEUE_WAIT = "queue_wait"
#: ``_routing["offload"]`` when no other route could serve the request.
OFFLOAD_LAST_RESORT = "last_resort"


@dataclass(frozen=True, slots=True)
class OffloadPolicy:
    """One model's offload route and how long a request queues before using it.

    Attributes:
        route_id: The route's admin id (``routing.endpoints.route_id_for_adapter``),
            which survives an edit that moves the route to another host.
        wait_seconds: How long an attempt on any other route of the model may
            wait for an outbound slot before the request is offloaded.
    """

    route_id: str
    wait_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or not self.route_id.strip():
            raise ValueError("offload route_id must be a non-empty string")
        wait = self.wait_seconds
        if isinstance(wait, bool) or not isinstance(wait, (int, float)):
            raise ValueError("offload wait_seconds must be a number")
        if not math.isfinite(wait) or wait <= 0:
            raise ValueError("offload wait_seconds must be a positive, finite number")
        object.__setattr__(self, "wait_seconds", float(wait))


@runtime_checkable
class OffloadPolicySource(Protocol):
    """Synchronous read of the offload policy in force for a canonical model."""

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        """Return the model's offload policy, or None when it has none."""
        ...


def ended_in_gateway_queue(exc: BaseException) -> bool:
    """Return whether an attempt ended in this gateway's outbound queue, unsent.

    Covers both ways a queued request leaves without a slot: its own offload
    budget running out (``UpstreamQueueWaitExpired``) and the limiter's acquire
    timeout (``UpstreamSaturated``, its base class).
    """
    return isinstance(exc, UpstreamSaturated)


class FallbackOrder:
    """The order one request tries the rest of its route in after its primary fails.

    Ordinary candidates keep their route order. The offload route, when the model
    has a usable one, is taken out of that order and placed by outcome instead:
    straight after an attempt that ended in the gateway queue, otherwise after
    every ordinary candidate. It is handed out at most once per request.

    Callers report each failed attempt through :meth:`record_failure` before
    asking for the next candidate.
    """

    __slots__ = ("_offload", "_offload_next", "_ordinary")

    def __init__(
        self,
        ordinary: Iterable[BaseAdapter],
        offload: BaseAdapter | None = None,
    ) -> None:
        self._ordinary: deque[BaseAdapter] = deque(
            adapter for adapter in ordinary if adapter is not offload
        )
        self._offload = offload
        self._offload_next = False

    @property
    def offload_pending(self) -> BaseAdapter | None:
        """Return the offload adapter while it has not been handed out yet."""
        return self._offload

    def record_failure(self, exc: BaseException) -> None:
        """Fold one failed attempt's error into the order of what comes next."""
        if self._offload is not None and ended_in_gateway_queue(exc):
            self._offload_next = True

    def next(self) -> tuple[BaseAdapter, str | None] | None:
        """Return the next candidate and, for the offload route, why it is used.

        The second element is ``None`` for an ordinary candidate, and
        :data:`OFFLOAD_QUEUE_WAIT` or :data:`OFFLOAD_LAST_RESORT` for the
        offload route. ``None`` overall means the order is exhausted.
        """
        if self._offload is not None and (self._offload_next or not self._ordinary):
            adapter = self._offload
            reason = OFFLOAD_QUEUE_WAIT if self._offload_next else OFFLOAD_LAST_RESORT
            self._offload = None
            self._offload_next = False
            return adapter, reason
        if self._ordinary:
            return self._ordinary.popleft(), None
        return None
