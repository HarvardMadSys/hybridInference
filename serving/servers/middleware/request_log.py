from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request, Response

logger = get_logger(__name__)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Emit a concise structured log per HTTP request."""

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[override]
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)
        ctx = req_ctx.get()
        logger.info(
            "http_request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": getattr(response, "status_code", 0),
                "duration_ms": duration_ms,
                "model": ctx.get("model"),
                "provider": ctx.get("provider"),
            },
        )
        return response
