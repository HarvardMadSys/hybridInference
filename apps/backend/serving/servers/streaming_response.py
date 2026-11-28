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

Given a ``send_timeout``, it also ends a response whose client reads nothing of
it for that long. Such a client suspends the body at a ``yield`` with whatever
it holds until the stream cap; a download holding one of the process's few
slots (:mod:`serving.batch.delivery`) would keep it from everyone else.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
from starlette.responses import StreamingResponse

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import Message, Receive, Scope, Send

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


#: Under a send timeout, a body is passed on in parts of at most this size, so
#: that the timeout measures a client reading nothing, not a slow one reading a
#: large chunk: each part waits only for the client to take about this much.
SEND_PART_BYTES = 64 * 1024


class SendTimedOut(Exception):
    """The client read nothing of the response within its send timeout."""


class ClosingStreamingResponse(StreamingResponse):
    """A :class:`StreamingResponse` that closes its body iterator when it ends.

    With *send_timeout*, a send that the client leaves waiting that many
    seconds ends the response, as a client that went away would.
    """

    def __init__(self, *args: Any, send_timeout: float | None = None, **kwargs: Any) -> None:
        """Take the :class:`StreamingResponse` arguments, and an optional send timeout."""
        super().__init__(*args, **kwargs)
        self.send_timeout = send_timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response, then close the body, whether it finished or not."""
        try:
            if self.send_timeout is not None:
                send = _timed_send(send, self.send_timeout)
            await super().__call__(scope, receive, send)
        except SendTimedOut:
            logger.warning(
                "A client read nothing of a streaming response for %.0fs; ended it",
                self.send_timeout,
                extra={"event": "stream_send_timeout"},
            )
        finally:
            await close_body(self.body_iterator)


def _timed_send(send: Send, timeout: float) -> Send:
    """*send*, raising :class:`SendTimedOut` when one part waits longer than *timeout*."""

    async def timed(message: Message) -> None:
        body = message.get("body", b"") if message["type"] == "http.response.body" else b""
        if len(body) <= SEND_PART_BYTES:
            await _send_in_time(send, message, timeout)
            return
        more = message.get("more_body", False)
        for start in range(0, len(body), SEND_PART_BYTES):
            end = start + SEND_PART_BYTES
            part = {
                "type": "http.response.body",
                "body": body[start:end],
                "more_body": more or end < len(body),
            }
            await _send_in_time(send, part, timeout)

    return timed


async def _send_in_time(send: Send, message: Message, timeout: float) -> None:
    with anyio.move_on_after(timeout) as scope:
        await send(message)
    if scope.cancelled_caught:
        raise SendTimedOut


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
