"""Anthropic Messages API northbound router.

Serves both /v1/messages and /anthropic/v1/messages via two decorators on the
same handler. Replaces the old anthropic_proxy.py (claude_sub-only) which is
deleted in Task 16.

This task (Task 12) covers the non-streaming path. Streaming dispatch lands
in Task 13.

Field translation lives in adapter.messages() / adapter.stream_messages();
this router only owns:
  - auth, rate limiting, concurrency
  - model resolution (with Anthropic alias map)
  - field sanitization for OpenAI-backed dispatch
  - error formatting (Anthropic shape)
  - DB logging + metrics
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from serving.adapters.anthropic_aliases import resolve_anthropic_alias
from serving.config.settings import has_role
from serving.observability.metrics import (
    API_MODEL_REQUESTS,
    normalize_model_label,
    normalize_provider_label,
)
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import get_db_logger, get_rate_limiter, get_router
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

logger = get_logger(__name__)
router = APIRouter()


# --- Anthropic-format error envelope ---------------------------------------

_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
}


def _anthropic_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {"type": _ERROR_TYPE_BY_STATUS.get(status, "api_error"), "message": message},
        },
    )


_ANTHROPIC_PATHS = ("/v1/messages", "/anthropic/")


async def anthropic_aware_http_exception_handler(request: Request, exc: HTTPException):
    """Path-aware HTTP exception handler.

    Emits Anthropic-format errors for requests against the Anthropic surfaces,
    and the default OpenRouter JSON shape for everything else.
    """
    path = request.url.path
    if any(path.startswith(p) for p in _ANTHROPIC_PATHS):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": "error",
                "error": {
                    "type": _ERROR_TYPE_BY_STATUS.get(exc.status_code, "api_error"),
                    "message": str(exc.detail),
                },
            },
            headers=dict(exc.headers or {}),
        )
    # Non-Anthropic paths: produce the same OpenRouter shape as install_error_handlers.
    from serving.servers.middleware.error import _build_error_response
    from serving.utils.errors import categorize_exception

    content = _build_error_response(
        str(exc.detail), code=exc.status_code, typ=categorize_exception(exc)
    )
    return JSONResponse(
        status_code=exc.status_code, content=content, headers=dict(exc.headers or {})
    )


# --- Model resolution ------------------------------------------------------


def _resolve(model_id: str, router_exec, user_ctx: dict | None):
    """Return (canonical_model_id, route, adapter)."""
    canonical = resolve_anthropic_alias(model_id)
    route = router_exec.routes.get(canonical)
    if route is None:
        raise HTTPException(404, f"Model '{model_id}' not found")
    required = route.required_role or ("admin" if route.admin_only else "free")
    user_role = (user_ctx or {}).get("role", "free")
    if not has_role(user_role, required):
        raise HTTPException(404, f"Model '{model_id}' not found")
    if not route.adapters:
        raise HTTPException(404, f"Model '{model_id}' has no adapters")
    adapter, _ = route.adapters[0]
    return canonical, route, adapter


# --- Field sanitization for OpenAI backends --------------------------------


def _sanitize_for_openai_backend(body: dict[str, Any]) -> list[str]:
    """Strip Anthropic-only block fields the OpenAI translator can't represent.

    Returns sorted list of dropped-field names for warning logging.
    """
    dropped: set[str] = set()
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    block.pop("cache_control")
                    dropped.add("cache_control")
    if "thinking" in body:
        body.pop("thinking")
        dropped.add("thinking")
    return sorted(dropped)


# --- DB logging (fire-and-forget) ------------------------------------------


def _schedule_db_log(
    db_logger,
    *,
    request_id: str,
    model_id: str,
    provider: str,
    usage: dict[str, int],
    latency_ms: int,
    status_code: int,
    pricing: dict[str, str],
    metadata: dict[str, Any],
) -> None:
    """Schedule a background DB log task (fire-and-forget)."""

    async def _log() -> None:
        try:
            await db_logger.log_request(
                request_id=request_id,
                model_id=model_id,
                provider=provider,
                prompt=[],
                response=None,
                usage={
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                },
                latency_ms=latency_ms,
                status_code=status_code,
                params={"surface": "anthropic_messages"},
                metadata=metadata,
                pricing=pricing,
            )
        except Exception:
            logger.debug(f"Background DB log failed for {request_id}", exc_info=True)

    asyncio.create_task(_log())  # noqa: RUF006


# --- Rate-limit pre-estimate flatten ---------------------------------------


def _flatten_anthropic_for_token_estimate(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Approximate OpenAI-shaped messages for tiktoken pre-estimation."""
    out = []
    sys = body.get("system")
    if isinstance(sys, str) and sys:
        out.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        out.append(
            {
                "role": "system",
                "content": "\n\n".join(
                    b.get("text", "")
                    for b in sys
                    if isinstance(b, dict) and b.get("type") == "text"
                ),
            }
        )
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
        elif isinstance(content, list):
            text = "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            out.append({"role": msg["role"], "content": text})
    return out


# --- Route handler ---------------------------------------------------------


@router.post("/v1/messages", response_model=None)
@router.post("/anthropic/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    rate_limiter=Depends(get_rate_limiter),
    db_logger=Depends(get_db_logger),
    _conc=Depends(enforce_user_concurrency),
):
    """Handle Anthropic Messages API requests (non-streaming)."""
    request_id = f"amsg_{int(time.time() * 1_000_000)}"
    start = time.time()

    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Invalid JSON in request body")

    model_id = body.get("model")
    if not model_id:
        return _anthropic_error(400, "Missing required field: model")
    if "messages" not in body:
        return _anthropic_error(400, "Missing required field: messages")
    if "max_tokens" not in body:
        return _anthropic_error(400, "Missing required field: max_tokens")

    try:
        canonical, _route, adapter = _resolve(model_id, router_exec, user_ctx)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))

    body["model"] = canonical

    if rate_limiter:
        oai_msgs = _flatten_anthropic_for_token_estimate(body)
        ok, meta = await rate_limiter.acquire_tokens(
            model_id=canonical,
            messages=oai_msgs,
            max_tokens=body.get("max_tokens"),
            priority=1 if user_ctx.get("authenticated") else 0,
            timeout=30.0,
        )
        if not ok:
            return _anthropic_error(429, meta.get("error", "Rate limit exceeded"))

    if adapter.native_format == "openai":
        dropped = _sanitize_for_openai_backend(body)
        if dropped:
            logger.warning(
                f"[{request_id}] Dropped Anthropic-only fields for OpenAI backend: {dropped}"
            )

    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "ip": get_client_ip(request),
        "authenticated": bool(user_ctx.get("authenticated")),
        "user_id": user_ctx.get("user_id"),
        "surface": "anthropic_messages",
        "alias_input": model_id if model_id != canonical else None,
    }

    is_streaming = bool(body.get("stream"))
    if is_streaming:
        from fastapi.responses import StreamingResponse

        sse_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }

        async def _gen():
            usage = {"input_tokens": 0, "output_tokens": 0}
            stream_failed = False
            try:
                async for chunk in adapter.stream_messages(body, request_id=request_id):
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    yield chunk
            except Exception as exc:
                stream_failed = True
                logger.exception(f"[{request_id}] Streaming dispatch failed")
                import json as _j

                err = {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"Stream interrupted: {exc}"},
                }
                yield f"event: error\ndata: {_j.dumps(err)}\n\n".encode()
            finally:
                if hasattr(adapter, "last_stream_usage"):
                    usage = adapter.last_stream_usage
                latency_ms = int((time.time() - start) * 1000)
                status_code = 502 if stream_failed else 200
                API_MODEL_REQUESTS.labels(
                    model=normalize_model_label(canonical),
                    provider=normalize_provider_label(adapter.config.provider),
                    status_code=str(status_code),
                ).inc()
                if db_logger:
                    _schedule_db_log(
                        db_logger,
                        request_id=request_id,
                        model_id=canonical,
                        provider=adapter.config.provider,
                        usage=usage,
                        latency_ms=latency_ms,
                        status_code=status_code,
                        pricing=adapter.config.pricing
                        if hasattr(adapter.config, "pricing")
                        else {},
                        metadata=metadata,
                    )

        return StreamingResponse(_gen(), media_type="text/event-stream", headers=sse_headers)

    try:
        resp = await adapter.messages(body, request_id=request_id)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))
    except Exception as exc:
        logger.exception(f"[{request_id}] Adapter messages() failed")
        return _anthropic_error(502, f"Upstream error: {exc}")

    usage = (resp.get("usage") or {}) if isinstance(resp, dict) else {}
    usage_for_log = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }
    latency_ms = int((time.time() - start) * 1000)
    provider = adapter.config.provider
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(canonical),
        provider=normalize_provider_label(provider),
        status_code="200",
    ).inc()
    if db_logger:
        _schedule_db_log(
            db_logger,
            request_id=request_id,
            model_id=canonical,
            provider=provider,
            usage=usage_for_log,
            latency_ms=latency_ms,
            status_code=200,
            pricing=adapter.config.pricing,
            metadata=metadata,
        )
    return JSONResponse(content=resp)
