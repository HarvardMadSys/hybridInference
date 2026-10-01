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
- **Engine wait.** A streaming attempt that reaches an engine but gets no first
  token within the same threshold is cancelled there and offloaded the same
  way (``routing.engine_wait``). That is the request's own outcome: the engine
  is not marked, and the next request is routed to it as usual.
- **Last resort.** When no other route is admissible at selection time, or every
  other route has failed, the offload route is tried like any fallback.

The offload attempt itself queues normally, with no threshold of its own: there
is nowhere left to offload it to.

An attempt cut short for the offload route got no answer from its own route. So
when the offload route fails too, the request goes back to that route once, after
every other candidate, and waits there as long as it needs. An offload can make a
request slower, but never fails one its own route would have served.

Nor is a request cut short for an offload route that cannot serve it. When the
route holds no key the caller may spend -- each is reserved above the caller's
tier (``KeyPool`` ``min_role``), or in cooldown -- the request's attempts get no
deadline, and wait as long as they would with no offload route at all.

What makes this safe to do on a queue wait is that the wait happens *before*
dispatch. The request has not reached the provider, so abandoning its place in
line releases nothing upstream and cannot duplicate a generation; the limiter
exempts the refusal from endpoint health for the same reason.

A local inference server has no queue in the gateway of its own: it accepts every
request it is sent and queues what it cannot schedule yet, where the gateway
cannot see it. The policy can give it one. With an *engine queue limit* of N, a
local engine of the model is sent at most N streaming requests that have not
returned a first token yet, and the rest wait in the gateway in arrival order
(``upstream_limiter.EngineHold``). A request held there is a queue wait like any
other: one that could be offloaded leaves at the threshold, before the engine has
seen it, and one that could not waits its turn. A request's place frees the
moment its engine returns a first token, so the requests an engine is already
answering do not count, and nothing caps how many it runs at once.

An endpoint the gateway does not queue for -- a local inference server under no
engine queue limit, or a remote endpoint while the limiter is disabled -- never
triggers an offload by waiting in that queue, only by an engine wait.

The policy can also cap what is offloaded by size. With a *max input* of N
tokens, a request whose prompt is estimated at more than N is never sent to the
offload route, for any of the three reasons, and is served only by the model's
other routes, as though the offload route were not on the model at all. Its
attempts get no deadline, so it waits as long as its own route needs; and when
none of the other routes is admissible, it fails rather than being offloaded.
The size is the router's own estimate of the prompt
(``routing.prefill_load.estimate_prefill_tokens``: everything the request sends
upstream, at four bytes a token), not an upstream tokenizer's count.

This module holds the parts that are independent of one router's control flow:
the policy value, the read-only source routers consult, and the per-request
fallback order. The router owns dispatch, admission and accounting.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from routing.engine_wait import EngineWaitExpired
from serving.adapters.upstream_limiter import UpstreamQueueWaitExpired, UpstreamSaturated

if TYPE_CHECKING:
    from collections.abc import Iterable

    from serving.adapters.base import BaseAdapter

__all__ = [
    "OFFLOAD_ENGINE_WAIT",
    "OFFLOAD_LAST_RESORT",
    "OFFLOAD_QUEUE_WAIT",
    "FallbackOrder",
    "OffloadPolicy",
    "OffloadPolicySource",
    "abandoned_for_offload",
    "ended_in_gateway_queue",
    "offload_reason_for",
]

#: ``_routing["offload"]`` when an earlier attempt waited out its queue budget.
OFFLOAD_QUEUE_WAIT = "queue_wait"
#: ``_routing["offload"]`` when an earlier attempt's engine sent no first token in time.
OFFLOAD_ENGINE_WAIT = "engine_wait"
#: ``_routing["offload"]`` when no other route could serve the request.
OFFLOAD_LAST_RESORT = "last_resort"


@dataclass(frozen=True, slots=True)
class OffloadPolicy:
    """One model's offload route and how long a request queues before using it.

    Attributes:
        route_id: The route's admin id (``routing.endpoints.route_id_for_adapter``),
            which survives an edit that moves the route to another host.
        wait_seconds: How long an attempt on any other route of the model may
            wait for an outbound slot, and then for the engine's first token,
            before the request is offloaded.
        engine_queue_limit: How many streaming requests a local engine of the
            model may have been sent without returning a first token before the
            gateway holds the rest, or ``None`` to send each one straight to the
            engine.
        max_input_tokens: The largest estimated prompt, in tokens, the offload
            route may be sent, or ``None`` for no limit. A longer request is
            served only by the model's other routes (:meth:`takes_input`).
    """

    route_id: str
    wait_seconds: float
    engine_queue_limit: int | None = None
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or not self.route_id.strip():
            raise ValueError("offload route_id must be a non-empty string")
        wait = self.wait_seconds
        if isinstance(wait, bool) or not isinstance(wait, (int, float)):
            raise ValueError("offload wait_seconds must be a number")
        if not math.isfinite(wait) or wait <= 0:
            raise ValueError("offload wait_seconds must be a positive, finite number")
        object.__setattr__(self, "wait_seconds", float(wait))
        limit = self.engine_queue_limit
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("offload engine_queue_limit must be a whole number of at least 1")
        max_input = self.max_input_tokens
        if max_input is not None and (
            isinstance(max_input, bool) or not isinstance(max_input, int) or max_input < 1
        ):
            raise ValueError("offload max_input_tokens must be a whole number of at least 1")

    def takes_input(self, prompt_tokens: int) -> bool:
        """Return whether a request of ``prompt_tokens`` estimated tokens may be offloaded.

        True up to and including the max input, and always when there is none.
        """
        return self.max_input_tokens is None or prompt_tokens <= self.max_input_tokens


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


def offload_reason_for(exc: BaseException) -> str | None:
    """Return why a failed attempt sends its request to the offload route next.

    :data:`OFFLOAD_QUEUE_WAIT` for an attempt that ended in the gateway's queue,
    :data:`OFFLOAD_ENGINE_WAIT` for one whose engine never sent a first token,
    and ``None`` for any other failure, which walks the rest of the route first.
    """
    if ended_in_gateway_queue(exc):
        return OFFLOAD_QUEUE_WAIT
    if isinstance(exc, EngineWaitExpired):
        return OFFLOAD_ENGINE_WAIT
    return None


def abandoned_for_offload(exc: BaseException) -> bool:
    """Return whether the router cut an attempt short to offload its request.

    True for the two deadlines an offload route arms: the engine wait
    (``EngineWaitExpired``) and the queue wait (``UpstreamQueueWaitExpired``).
    Neither is the route's answer, because the route was never heard from. A
    queue wait that ran to the limiter's own acquire timeout (a plain
    ``UpstreamSaturated``) is not included: that attempt waited as long as it
    would have with no offload route at all.
    """
    return isinstance(exc, (EngineWaitExpired, UpstreamQueueWaitExpired))


class FallbackOrder:
    """The order one request tries the rest of its route in after its primary fails.

    Ordinary candidates keep their route order. The offload route, when the model
    has a usable one, is taken out of that order and placed by outcome instead:
    straight after an attempt that waited too long (:func:`offload_reason_for`),
    otherwise after every ordinary candidate. It is handed out at most once per
    request.

    An attempt cut short for the offload route (:func:`abandoned_for_offload`)
    has its route handed out once more, after everything else. By then the
    offload route is no longer pending, so the caller arms no deadline for it,
    and the route answers however long it takes.

    Callers report each failed attempt through :meth:`record_failure` before
    asking for the next candidate.
    """

    __slots__ = ("_offload", "_offload_next", "_ordinary", "_retried", "_retries")

    def __init__(
        self,
        ordinary: Iterable[BaseAdapter],
        offload: BaseAdapter | None = None,
    ) -> None:
        self._ordinary: deque[BaseAdapter] = deque(
            adapter for adapter in ordinary if adapter is not offload
        )
        self._offload = offload
        self._offload_next: str | None = None
        self._retries: deque[BaseAdapter] = deque()
        self._retried: set[int] = set()

    @property
    def offload_pending(self) -> BaseAdapter | None:
        """Return the offload adapter while it has not been handed out yet."""
        return self._offload

    def record_failure(self, exc: BaseException, adapter: BaseAdapter | None = None) -> None:
        """Fold one failed attempt's error into the order of what comes next.

        ``adapter`` is the route the attempt was sent to. It is queued for a
        retry when the attempt was cut short for the offload route, even if the
        order no longer holds that offload route. Each route is queued at most
        once per request, whatever its retry raises, so no error can keep a
        request cycling back to the same route.
        """
        if adapter is not None and abandoned_for_offload(exc) and id(adapter) not in self._retried:
            self._retried.add(id(adapter))
            self._retries.append(adapter)
        if self._offload is None:
            return
        reason = offload_reason_for(exc)
        if reason is not None:
            self._offload_next = reason

    def next(self) -> tuple[BaseAdapter, str | None] | None:
        """Return the next candidate and, for the offload route, why it is used.

        The second element is ``None`` for an ordinary candidate or a retry,
        and one of the ``OFFLOAD_*`` reasons for the offload route. ``None``
        overall means the order is exhausted.
        """
        if self._offload is not None and (self._offload_next or not self._ordinary):
            adapter = self._offload
            reason = self._offload_next or OFFLOAD_LAST_RESORT
            self._offload = None
            self._offload_next = None
            return adapter, reason
        if self._ordinary:
            return self._ordinary.popleft(), None
        if self._retries:
            return self._retries.popleft(), None
        return None
