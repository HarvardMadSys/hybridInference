"""Engine stalls: an endpoint that has accepted a request but not started answering it.

The gateway cannot see an inference engine's own queue. A vLLM or SGLang server
accepts every request it is sent and queues whatever it cannot schedule yet, so
from outside, a queued request and a slow one look the same until the engine
produces its first token. That first token is the one signal the gateway has.

For a model with an offload route (``routing.offload``), ``FixedRouter`` watches
the streaming attempts it sends to the model's other routes:

- **First-token wait.** An attempt that produces no token within the model's
  offload wait is cancelled -- closing the stream aborts the request at the
  engine -- and the request goes to the offload route next. The wait starts when
  the request leaves the gateway. Time spent in the gateway's own outbound
  queue has that queue's deadline instead: ``serving.adapters.upstream_limiter``
  tells the attempt's :class:`FirstTokenWatch` when the request starts waiting
  for a slot and when it gets one.
- **Stalled endpoints.** An endpoint where an attempt waited that long is
  *stalled*. New requests go to the model's other routes, or to its offload
  route, instead of joining a queue already known to be long. It stays stalled
  until one of the attempts already on it produces a token. When this process
  has none left there, it takes one streaming request as a probe, and that
  probe's first token -- or its own wait running out -- settles it.

Only streaming attempts can be watched: a non-streaming response arrives whole,
so there is no first token to wait for. Non-streaming requests still avoid a
stalled endpoint, but never probe one.

State is per process, like the circuit breaker: each gateway worker learns about
an engine from its own traffic.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = get_logger(__name__)

__all__ = [
    "EngineStallTracker",
    "EngineWaitExpired",
    "FirstTokenWatch",
]


class EngineWaitExpired(Exception):
    """An attempt sent to an endpoint produced no first token within its offload wait.

    Raised by ``FixedRouter`` after it cancels the attempt. Like the gateway's
    own ``UpstreamSaturated`` it carries no ``status`` / ``status_code`` /
    ``code`` attribute: the engine gave no verdict on the request, so nothing may
    read one into it. ``endpoint_health`` exempts it from the circuit breaker --
    a stalled endpoint is avoided by :class:`EngineStallTracker` instead, which
    lets it back in as soon as it answers.
    """

    def __init__(self, endpoint_id: str, wait_seconds: float) -> None:
        super().__init__(f"No first token from endpoint {endpoint_id!r} within {wait_seconds:g}s")
        self.endpoint_id = endpoint_id
        self.wait_seconds = wait_seconds


@dataclass
class _EndpointState:
    #: Watched attempts still waiting for a first token: token -> start time.
    pending: dict[int, float] = field(default_factory=dict)
    #: ``clock()`` reading when the endpoint stalled; None while it is not stalled.
    stalled_since: float | None = None
    model_id: str | None = None
    wait_seconds: float | None = None


class EngineStallTracker:
    """Per-endpoint record of watched attempts and whether the endpoint has stalled.

    Every watched attempt is registered by :meth:`begin` and settled exactly once,
    by :meth:`answered`, :meth:`expired` or :meth:`abandoned`. ``FixedRouter``
    does both through :class:`FirstTokenWatch`, which it creates in the same
    synchronous step as the selection it tracks: a probe admitted by
    :meth:`admits` is therefore pending before any other request can be told the
    endpoint is free to probe.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._endpoints: dict[str, _EndpointState] = {}
        self._tokens = itertools.count(1)

    def begin(self, endpoint_id: str) -> int:
        """Register a watched attempt on ``endpoint_id`` and return its token."""
        with self._lock:
            token = next(self._tokens)
            state = self._endpoints.setdefault(endpoint_id, _EndpointState())
            state.pending[token] = self._clock()
            return token

    def answered(self, endpoint_id: str, token: int) -> None:
        """Settle an attempt that produced a token, or finished without one.

        Either way the engine got to the request, so a stall ends here.
        """
        recovered: tuple[float, str | None] | None = None
        with self._lock:
            state = self._endpoints.get(endpoint_id)
            if state is None:
                return
            state.pending.pop(token, None)
            if state.stalled_since is not None:
                recovered = (self._clock() - state.stalled_since, state.model_id)
                state.stalled_since = None
            self._drop_if_idle_locked(endpoint_id, state)
        if recovered is not None:
            stalled_for, model_id = recovered
            logger.info(
                "engine_recovered",
                extra={
                    "event": "engine_recovered",
                    "endpoint_id": endpoint_id,
                    "model_id": model_id,
                    "stalled_seconds": round(stalled_for, 3),
                },
            )

    def expired(
        self,
        endpoint_id: str,
        token: int,
        *,
        model_id: str,
        wait_seconds: float,
    ) -> None:
        """Settle an attempt whose first-token wait ran out, and stall its endpoint."""
        with self._lock:
            state = self._endpoints.setdefault(endpoint_id, _EndpointState())
            state.pending.pop(token, None)
            newly_stalled = state.stalled_since is None
            if newly_stalled:
                state.stalled_since = self._clock()
            state.model_id = model_id
            state.wait_seconds = wait_seconds
            pending = len(state.pending)
        logger.info(
            "engine_stalled",
            extra={
                "event": "engine_stalled",
                "endpoint_id": endpoint_id,
                "model_id": model_id,
                "wait_seconds": wait_seconds,
                "newly_stalled": newly_stalled,
                "pending": pending,
            },
        )

    def abandoned(self, endpoint_id: str, token: int) -> None:
        """Settle an attempt that ended without a token for another reason.

        An upstream error or a client disconnect says nothing about the engine's
        queue, so a stall neither starts nor ends here.
        """
        with self._lock:
            state = self._endpoints.get(endpoint_id)
            if state is None:
                return
            state.pending.pop(token, None)
            self._drop_if_idle_locked(endpoint_id, state)

    def admits(self, endpoint_id: str, *, probe: bool) -> bool:
        """Return whether a new request may be sent to ``endpoint_id``.

        Always, unless the endpoint is stalled. A stalled endpoint takes a new
        request only as a probe -- a streaming request, which can be watched --
        and only while none of this process's watched attempts are still on it:
        those are the requests ahead of a new one in the engine's queue, and the
        first of them to answer ends the stall anyway.
        """
        with self._lock:
            state = self._endpoints.get(endpoint_id)
            if state is None or state.stalled_since is None:
                return True
            return probe and not state.pending

    def is_stalled(self, endpoint_id: str) -> bool:
        """Return whether ``endpoint_id`` is stalled."""
        with self._lock:
            state = self._endpoints.get(endpoint_id)
            return state is not None and state.stalled_since is not None

    def stalled(self, endpoint_ids: Iterable[str]) -> list[str]:
        """Return the stalled endpoints among ``endpoint_ids``, in the order given."""
        with self._lock:
            return [
                endpoint_id
                for endpoint_id in endpoint_ids
                if (state := self._endpoints.get(endpoint_id)) is not None
                and state.stalled_since is not None
            ]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return every stalled endpoint with how long it has been stalled."""
        now = self._clock()
        with self._lock:
            return {
                endpoint_id: {
                    "model_id": state.model_id,
                    "wait_seconds": state.wait_seconds,
                    "stalled_seconds": round(now - state.stalled_since, 3),
                    "pending": len(state.pending),
                }
                for endpoint_id, state in self._endpoints.items()
                if state.stalled_since is not None
            }

    def _drop_if_idle_locked(self, endpoint_id: str, state: _EndpointState) -> None:
        if not state.pending and state.stalled_since is None:
            del self._endpoints[endpoint_id]


class FirstTokenWatch:
    """One streaming attempt's wait for its first token.

    Holds the attempt's place in :class:`EngineStallTracker` from the moment it
    is created, and settles it exactly once: :meth:`record_answer`,
    :meth:`record_expiry`, or :meth:`close` for any other ending.

    When ``wait_seconds`` is set it is also the attempt's deadline. The deadline
    runs in the task awaiting the first token and cancels that task when it
    passes, the way ``asyncio.timeout`` does (which Python 3.10 lacks). The
    cancellation lands inside the adapter's read and unwinds its stream, which
    closes the connection and so aborts the request at the engine; the router
    then turns it into :class:`EngineWaitExpired`. The router yields nothing
    while the deadline is armed, so it can never cancel the stream's consumer.

    ``serving.adapters.upstream_limiter`` calls :meth:`on_queued` when the
    request has to wait for an outbound slot and :meth:`on_sent` when it gets
    one. The queue has a deadline of its own, and the engine's wait starts when
    the request reaches the engine.
    """

    def __init__(
        self,
        tracker: EngineStallTracker,
        endpoint_id: str,
        *,
        model_id: str,
        wait_seconds: float | None,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.model_id = model_id
        self.wait_seconds = wait_seconds
        self._tracker = tracker
        self._token: int | None = tracker.begin(endpoint_id)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._cancelling = 0
        self._handle: asyncio.TimerHandle | None = None
        self._fired = False

    # -- the deadline ---------------------------------------------------------

    def start(self) -> None:
        """Arm the deadline in the current task. A no-op without ``wait_seconds``."""
        if self.wait_seconds is None:
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        cancelling = getattr(self._task, "cancelling", None)
        self._cancelling = cancelling() if cancelling is not None else 0
        self._arm()

    def on_queued(self) -> None:
        """Pause the deadline: the request is waiting for an outbound slot."""
        self._disarm()

    def on_sent(self) -> None:
        """Restart the deadline: the request is on its way to the engine."""
        self._arm()

    def stop(self) -> None:
        """Disarm the deadline for good; later reports from the limiter do nothing."""
        self._disarm()
        self._loop = None

    def owns_cancellation(self) -> bool:
        """Return whether the ``CancelledError`` being handled is this deadline's.

        Call it once, from the handler. On Python 3.11+ it also withdraws this
        deadline's cancel request, and answers False when something else asked
        for the task to be cancelled as well: a client disconnect landing at the
        same moment still cancels the request instead of turning into an
        offload.
        """
        if not self._fired:
            return False
        uncancel = getattr(self._task, "uncancel", None)
        if uncancel is None:
            return True
        return uncancel() <= self._cancelling

    def _arm(self) -> None:
        if self._loop is None or self._fired or self.wait_seconds is None:
            return
        self._disarm()
        self._handle = self._loop.call_later(self.wait_seconds, self._fire)

    def _disarm(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _fire(self) -> None:
        self._handle = None
        self._fired = True
        if self._task is not None:
            self._task.cancel()

    # -- the tracker ----------------------------------------------------------

    def record_answer(self) -> None:
        """Settle the attempt as answered: the engine produced a token or finished."""
        if self._token is not None:
            self._tracker.answered(self.endpoint_id, self._token)
            self._token = None

    def record_expiry(self) -> None:
        """Settle the attempt as expired, which stalls its endpoint."""
        if self._token is not None:
            self._tracker.expired(
                self.endpoint_id,
                self._token,
                model_id=self.model_id,
                wait_seconds=float(self.wait_seconds or 0.0),
            )
            self._token = None

    def close(self) -> None:
        """Disarm, and settle the attempt as abandoned if nothing else settled it."""
        self.stop()
        if self._token is not None:
            self._tracker.abandoned(self.endpoint_id, self._token)
            self._token = None
