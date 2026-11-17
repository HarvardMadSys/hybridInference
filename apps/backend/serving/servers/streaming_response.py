"""A streaming response whose body is finished before the response is over.

Starlette's ``StreamingResponse`` iterates its body and never closes it. When
the client goes away, Starlette cancels the task sending the body; if the body
generator was suspended at a ``yield`` at that moment (the send was waiting for
a slow client to read), nothing closes it. Python closes an abandoned async
generator only once it is garbage-collected -- later, in a task of its own, or,
when a traceback keeps it alive, much later. A gateway stream bills what the
upstream did and writes its request-log row in that close, so until then the
request has ended for the server and its client but not for the books: the
release handoff (:mod:`serving.servers.handoff`) would see the process idle
while a charge and a row were still to come, and a process stopped then loses
them.

:class:`ClosingStreamingResponse` closes the body as the response ends, however
it ends, inside the request: its accounting is done before the request is.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
from starlette.responses import StreamingResponse

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

logger = get_logger(__name__)

#: The longest closing a body may take. A gateway body's close is synchronous
#: bookkeeping plus closing its upstream reader; this only bounds a close that
#: hangs, so that it cannot hold the request for good.
CLOSE_TIMEOUT_SECONDS = 10.0

#: Bodies whose close took longer than :data:`CLOSE_TIMEOUT_SECONDS` and was
#: abandoned, since the process started. Whatever their close had still to
#: bill or log is not known to be done, so a process with any is never sealed
#: (:mod:`serving.servers.handoff`).
_ABANDONED_CLOSES = 0


def abandoned_closes() -> int:
    """How many body closes were abandoned unfinished since the process started."""
    return _ABANDONED_CLOSES


class ClosingStreamingResponse(StreamingResponse):
    """A :class:`StreamingResponse` that closes its body iterator when it ends."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response, then close the body, whether it finished or not."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            await close_body(self.body_iterator)


async def close_body(body: Any, *, quiet: bool = False) -> bool:
    """Close *body* if it can be closed, within a deadline, shielded from the request's cancellation.

    A body that finished, or that a cancellation already unwound, is closed
    already, and closing it again does nothing. A cancelled request (the
    gateway's timeout, a disconnect) is still being cancelled here, and the
    close must not be cut short by it, hence the shield. A close that hangs is
    cut short at :data:`CLOSE_TIMEOUT_SECONDS` instead, counted
    (:func:`abandoned_closes`) and logged; False is returned.

    Every close a body makes of what it wraps on the way out goes through here
    too (the chat stream's, the Responses events'): a shield with no deadline
    anywhere in that chain would stop the outermost deadline from reaching a
    hung close inside it, so each level carries its own. *quiet* also swallows
    a cancellation the closed body raises itself, for closes of an upstream
    reader whose own cancellation is not this request's concern.
    """
    aclose = getattr(body, "aclose", None)
    if aclose is None:
        return True
    swallowed: tuple[type[BaseException], ...] = (
        (Exception, asyncio.CancelledError) if quiet else (Exception,)
    )
    with anyio.move_on_after(CLOSE_TIMEOUT_SECONDS, shield=True) as scope:
        try:
            await aclose()
        except swallowed:
            if not quiet and not scope.cancel_called:
                logger.warning("Closing a streaming response body failed", exc_info=True)
    # Read the deadline, not the exception: the body may swallow the cancellation.
    if not scope.cancel_called:
        return True
    global _ABANDONED_CLOSES
    _ABANDONED_CLOSES += 1
    logger.error(
        "Closing a streaming response body took over %.0fs; left unfinished",
        CLOSE_TIMEOUT_SECONDS,
        extra={"event": "stream_body_close_timeout"},
    )
    return False
