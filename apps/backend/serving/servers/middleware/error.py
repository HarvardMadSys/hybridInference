"""Global exception handlers that produce OpenRouter-style error bodies."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from serving.exceptions import scrub_error_for_user
from serving.schemas import ErrorDetail, ErrorResponse
from serving.utils.errors import categorize_exception
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = get_logger(__name__)


def _build_error_response(
    message: str,
    *,
    code: int | None = None,
    typ: str = "server_error",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standardized error response payload."""
    detail = ErrorDetail(type=typ, message=message, code=code, **(extra or {}))
    return ErrorResponse(error=detail).model_dump()


class FallbackErrorMiddleware:
    """Catch unhandled exceptions and return structured JSON 500 responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Catch unhandled exceptions and return structured JSON 500 responses."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except HTTPException:
            raise
        except Exception as exc:
            if response_started:
                logger.error(
                    "unhandled_exception_after_response_start",
                    extra={
                        "error_type": categorize_exception(exc),
                        "path": scope.get("path", ""),
                        "method": scope.get("method", ""),
                    },
                    exc_info=exc,
                )
                raise

            err_type = categorize_exception(exc)
            request_id = scope.get("state", {}).get("request_id")
            logger.error(
                "unhandled_exception",
                extra={
                    "error_type": err_type,
                    "path": scope.get("path", ""),
                    "method": scope.get("method", ""),
                },
                exc_info=exc,
            )
            user_msg = scrub_error_for_user(exc, request_id, 500)
            content = _build_error_response(user_msg, code=500, typ=err_type)
            body = json.dumps(content).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
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


def install_error_handlers(app: FastAPI) -> None:
    """Install global exception handlers that return OpenRouter-like errors."""

    @app.exception_handler(HTTPException)
    async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code, content=exc.detail, headers=exc.headers
            )
        err_type = categorize_exception(exc)
        log_fn = logger.error if exc.status_code >= 500 else logger.warning
        log_fn(
            "http_error",
            extra={
                "error_type": err_type,
                "status_code": exc.status_code,
                "path": request.url.path,
                "method": request.method,
            },
            exc_info=exc if exc.status_code >= 500 else None,
        )
        content = _build_error_response(str(exc.detail), code=exc.status_code, typ=err_type)
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(Exception)
    async def any_exc_handler(request: Request, exc: Exception) -> JSONResponse:
        err_type = categorize_exception(exc)
        request_id = getattr(request.state, "request_id", None)
        logger.error(
            "unhandled_exception",
            extra={
                "error_type": err_type,
                "path": request.url.path,
                "method": request.method,
            },
            exc_info=exc,
        )
        user_msg = scrub_error_for_user(exc, request_id, 500)
        content = _build_error_response(user_msg, code=500, typ=err_type)
        return JSONResponse(status_code=500, content=content)
