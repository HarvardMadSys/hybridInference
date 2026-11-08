"""What the serving layer tells a dispatch's first-token watch.

``FixedRouter`` pushes a report around every attempt, as
``req_ctx.UPSTREAM_DISPATCH_WATCH``: it counts the attempt's prefill lease
against other callers only once the request has left the gateway, and passes
each report on to a first-token watch (``routing.engine_wait.FirstTokenWatch``)
around a streaming attempt it would offload if the engine did not start
answering in time. The router sees only what the attempt yields; these report
what only the serving layer knows:

- :func:`report_queued` and :func:`report_sent` -- the outbound limiter
  (``upstream_limiter``) is making the request wait for a slot, or has given it
  one, so the engine's wait starts when the request leaves the gateway.
- :func:`report_first_token` -- a streaming adapter has read the first output of
  the upstream's answer, before anything it does with that output can hold it
  back from the router.

The first-token report also reaches the dispatch's place in line for a local
engine (``upstream_limiter.EngineHold``, ``req_ctx.UPSTREAM_ENGINE_HOLD``), which
gives it up so the next held request can go.

Each is a no-op when the dispatch has no watch or hold, and a failing one is
logged and ignored: both only refine routing, and must never cost a request its
slot or its answer.

:func:`report_sent` also marks the handler's :class:`DispatchRecord`
(``req_ctx.UPSTREAM_DISPATCH_RECORD``), which a non-streaming handler pushes
around each upstream call: a call a cancellation cuts short is billed only once
it left the gateway.
"""

from __future__ import annotations

import asyncio

from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["DispatchRecord", "report_first_token", "report_queued", "report_sent"]


def report_queued() -> None:
    """Tell the watch, and the dispatch record, the request is about to wait for a slot."""
    _tell("on_queued")
    _tell("on_queued", key=req_ctx.UPSTREAM_DISPATCH_RECORD)


class DispatchRecord:
    """Whether a handler's upstream call has been sent, and to which provider.

    :attr:`sent` turns true when the outbound limiter gives the call its slot,
    right before the request leaves the gateway; before that the upstream has
    not seen it. An attempt that then waits for another slot -- a key rotation,
    or a fallback after a failed attempt, which nobody charges for -- is off the
    wire again. Attempts are told apart by the task they run in, so a hedged
    request's two legs, which run side by side, count separately.
    ``provider`` is the label of the route the router was dispatching to when
    the request was last sent (``req_ctx.PROVIDER``, which it pushes around each
    attempt), so a call cut off before it answered can be priced.
    """

    __slots__ = ("_on_wire", "provider")

    def __init__(self) -> None:
        """Start unsent."""
        self._on_wire: set[object] = set()
        self.provider: str | None = None

    @property
    def sent(self) -> bool:
        """Whether an attempt of the call has been sent and not given up its place since."""
        return bool(self._on_wire)

    def on_queued(self) -> None:
        """This attempt waits for an outbound slot: what it sent before has ended."""
        self._on_wire.discard(_attempt())

    def on_sent(self) -> None:
        """This attempt holds its slot and is about to be sent."""
        self._on_wire.add(_attempt())
        provider = req_ctx.get().get(req_ctx.PROVIDER)
        if isinstance(provider, str) and provider not in ("", req_ctx.ROUTER_PROVIDER_SENTINEL):
            self.provider = provider


def report_sent() -> None:
    """Tell the watch, and the dispatch record, the request holds a slot and is about to be sent."""
    _tell("on_sent")
    _tell("on_sent", key=req_ctx.UPSTREAM_DISPATCH_RECORD)


def report_first_token() -> None:
    """Tell the watch, and the engine hold, that the upstream has started answering.

    Call it once, at the first frame carrying generated output -- text,
    reasoning, or any part of a tool call -- as read from the upstream, before a
    processor or accumulator can hold that output back.
    """
    _tell("on_first_token")
    _tell("on_first_token", key=req_ctx.UPSTREAM_ENGINE_HOLD)


def _attempt() -> object:
    """The task the reporting attempt runs in (None outside one)."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _tell(hook: str, *, key: str = req_ctx.UPSTREAM_DISPATCH_WATCH) -> None:
    target = req_ctx.get().get(key)
    if target is None:
        return
    try:
        getattr(target, hook)()
    except Exception:
        logger.error(
            "upstream_dispatch_watch_failed",
            exc_info=True,
            extra={"event": "upstream_dispatch_watch_failed", "hook": hook, "target": key},
        )
