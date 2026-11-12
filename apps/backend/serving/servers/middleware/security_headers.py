"""Give every gateway response ``X-Content-Type-Options: nosniff``.

The console adds this header to every response it relays, and until a
deployment's entry sent ``/v1`` and ``/anthropic`` straight to the gateway that
was every response a client saw. With the entry split the gateway answers
those clients itself, so it sets the header on its own responses: no browser
is to guess a content type the gateway did not declare.

Pure ASGI, and it touches only the ``http.response.start`` message: the body
passes through as the application sends it, so a stream is never buffered. A
response that already carries the header keeps its own and gets no second one.
The console still sets the header on what it relays; the two agree, so a
response through the console is unchanged.

The console's other two headers, ``X-Frame-Options`` and ``Referrer-Policy``,
only mean something for a page, and the gateway serves none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_NAME = b"x-content-type-options"
_VALUE = b"nosniff"


class NoSniffMiddleware:
    """Add ``X-Content-Type-Options: nosniff`` to responses that lack it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, adding the header to the response start."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == _NAME for name, _ in headers):
                    headers.append((_NAME, _VALUE))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)
