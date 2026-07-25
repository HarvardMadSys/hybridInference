"""HTTP request logging middleware."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.requests import Request

from serving.utils import context as req_ctx
from serving.utils.logging import _QUIET_PATHS, get_logger
from serving.utils.request_ip import get_client_ip_info

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = get_logger(__name__)


class RequestLogMiddleware:
    """Emit a concise structured log per HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Emit a structured log after each HTTP request completes."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        start = time.perf_counter()
        status_code = 500
        exc_to_raise: Exception | None = None

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            exc_to_raise = e
            if hasattr(e, "status_code"):
                status_code = e.status_code
            elif "timeout" in str(type(e).__name__).lower():
                status_code = 504
            else:
                status_code = 500

        duration_ms = int((time.perf_counter() - start) * 1000)
        ctx = req_ctx.get()
        ip_info = get_client_ip_info(request)
        remote_ip = ip_info.client_ip
        user_agent = request.headers.get("user-agent")
        host = request.headers.get("host")
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        request_id = ctx.get("request_id")
        canonical_session_id = request.headers.get("X-Session-ID")

        log_extra = {
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "model": ctx.get("model"),
            "provider": ctx.get("provider"),
            "remote_ip": remote_ip,
            "peer_ip": ip_info.peer_ip,
            "ip_source": ip_info.source,
            "x_forwarded_for": ip_info.x_forwarded_for,
            "x_real_ip": ip_info.x_real_ip,
            "cf_connecting_ip": ip_info.cf_connecting_ip,
            "cf_connecting_ipv6": ip_info.cf_connecting_ipv6,
            "user_agent": user_agent,
            "host": host,
            "origin": origin,
            "referer": referer,
            "request_id": request_id,
            "session_id": canonical_session_id,
            # Gateway-generated client-error tag (e.g. model-not-found), set by the
            # handler via req_ctx so failure-rate alerts can exclude user-driven 404s.
            "client_error_kind": ctx.get(req_ctx.CLIENT_ERROR_KIND),
        }
        is_quiet_path = request.url.path in _QUIET_PATHS
        is_synthetic_probe = request.headers.get("x-probe", "").lower() == "synthetic"

        is_auth_challenge = status_code == 401

        if exc_to_raise:
            log_extra["error"] = str(exc_to_raise)
            log_extra["error_type"] = type(exc_to_raise).__name__
            logger.error("http_request", extra=log_extra)
        elif is_quiet_path or is_synthetic_probe or is_auth_challenge:
            logger.debug("http_request", extra=log_extra)
        else:
            logger.info("http_request", extra=log_extra)

        if logger.isEnabledFor(10):
            masked_headers: dict[str, str] = {}
            for k, v in request.headers.items():
                key_lower = k.lower()
                if key_lower in {"authorization", "x-api-key"}:
                    masked_headers[k] = "***"
                else:
                    val = v if v is not None else ""
                    masked_headers[k] = (val[:256] + "…") if len(val) > 256 else val
            logger.debug("http_request_headers", extra={"headers": masked_headers})

        if exc_to_raise:
            raise exc_to_raise
