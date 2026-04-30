"""Request timeout middleware.

Enforces a per-request asyncio timeout, returning 504 Gateway Timeout when
exceeded. Read once at import time from ``REQUEST_TIMEOUT_SECONDS``
(default 120s).
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request, Response


_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "120"))


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Cancel requests that exceed ``REQUEST_TIMEOUT_SECONDS`` and return 504."""

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[override]
        try:
            response: Response = await asyncio.wait_for(call_next(request), timeout=_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Gateway Timeout") from None
        return response
