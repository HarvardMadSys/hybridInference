"""Global exception handlers that produce OpenRouter-style error bodies."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from serving.schemas import ErrorDetail, ErrorResponse


def _build_error_response(
    message: str,
    *,
    code: int | None = None,
    typ: str = "server_error",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standardized error response payload.

    Args:
        message: Human-readable error message.
        code: Optional HTTP status code associated with the error.
        typ: Error type identifier.
        extra: Optional additional fields to include under ``error``.

    Returns:
        Dict[str, Any]: Serialized error object conforming to OpenRouter style.
    """
    detail = ErrorDetail(type=typ, message=message, code=code, **(extra or {}))
    return ErrorResponse(error=detail).model_dump()


def install_error_handlers(app: FastAPI) -> None:
    """Install global exception handlers that return OpenRouter-like errors."""

    @app.exception_handler(HTTPException)
    async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
        # If detail already shaped like our error, forward as-is
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code, content=exc.detail, headers=exc.headers
            )

        content = _build_error_response(str(exc.detail), code=exc.status_code)
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(Exception)
    async def any_exc_handler(request: Request, exc: Exception) -> JSONResponse:
        content = _build_error_response(str(exc), code=500)
        return JSONResponse(status_code=500, content=content)

    # As a defensive fallback, also install an HTTP middleware that catches any
    # exceptions that might bypass the exception handlers in certain testing
    # transports or edge cases, ensuring a consistent JSON error response.
    @app.middleware("http")
    async def catch_all_errors(request: Request, call_next: Any) -> Any:
        try:
            return await call_next(request)
        except HTTPException as exc:
            # Mirror the HTTPException handler behavior.
            if isinstance(exc.detail, dict) and "error" in exc.detail:
                return JSONResponse(
                    status_code=exc.status_code, content=exc.detail, headers=exc.headers
                )
            content = _build_error_response(str(exc.detail), code=exc.status_code)
            return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)
        except Exception as exc:  # pragma: no cover - exercised in integration test
            content = _build_error_response(str(exc), code=500)
            return JSONResponse(status_code=500, content=content)
