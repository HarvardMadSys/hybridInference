from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from utils import request_context as req_ctx


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach/propagate an ``X-Request-ID`` header and expose in request.state."""

    header_name = "X-Request-ID"

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[override]
        req_id = request.headers.get(self.header_name) or secrets.token_hex(12)
        request.state.request_id = req_id
        # seed request context
        req_ctx.update({"request_id": req_id})
        response: Response = await call_next(request)
        response.headers[self.header_name] = req_id
        return response
