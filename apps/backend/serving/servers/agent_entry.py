"""The agent entry: the one listener on which an inference grant is honoured.

A Cloud Agent sandbox can reach the public internet, so a grant it holds can
leave it. The public API therefore refuses every grant, and the Cloud Agent's
gateway relay reaches this separate listener instead: a second socket in the
same process, serving the same application with the same services, quotas and
in-process limits, whose connections alone are marked as having arrived here.

The listener sets the mark, not the request. The server that accepted the
connection writes it into the ASGI scope; no header, source address or loopback
check is involved, so nothing a caller sends can make a public request look
like one of these. Where the listener is reachable is the deployment's to
decide: FreeInference publishes it on the gateway host's loopback, where only
the relay's SSH forward reaches it.

With ``GATEWAY_AGENT_ENTRY_PORT`` unset there is no agent entry, and no request
anywhere may use a grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

ENV_PORT = "GATEWAY_AGENT_ENTRY_PORT"
ENV_HOST = "GATEWAY_AGENT_ENTRY_HOST"
DEFAULT_HOST = "0.0.0.0"

#: Set on the scope of every connection the agent entry accepts, and by nothing
#: else. A scope key rather than a header: a client can send any header it
#: likes, and cannot write to the scope.
SCOPE_KEY = "serving.agent_entry"

_STARTUP_TIMEOUT_S = 10.0


def is_agent_entry(request: Request) -> bool:
    """Return whether this request arrived on the agent entry's socket."""
    return request.scope.get(SCOPE_KEY) is True


def configured_address(env: Mapping[str, str] | None = None) -> tuple[str, int] | None:
    """Return the address the agent entry listens on, or None when there is none.

    Raises ValueError for a port that is not one, so a typo stops the gateway
    at startup instead of silently leaving every grant unusable.
    """
    source = os.environ if env is None else env
    raw = (source.get(ENV_PORT) or "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{ENV_PORT}={raw!r} is not a port number") from None
    if not 0 < port < 65536:
        raise ValueError(f"{ENV_PORT}={port} is not a port number")
    host = (source.get(ENV_HOST) or "").strip() or DEFAULT_HOST
    return host, port


class _Marked:
    """Hand every connection to the application, marked as the agent entry's."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, SCOPE_KEY: True}
        await self.app(scope, receive, send)


class _Server(uvicorn.Server):
    """A uvicorn server that leaves the process's signals to the main listener."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The gateway's own listener owns SIGTERM. This one is stopped by the
        # application's shutdown, which runs once that listener has drained.
        yield


class AgentEntry:
    """A running agent entry. The application's shutdown stops it."""

    def __init__(self, server: _Server, task: asyncio.Task[None], sock: socket.socket) -> None:
        self._server = server
        self._task = task
        self.address: tuple[str, int] = sock.getsockname()[:2]

    async def stop(self) -> None:
        """Stop accepting, let open requests finish, and close the socket."""
        self._server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def start(app: ASGIApp, host: str, port: int) -> AgentEntry:
    """Bind the agent entry and serve *app* on it until stopped.

    The socket is bound here, before serving, so a port that is taken fails the
    application's startup with a message naming the agent entry, instead of
    uvicorn exiting the process from inside a task.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise RuntimeError(f"the agent entry cannot listen on {host}:{port}: {exc}") from exc

    config = uvicorn.Config(
        _Marked(app),
        # The application's lifespan belongs to the main listener; running it
        # again here would start every background task twice.
        lifespan="off",
        # The same boundary as the main listener: no forwarding headers, no
        # server banner, and logging left as the gateway configured it.
        proxy_headers=False,
        server_header=False,
        log_config=None,
    )
    server = _Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name="agent-entry")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_TIMEOUT_S
    while not server.started:
        if task.done():
            sock.close()
            error = task.exception() if not task.cancelled() else None
            raise RuntimeError(f"the agent entry on {host}:{port} did not start: {error!r}")
        if loop.time() > deadline:
            server.should_exit = True
            task.cancel()
            sock.close()
            raise RuntimeError(f"the agent entry on {host}:{port} did not start in time")
        await asyncio.sleep(0.01)

    entry = AgentEntry(server, task, sock)
    logger.info("agent entry listening on %s:%d", *entry.address)
    return entry
