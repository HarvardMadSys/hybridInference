"""Request timeout middleware.

Enforces a per-request timeout, returning 504 Gateway Timeout when exceeded.
``REQUEST_TIMEOUT_SECONDS`` (default 120s) and ``STREAM_REQUEST_TIMEOUT_SECONDS``
(default 3600s; <=0 disables the stream cap) are read once when the middleware is
instantiated; changing the env at runtime requires a restart.
"""

from __future__ import annotations

import json
import math
import os
from typing import TYPE_CHECKING

import anyio

from serving.servers.streaming_state import (
    REQUEST_TIMEOUT_SCOPE_STATE_KEY,
    STREAMING_RESPONSE_MARKER_HEADER,
    STREAMING_RESPONSE_SCOPE_STATE_KEY,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_DEFAULT_TIMEOUT_S = 120.0
_DEFAULT_STREAM_TIMEOUT_S = 3600.0


def _parse_timeout_env() -> float:
    """Read ``REQUEST_TIMEOUT_SECONDS``, falling back to the default on bad input."""
    return _parse_positive_timeout_env("REQUEST_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_S)


def _parse_stream_timeout_env() -> float | None:
    """Read stream cap env; non-positive values intentionally disable the cap."""
    raw = os.getenv("STREAM_REQUEST_TIMEOUT_SECONDS")
    if raw is None or raw.strip() == "":
        return _DEFAULT_STREAM_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_STREAM_TIMEOUT_S
    return value if value > 0 else None


def _parse_positive_timeout_env(name: str, default: float) -> float:
    """Read a strictly-positive timeout value with a safe default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class TimeoutMiddleware:
    """Cancel requests that exceed ``REQUEST_TIMEOUT_SECONDS`` and return 504."""

    def __init__(
        self,
        app: ASGIApp,
        timeout_s: float | None = None,
        stream_timeout_s: float | None = None,
    ) -> None:
        self.app = app
        self._timeout_s = timeout_s if timeout_s is not None else _parse_timeout_env()
        self._stream_timeout_s = (
            (stream_timeout_s if stream_timeout_s > 0 else None)
            if stream_timeout_s is not None
            else _parse_stream_timeout_env()
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce per-request timeout and return 504 on expiry."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        with anyio.move_on_after(self._timeout_s) as scope_deadline:
            # Expose the scope so response generators (StreamSession) can
            # classify a mid-stream cancellation: ``cancel_called`` is True
            # only when this deadline fired, i.e. not on client disconnects.
            scope.setdefault("state", {})[REQUEST_TIMEOUT_SCOPE_STATE_KEY] = scope_deadline

            async def send_wrapper(message: dict) -> None:
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                    headers = []
                    state = scope.get("state") or {}
                    is_streaming = bool(state.get(STREAMING_RESPONSE_SCOPE_STATE_KEY))
                    for name, value in message.get("headers") or []:
                        lower_name = name.lower()
                        if lower_name == STREAMING_RESPONSE_MARKER_HEADER:
                            is_streaming = True
                            continue
                        if lower_name == b"content-type" and value.lower().startswith(
                            b"text/event-stream"
                        ):
                            is_streaming = True
                        headers.append((name, value))
                    if is_streaming:
                        # Streams are legitimately long-lived, but not immortal:
                        # give them a separate cap so stuck bodies can't occupy
                        # connection slots forever. Operators can set
                        # STREAM_REQUEST_TIMEOUT_SECONDS<=0 to preserve the old
                        # uncapped behavior.
                        scope_deadline.deadline = (
                            math.inf
                            if self._stream_timeout_s is None
                            else anyio.current_time() + self._stream_timeout_s
                        )
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_wrapper)
            return

        if not response_started:
            body = json.dumps(
                {
                    "error": {
                        "type": "timeout",
                        "message": "Gateway Timeout",
                        "code": 504,
                    }
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 504,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                }
            )
