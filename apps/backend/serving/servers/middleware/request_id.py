"""Middleware to attach and propagate X-Request-ID header."""

from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import TYPE_CHECKING

from serving.pricing import get_pricing_snapshot
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_HEADER_BYTES = b"x-request-id"
_USER_AGENT_BYTES = b"user-agent"

#: A request id the client may choose: up to 128 visible ASCII characters, the
#: shape the admin alert-history lookup accepts. The id is logged on every line
#: the request writes, echoed in its response, put in its error bodies and its
#: alerts; anything else is replaced by one the gateway mints, rather than
#: carried into all of those at whatever length the client sent.
_CLIENT_REQUEST_ID = re.compile(r"[\x21-\x7e]{1,128}")


class RequestIdMiddleware:
    """Attach/propagate an ``X-Request-ID`` header and expose in request.state."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Attach request ID, seed context, and inject header into response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_id: str | None = None
        user_agent: str | None = None
        for name, value in scope.get("headers", []):
            if name == _HEADER_BYTES:
                client_id = value.decode("latin-1")
            elif name == _USER_AGENT_BYTES:
                user_agent = value.decode("latin-1")
        if client_id is not None and not _CLIENT_REQUEST_ID.fullmatch(client_id):
            client_id = None
        req_id = client_id or secrets.token_hex(12)

        scope.setdefault("state", {})["request_id"] = req_id
        # Clear every per-request key before the request runs, so a request that
        # doesn't populate one cannot inherit a prior request's value when the
        # same task handles sequential scopes. The set lives in
        # ``req_ctx.REQUEST_SCOPED_KEYS`` rather than being spelled out here:
        # each key exists because some consumer reads "key present" as a fact
        # about the current request (``user_id``/``user_name`` for
        # circuit-breaker attribution, ``client_error_kind`` for the 404 split,
        # ``provider`` for the 401 split), so the reset has to grow with that set
        # and one list is easier to keep complete than a literal in a middleware.
        req_ctx.reset_request_scope(
            request_id=req_id,
            # Set when the id is the client's own choice: then it is not
            # unique, and nothing may key a stored row by it.
            client_request_id=client_id,
            client_user_agent=user_agent or None,
            pricing_time=dt.datetime.now(dt.UTC),
            pricing_snapshot=get_pricing_snapshot(),
        )

        req_id_bytes = req_id.encode("latin-1")

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != _HEADER_BYTES
                ]
                headers.append((_HEADER_BYTES, req_id_bytes))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)
