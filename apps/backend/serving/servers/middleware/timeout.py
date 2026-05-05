"""Request timeout middleware.

Enforces a per-request timeout, returning 504 Gateway Timeout when exceeded.
``REQUEST_TIMEOUT_SECONDS`` (default 120s) is read once when the middleware is
instantiated; changing the env at runtime requires a restart.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_DEFAULT_TIMEOUT_S = 120.0


def _parse_timeout_env() -> float:
    """Read ``REQUEST_TIMEOUT_SECONDS``, falling back to the default on bad input."""
    raw = os.getenv("REQUEST_TIMEOUT_SECONDS")
    if raw is None or raw.strip() == "":
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    return value if value > 0 else _DEFAULT_TIMEOUT_S


class TimeoutMiddleware:
    """Cancel requests that exceed ``REQUEST_TIMEOUT_SECONDS`` and return 504."""

    def __init__(self, app: ASGIApp, timeout_s: float | None = None) -> None:
        self.app = app
        self._timeout_s = timeout_s if timeout_s is not None else _parse_timeout_env()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce per-request timeout and return 504 on expiry."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        with anyio.move_on_after(self._timeout_s):
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
