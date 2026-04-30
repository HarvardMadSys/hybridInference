"""Request timeout middleware.

Enforces a per-request asyncio timeout, returning 504 Gateway Timeout when
exceeded. ``REQUEST_TIMEOUT_SECONDS`` (default 120s) is read once when the
middleware is instantiated; changing the env at runtime requires a restart.

We return a JSONResponse directly rather than raising HTTPException because
exceptions raised from a ``BaseHTTPMiddleware`` bubble outside FastAPI's
exception handlers (which wrap the router, not the middleware stack) and
would otherwise be converted to a generic 500 by Starlette's outermost
ServerErrorMiddleware.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request, Response
    from starlette.types import ASGIApp


_DEFAULT_TIMEOUT_S = 120.0


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Cancel requests that exceed ``REQUEST_TIMEOUT_SECONDS`` and return 504."""

    def __init__(self, app: ASGIApp, timeout_s: float | None = None) -> None:
        super().__init__(app)
        self._timeout_s = (
            timeout_s
            if timeout_s is not None
            else float(os.getenv("REQUEST_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_S)))
        )

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[override]
        try:
            response: Response = await asyncio.wait_for(call_next(request), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=504,
                content={
                    "error": {
                        "type": "timeout",
                        "message": "Gateway Timeout",
                        "code": 504,
                    }
                },
            )
        return response
