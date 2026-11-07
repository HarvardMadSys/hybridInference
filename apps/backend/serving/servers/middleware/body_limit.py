"""Refuse request bodies over a size limit with 413, without buffering them.

The gateway serves every user from one event loop, and a request body is read
into memory and parsed as one piece: by FastAPI before any dependency runs on a
route that declares a body model, by a handler or the concurrency gate on the
inference routes. With no limit, one large body costs every user a stall and the
process its memory. This middleware answers ``413 Content Too Large``:

* before reading anything, when ``Content-Length`` declares more than the limit;
* as soon as the bytes actually received pass it, for a body sent chunked or
  under a length the server never sees.

It counts bytes as they pass and keeps none of them, and it never touches a
response it did not write itself, so streaming responses pass through exactly as
before. Once a body passes the limit, the application is told the client
disconnected (``http.disconnect``) and whatever it answers is discarded: the 413
has already been sent. The response asks for the connection to be closed, so the
server reads no more of the body.

The limit is ``REQUEST_MAX_BODY_MB`` (32 MiB unless set; the Anthropic Messages
API refuses larger requests too), read on every request. Batch input uploads are
exempt: they stream to storage under their own limit, ``BATCH_MAX_BODY_MB``,
and refuse an oversized body themselves.

Control routes -- sign-in and the console's own account, the identity hand-off,
administration and service-to-service calls -- get a much smaller limit
(:data:`CONTROL_MAX_BODY_BYTES`, :data:`ADMIN_MAX_BODY_BYTES`). FastAPI reads
and decodes a route's declared body model before any dependency runs,
authentication included, so on these routes an anonymous caller's body is
decoded before the 401; none of them takes more than a few KiB from a
legitimate caller. The few control routes that do take a large body read it
only once the caller is authenticated (:mod:`serving.servers.request_body`)
and keep the general limit (:data:`AUTHENTICATED_BODY_PATHS`).

Registered as the innermost middleware, inside CORS, so the 413 carries the
CORS headers a browser needs to read it, and inside the request-id and
request-log middlewares, so it is logged like any other response.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from serving.config.app_config import config_value
from serving.servers.middleware.error import _build_error_response
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger(__name__)

MAX_BODY_SETTING = "REQUEST_MAX_BODY_MB"
DEFAULT_MAX_BODY_MB = 32
_MIB = 1024 * 1024

#: Routes that stream an upload into storage and enforce their own, larger
#: limit (``BATCH_MAX_BODY_MB``, plus multipart framing) as the bytes arrive.
#: Capping them here too would make that setting unable to raise their limit.
EXEMPT_PATHS: frozenset[str] = frozenset({"/v1/files", "/v1/batches", "/user/batch-files"})

#: The limit on a control route's body. Their bodies are a few fields; the
#: largest legitimate one is a portrait (``PATCH /user/profile``, under 70 KiB).
#: Decoding this much costs about 10 ms, against 1.6-2.7 s for 32 MiB.
CONTROL_MAX_BODY_BYTES = 256 * 1024

#: The limit under ``/admin/``, where an administrator also writes longer text:
#: a broadcast email, a site update.
ADMIN_MAX_BODY_BYTES = _MIB

#: Path prefixes of the control routes, each with its limit.
CONTROL_PREFIXES: tuple[tuple[str, int], ...] = (
    ("/admin/", ADMIN_MAX_BODY_BYTES),
    ("/auth/", CONTROL_MAX_BODY_BYTES),
    ("/user/", CONTROL_MAX_BODY_BYTES),
    ("/internal/", CONTROL_MAX_BODY_BYTES),
    ("/v1/identity/", CONTROL_MAX_BODY_BYTES),
)

#: Control routes outside those prefixes, matched exactly.
CONTROL_PATHS: frozenset[str] = frozenset({"/v1/batch_webhook"})

#: Control routes that legitimately take a large body and read it only once the
#: caller is authenticated, so they keep the general limit: a configuration
#: backup, a playground prompt, and the alert-history ingest, which enforces
#: its own smaller limit and error.
AUTHENTICATED_BODY_PATHS: frozenset[str] = frozenset(
    {
        "/admin/config/restore",
        "/admin/config/restore/preview",
        "/internal/playground/chat",
        "/internal/alert-history/events",
    }
)

#: The surfaces whose clients parse Anthropic-shaped errors; the same prefixes
#: ``anthropic_messages.anthropic_aware_http_exception_handler`` matches.
_ANTHROPIC_PATH_PREFIXES = ("/v1/messages", "/anthropic/")

#: Anthropic's own error type for a request over its size limit.
_ERROR_TYPE = "request_too_large"


def configured_max_body_bytes() -> int:
    """Return the body limit in bytes from ``REQUEST_MAX_BODY_MB``.

    A missing, malformed or non-positive value falls back to the default: a
    typo must not lift the limit, and the setting has no "unlimited" value.
    """
    raw = config_value(MAX_BODY_SETTING)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_BODY_MB * _MIB
    try:
        megabytes = int(raw.strip())
    except ValueError:
        return DEFAULT_MAX_BODY_MB * _MIB
    return (megabytes if megabytes > 0 else DEFAULT_MAX_BODY_MB) * _MIB


def control_limit(path: str) -> int | None:
    """The smaller limit of a control route's body, or None for any other path.

    *path* is the request path without the mount prefix or a trailing slash.
    """
    if path in AUTHENTICATED_BODY_PATHS:
        return None
    if path in CONTROL_PATHS:
        return CONTROL_MAX_BODY_BYTES
    for prefix, limit in CONTROL_PREFIXES:
        if path.startswith(prefix):
            return limit
    return None


def _route_path(scope: Scope) -> str:
    """The request path without the mount prefix or a trailing slash."""
    path: str = scope.get("path") or "/"
    root_path: str = scope.get("root_path") or ""
    if root_path and path.startswith(root_path + "/"):
        path = path[len(root_path) :]
    return path.rstrip("/") or "/"


def _declared_length(scope: Scope) -> int | None:
    """The ``Content-Length`` the client declared, or None if absent or unreadable."""
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _describe(limit: int) -> str:
    if limit % _MIB == 0:
        return f"{limit // _MIB} MiB"
    return f"{limit} bytes"


def _too_large_body(path: str, limit: int) -> bytes:
    message = f"Request body exceeds the size limit of {_describe(limit)}"
    content: dict[str, Any]
    if path.startswith(_ANTHROPIC_PATH_PREFIXES):
        content = {"type": "error", "error": {"type": _ERROR_TYPE, "message": message}}
    else:
        content = _build_error_response(message, code=413, typ=_ERROR_TYPE)
    return json.dumps(content).encode()


async def _send_too_large(send: Send, path: str, limit: int) -> None:
    body = _too_large_body(path, limit)
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # Do not read the rest of the body to keep the connection open.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestBodyLimitMiddleware:
    """Answer 413 for a request body over the limit; pass everything else through.

    ``max_body_bytes`` fixes the general limit; without it the limit is read
    from the configuration on every request, so a changed setting applies to
    the next request without a restart. A control route's own limit applies
    when it is smaller.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int | None = None) -> None:
        self.app = app
        self._fixed_limit = max_body_bytes

    def _limit(self, path: str) -> int:
        limit = self._fixed_limit if self._fixed_limit is not None else configured_max_body_bytes()
        control = control_limit(path)
        return limit if control is None else min(limit, control)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the body limit on one HTTP request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = _route_path(scope)
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        limit = self._limit(path)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            logger.warning(
                "request_body_too_large",
                extra={
                    "path": path,
                    "method": scope.get("method"),
                    "declared_bytes": declared,
                    "limit_bytes": limit,
                },
            )
            await _send_too_large(send, path, limit)
            return

        received = 0
        over_limit = False
        answered = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, over_limit, answered
            if over_limit:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] != "http.request":
                return message
            received += len(message.get("body", b""))
            if received <= limit:
                return message
            over_limit = True
            logger.warning(
                "request_body_too_large",
                extra={
                    "path": path,
                    "method": scope.get("method"),
                    "received_bytes": received,
                    "limit_bytes": limit,
                },
            )
            if not response_started:
                # Claimed before the first await, so nothing the application
                # sends from here on can interleave with this response.
                answered = True
                await _send_too_large(send, path, limit)
            # The chunk that passed the limit is never handed on: the
            # application sees the client go away and stops reading.
            return {"type": "http.disconnect"}

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if answered:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not answered:
                raise
            # The application failed on the body it was refused; the client
            # already has its answer.
            logger.debug("request_body_too_large: application error after 413", exc_info=True)
