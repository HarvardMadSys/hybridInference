"""Middleware to attach and propagate X-Request-ID header."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_HEADER_NAME = "x-request-id"
_HEADER_BYTES = b"x-request-id"


class RequestIdMiddleware:
    """Attach/propagate an ``X-Request-ID`` header and expose in request.state."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        req_id: str | None = None
        for name, value in scope.get("headers", []):
            if name == _HEADER_BYTES:
                req_id = value.decode("latin-1")
                break
        if not req_id:
            req_id = secrets.token_hex(12)

        scope.setdefault("state", {})["request_id"] = req_id
        req_ctx.update({"request_id": req_id})

        req_id_bytes = req_id.encode("latin-1")

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((_HEADER_BYTES, req_id_bytes))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)
