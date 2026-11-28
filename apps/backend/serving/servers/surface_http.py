"""What an HTTP route hands a protocol's execution function about its request.

The execution functions (:mod:`serving.surfaces`) never see a ``Request``. A
route reads the facts it can establish at entry into a
:class:`~serving.surfaces.context.ClientOrigin`, and the one capability needed
later -- whether ``TimeoutMiddleware``'s deadline has fired -- into a probe.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, TypeVar

import anyio

from serving.servers.streaming_state import REQUEST_TIMEOUT_SCOPE_STATE_KEY
from serving.surfaces.context import ClientOrigin, origin_headers
from serving.utils.request_ip import get_client_ip_info

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request

_T = TypeVar("_T")


class ClientDisconnectedError(Exception):
    """The HTTP client closed its connection before the response started."""


async def _cancel_on_disconnect(request: Request, waiting: anyio.CancelScope) -> None:
    try:
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                waiting.cancel()
                return
    except (OSError, RuntimeError):
        return


async def await_while_connected(request: Request, awaitable: Awaitable[_T]) -> _T:
    """Await *awaitable* in this task, cancelling it if *request*'s client disconnects.

    Starlette only listens for ``http.disconnect`` after a ``StreamingResponse``
    has been returned. Running *awaitable* directly in the caller's task inside
    an ``anyio.CancelScope`` keeps ``contextvars`` updates made during a
    pre-first-byte read visible to the route handler, aborts upstream prefill or
    queue waits as soon as the caller hangs up, and keeps outer
    ``TimeoutMiddleware`` deadlines distinct from client disconnects.
    """
    waiting = anyio.CancelScope()
    watcher = asyncio.create_task(_cancel_on_disconnect(request, waiting))
    try:
        with waiting:
            return await awaitable
    finally:
        watcher.cancel()
        with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
            await watcher
    assert waiting.cancelled_caught
    raise ClientDisconnectedError


def client_origin(request: Request, *, authorization: str | None = None) -> ClientOrigin:
    """The origin of *request*: its client, user agent, referer, path and readable headers.

    Args:
        request: The incoming request.
        authorization: The route's ``Authorization`` header parameter, when it
            declares one; only whether it was present is kept.
    """
    return ClientOrigin(
        resolve_ip=lambda: get_client_ip_info(request),
        user_agent=request.headers.get("user-agent"),
        referer=request.headers.get("referer"),
        route_path=request.url.path,
        authorization_header=bool(authorization),
        headers=origin_headers(request.headers.raw),
    )


def timeout_probe(request: Request) -> Callable[[], bool]:
    """Build a probe telling whether TimeoutMiddleware's deadline has fired.

    Read lazily at exception time (not at request entry): the middleware's
    ``CancelScope.cancel_called`` only flips once the deadline expires, which
    is what distinguishes a gateway timeout from a client disconnect.
    """

    def _fired() -> bool:
        state = request.scope.get("state") or {}
        timeout_scope = state.get(REQUEST_TIMEOUT_SCOPE_STATE_KEY)
        return bool(getattr(timeout_scope, "cancel_called", False))

    return _fired
