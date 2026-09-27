"""What the serving layer tells a dispatch's first-token watch.

``FixedRouter`` pushes a watch (``routing.engine_wait.FirstTokenWatch``) around a
streaming attempt it would offload if the engine did not start answering in
time, as ``req_ctx.UPSTREAM_DISPATCH_WATCH``, and ``None`` around every other
attempt. The router sees only what the attempt yields; these report what only
the serving layer knows:

- :func:`report_queued` and :func:`report_sent` -- the outbound limiter
  (``upstream_limiter``) is making the request wait for a slot, or has given it
  one, so the engine's wait starts when the request leaves the gateway.
- :func:`report_first_token` -- a streaming adapter has read the first output of
  the upstream's answer, before anything it does with that output can hold it
  back from the router.

Each is a no-op when the dispatch has no watch, and a failing watch is logged
and ignored: the watch only refines routing, and must never cost a request its
slot or its answer.
"""

from __future__ import annotations

from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["report_first_token", "report_queued", "report_sent"]


def report_queued() -> None:
    """Tell the watch the request is about to wait for an outbound slot."""
    _tell("on_queued")


def report_sent() -> None:
    """Tell the watch the request holds a slot and is about to be sent."""
    _tell("on_sent")


def report_first_token() -> None:
    """Tell the watch the upstream has started answering.

    Call it once, at the first frame carrying generated output -- text,
    reasoning, or any part of a tool call -- as read from the upstream, before a
    processor or accumulator can hold that output back.
    """
    _tell("on_first_token")


def _tell(hook: str) -> None:
    watch = req_ctx.get().get(req_ctx.UPSTREAM_DISPATCH_WATCH)
    if watch is None:
        return
    try:
        getattr(watch, hook)()
    except Exception:
        logger.error(
            "upstream_dispatch_watch_failed",
            exc_info=True,
            extra={"event": "upstream_dispatch_watch_failed", "hook": hook},
        )
