"""HTTP request logging middleware."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from serving.utils import context as req_ctx
from serving.utils.logging import _QUIET_PATHS, get_logger
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request, Response

logger = get_logger(__name__)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Emit a concise structured log per HTTP request."""

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[override]
        """Process request and emit structured log with status code and timing."""
        start = time.perf_counter()
        status_code = 500  # Default to 500 if we never get a response
        response: Response | None = None
        exc_to_raise: Exception | None = None

        try:
            response = await call_next(request)
            status_code = getattr(response, "status_code", 0)
        except Exception as e:
            # Capture exception info for logging, but still re-raise
            exc_to_raise = e
            # Try to determine status code from exception
            if hasattr(e, "status_code"):
                status_code = e.status_code
            elif "timeout" in str(type(e).__name__).lower():
                status_code = 504
            else:
                status_code = 500

        duration_ms = int((time.perf_counter() - start) * 1000)
        ctx = req_ctx.get()
        # Enrich logs to help identify misrouted or unexpected callers.
        # Note: ``request.client.host`` will be the proxy's IP (e.g., NGINX). The
        # original client should be available via ``X-Forwarded-For`` when the
        # proxy sets it.
        remote_ip = get_client_ip(request)
        xff = request.headers.get("x-forwarded-for")
        user_agent = request.headers.get("user-agent")
        host = request.headers.get("host")
        request_id = ctx.get("request_id")
        # Extract canonical session_id for logs (same as DB metadata).
        canonical_session_id = request.headers.get("X-Session-ID")

        log_extra = {
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "model": ctx.get("model"),
            "provider": ctx.get("provider"),
            "remote_ip": remote_ip,
            "x_forwarded_for": xff,
            "user_agent": user_agent,
            "host": host,
            "request_id": request_id,
            "session_id": canonical_session_id,
        }
        is_quiet_path = request.url.path in _QUIET_PATHS
        is_synthetic_probe = request.headers.get("x-probe", "").lower() == "synthetic"

        if exc_to_raise:
            log_extra["error"] = str(exc_to_raise)
            log_extra["error_type"] = type(exc_to_raise).__name__
            logger.error("http_request", extra=log_extra)
        elif is_quiet_path or is_synthetic_probe:
            logger.debug("http_request", extra=log_extra)
        else:
            logger.info("http_request", extra=log_extra)

        # Debug-only: emit a compact headers snapshot with sensitive fields masked.
        # This helps diagnose whether upstream clients (e.g., Cursor) include
        # conversation/session identifiers without flooding logs.
        if logger.isEnabledFor(10):  # logging.DEBUG
            masked_headers: dict[str, str] = {}
            for k, v in request.headers.items():
                key_lower = k.lower()
                if key_lower in {"authorization", "x-api-key"}:
                    masked_headers[k] = "***"
                else:
                    # Truncate very long header values to keep logs readable.
                    val = v if v is not None else ""
                    masked_headers[k] = (val[:256] + "…") if len(val) > 256 else val
            logger.debug("http_request_headers", extra={"headers": masked_headers})

        # Re-raise the exception after logging
        if exc_to_raise:
            raise exc_to_raise

        return response  # type: ignore[return-value]
