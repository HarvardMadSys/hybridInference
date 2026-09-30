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

The first-token report also reaches the dispatch's place in line for a local
engine (``upstream_limiter.EngineHold``, ``req_ctx.UPSTREAM_ENGINE_HOLD``), which
gives it up so the next held request can go.

Each is a no-op when the dispatch has no watch or hold, and a failing one is
logged and ignored: both only refine routing, and must never cost a request its
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
    """Tell the watch, and the engine hold, that the upstream has started answering.

    Call it once, at the first frame carrying generated output -- text,
    reasoning, or any part of a tool call -- as read from the upstream, before a
    processor or accumulator can hold that output back.
    """
    _tell("on_first_token")
    _tell("on_first_token", key=req_ctx.UPSTREAM_ENGINE_HOLD)


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
