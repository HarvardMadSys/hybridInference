"""Engine waits: offloading a request its engine has not started answering.

The gateway cannot see an inference engine's own queue. A vLLM or SGLang server
accepts every request it is sent and queues whatever it cannot schedule yet, so
from outside, a queued request and a slow one look the same until the engine
produces its first token. That first token is the one signal the gateway has.

For a model with an offload route (``routing.offload``), ``FixedRouter`` gives
every streaming attempt it could still offload a first-token wait of its own: an
attempt that produces no token within the model's offload wait is cancelled --
closing the stream aborts the request at the engine -- and that request goes to
the offload route next. The decision is the request's alone. Nothing is recorded
against the endpoint, and the next request is sent to it as usual, under a wait
of its own.

The router sees only what an attempt yields, so the serving layer reports what
it cannot see to the attempt's :class:`FirstTokenWatch`
(``serving.adapters.dispatch_watch``):

- **Time in the gateway's own queue.** ``serving.adapters.upstream_limiter``
  says when the request starts waiting for an outbound slot and when it gets
  one. That queue has a deadline of its own, and the engine's wait starts when
  the request leaves the gateway.
- **Output an adapter holds back.** An adapter may hold output until it can tell
  what it is -- a stream processor an XML tool call or a ``<think>`` block, the
  Claude adapters a tool call's JSON. It reports the first output it reads from
  the upstream, and from then on the request waits as long as the engine needs.

Only streaming attempts can be watched: a non-streaming response arrives whole,
so there is no first token to wait for.

The first-token wait needs Python 3.11+. Its deadline is ``asyncio.timeout``,
the one primitive that can tell its own cancellation of a task from a client's
that lands at the same moment; Python 3.10 has no way to (it merges the two),
and turning a disconnect into an offload would send a request nobody reads. On
3.10 attempts are never timed.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

logger = get_logger(__name__)

# ``asyncio.timeout`` (Python 3.11+); None on an interpreter without it.
_asyncio_timeout: Callable[[float | None], Any] | None = getattr(asyncio, "timeout", None)
_warned_untimed = False

__all__ = [
    "EngineWaitExpired",
    "FirstTokenWatch",
]


class EngineWaitExpired(Exception):
    """An attempt sent to an endpoint produced no first token within its offload wait.

    Raised by ``FixedRouter`` after it cancels the attempt. Like the gateway's
    own ``UpstreamSaturated`` it carries no ``status`` / ``status_code`` /
    ``code`` attribute: the engine gave no verdict on the request, so nothing may
    read one into it. ``endpoint_health`` exempts it from the circuit breaker --
    the engine is busy, not failing, and the next request goes to it as usual.
    """

    def __init__(self, endpoint_id: str, wait_seconds: float) -> None:
        super().__init__(f"No first token from endpoint {endpoint_id!r} within {wait_seconds:g}s")
        self.endpoint_id = endpoint_id
        self.wait_seconds = wait_seconds


class FirstTokenWatch:
    """One streaming attempt's wait for its first token.

    The wait is the attempt's deadline, enforced by ``asyncio.timeout`` around
    the router's wait for the first token (:meth:`deadline`, then :meth:`arm`).
    Its cancellation lands inside the adapter's read and unwinds the stream,
    which closes the connection and so aborts the request at the engine; the
    router then turns the ``TimeoutError`` into :class:`EngineWaitExpired`. A
    client's cancellation arriving at the same moment stays a cancellation --
    ``asyncio.timeout`` tells the two apart -- and the router yields nothing
    while the deadline is armed, so it never cancels the stream's consumer.

    What the serving layer reports (see ``serving.adapters.dispatch_watch``)
    moves the deadline: :meth:`on_queued` pauses it while the request waits for
    an outbound slot, :meth:`on_sent` restarts it when the request leaves, and
    :meth:`on_first_token` ends it for good once the upstream has started
    answering.
    """

    def __init__(self, endpoint_id: str, *, wait_seconds: float) -> None:
        self.endpoint_id = endpoint_id
        self.wait_seconds = wait_seconds
        self._deadline: Any = None
        self._stopped = False
        self._answered = False

    def deadline(self) -> AbstractAsyncContextManager[Any]:
        """Return the context the wait for the first token runs in.

        ``asyncio.timeout``, not yet armed -- pass what it yields to
        :meth:`arm`. On Python 3.10, a context that never times out.
        """
        if _asyncio_timeout is None:
            _warn_untimed()
            return contextlib.nullcontext()
        return _asyncio_timeout(None)

    def arm(self, deadline: Any) -> None:
        """Start the wait on the entered ``deadline``; a no-op for an untimed one."""
        if deadline is None:
            return
        self._deadline = deadline
        self._restart()

    def on_queued(self) -> None:
        """Pause the deadline: the request is waiting for an outbound slot."""
        self._reschedule(None)

    def on_sent(self) -> None:
        """Restart the deadline: the request is on its way to the engine."""
        self._restart()

    def on_first_token(self) -> None:
        """End the deadline for good: the upstream has started answering.

        Reported by the adapter when it reads the first output, before any of
        it is held back, so the attempt is no longer waiting on the engine even
        while the router has nothing to show for it yet.
        """
        self._answered = True
        self._reschedule(None)

    def stop(self) -> None:
        """End the wait; later reports do nothing."""
        self._stopped = True

    def expired(self) -> bool:
        """Return whether this attempt's deadline passed."""
        return self._deadline is not None and bool(self._deadline.expired())

    def _restart(self) -> None:
        if not self._answered:
            self._reschedule(asyncio.get_running_loop().time() + self.wait_seconds)

    def _reschedule(self, when: float | None) -> None:
        if self._deadline is None or self._stopped:
            return
        # A deadline that is already expiring, or whose block has exited, cannot
        # move, and has no attempt left to time.
        with contextlib.suppress(RuntimeError):
            self._deadline.reschedule(when)


def _warn_untimed() -> None:
    """Say once per process that first-token waits cannot be enforced here."""
    global _warned_untimed
    if _warned_untimed:
        return
    _warned_untimed = True
    logger.warning(
        "engine_wait_untimed",
        extra={
            "event": "engine_wait_untimed",
            "reason": "asyncio.timeout needs Python 3.11+; streams are not timed",
        },
    )
