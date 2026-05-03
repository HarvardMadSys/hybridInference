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
import copy
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from serving.adapters.anthropic_aliases import resolve_anthropic_alias
from serving.config.settings import has_role
from serving.exceptions import scrub_error_for_user
from serving.observability.metrics import (
    API_MODEL_REQUESTS,
    normalize_model_label,
    normalize_provider_label,
)
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import get_log_store, get_router
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
    and the default OpenRouter JSON shape for everything else. Pre-shaped
    error bodies (``exc.detail`` is a dict containing ``"error"``) are
    forwarded verbatim on every surface -- this preserves structured errors
    such as ``concurrency_limit_exceeded`` regardless of path. Matches the
    behaviour of the global handler installed by
    ``serving.servers.middleware.error.install_error_handlers``.
    """
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=dict(exc.headers or {}),
        )

    from serving.utils.errors import categorize_exception

    err_type = categorize_exception(exc)
    logger.error(
        "http_error",
        extra={
            "error_type": err_type,
            "status_code": exc.status_code,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=exc,
    )

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

    content = _build_error_response(str(exc.detail), code=exc.status_code, typ=err_type)
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


# --- Inbound header forwarding to upstream ---------------------------------

_FORWARDED_HEADERS = ("anthropic-beta",)


def _extract_forwarded_headers(request: Request) -> dict[str, str]:
    """Extract allowlisted Anthropic headers from the inbound request to forward upstream.

    Only headers in ``_FORWARDED_HEADERS`` are forwarded; auth-related headers
    (``x-api-key``, ``authorization``) are never forwarded because the adapter
    injects its own upstream credentials.
    """
    out: dict[str, str] = {}
    for k in _FORWARDED_HEADERS:
        v = request.headers.get(k)
        if v:
            out[k] = v
    return out


# --- Field sanitization for OpenAI backends --------------------------------


def _sanitize_for_openai_backend(body: dict[str, Any]) -> list[str]:
    """Strip Anthropic-only fields the OpenAI translator can't represent.

    Removes ``cache_control`` from content blocks and pops top-level fields
    (``thinking``, ``top_k``, ``container``) that have no OpenAI equivalent.
    Also detects unsupported ``metadata`` keys beyond ``user_id``.

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
    for k in ("thinking", "top_k", "container"):
        if k in body:
            body.pop(k)
            dropped.add(k)
    metadata = body.get("metadata") or {}
    extra_meta = set(metadata.keys()) - {"user_id"}
    if extra_meta:
        dropped.add(f"metadata.{','.join(sorted(extra_meta))}")
    return sorted(dropped)


# --- DB logging (fire-and-forget) ------------------------------------------


def _schedule_log_store_task(
    log_store,
    *,
    request_id: str,
    model_id: str,
    provider: str,
    usage: dict[str, int],
    latency_ms: int,
    status_code: int,
    pricing: dict[str, str],
    metadata: dict[str, Any],
    params: dict[str, Any],
    prompt: list[dict[str, Any]] | str | None = None,
    response: dict[str, Any] | str | None = None,
    ttft_ms: int | None = None,
) -> None:
    """Schedule a background log store task (fire-and-forget).

    ``usage`` is the upstream Anthropic usage shape with ``input_tokens`` /
    ``output_tokens`` (and optional ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``). To match OpenAI semantics used by the
    rest of the system (admin dashboard, completions logger, downstream
    metrics), ``prompt_tokens`` here is the *total* input including the
    cached subset (input_tokens + cache_read + cache_write). The cached
    subset is also stored in the dedicated ``cache_read_tokens`` /
    ``cache_write_tokens`` columns for separate billing; ``calculate_cost``
    subtracts the cached portion from ``prompt_tokens`` before applying
    ``prompt_price`` so cache is not double-billed.
    """
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    prompt_tokens = input_tokens + cache_read + cache_write
    total_tokens = prompt_tokens + output_tokens
    prompt_for_log: list[dict[str, Any]] | str = prompt if prompt is not None else []

    async def _log() -> None:
        try:
            await log_store.log_request(
                request_id=request_id,
                model_id=model_id,
                provider=provider,
                prompt=prompt_for_log,
                response=response,
                usage={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                },
                latency_ms=latency_ms,
                status_code=status_code,
                params=params,
                metadata=metadata,
                pricing=pricing,
                ttft_ms=ttft_ms,
            )
        except Exception:
            logger.debug(f"Background log store task failed for {request_id}", exc_info=True)

    asyncio.create_task(_log())  # noqa: RUF006


# --- Route handler ---------------------------------------------------------


@router.post("/v1/messages", response_model=None)
@router.post("/anthropic/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
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

    forwarded_headers = _extract_forwarded_headers(request)

    # Snapshot messages before _sanitize_for_openai_backend mutates them in-place
    # (strips cache_control blocks). The log must preserve the original client payload.
    messages_for_log = copy.deepcopy(body.get("messages"))

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

    params_for_log: dict[str, Any] = {"surface": "anthropic_messages"}
    for k in ("temperature", "top_p", "max_tokens", "stop_sequences", "stream"):
        if k in body:
            params_for_log[k] = body[k]
    if body.get("tools"):
        params_for_log["tool_count"] = len(body["tools"])

    is_streaming = bool(body.get("stream"))
    if is_streaming:
        from fastapi.responses import StreamingResponse

        sse_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }

        async def _gen():
            request_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
            stream_failed = False
            ttft_ms: int | None = None
            ttft_buffer = b""
            try:
                async for chunk in adapter.stream_messages(
                    body,
                    request_id=request_id,
                    usage_sink=request_usage,
                    extra_headers=forwarded_headers,
                ):
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    if ttft_ms is None:
                        ttft_buffer += chunk
                        nl = ttft_buffer.rfind(b"\n")
                        if nl >= 0:
                            head = ttft_buffer[: nl + 1]
                            ttft_buffer = ttft_buffer[nl + 1 :]
                            if b"event: content_block_delta" in head:
                                ttft_ms = int((time.time() - start) * 1000)
                                ttft_buffer = b""
                        elif len(ttft_buffer) > 16384:
                            ttft_buffer = b""
                    yield chunk
            except Exception as exc:
                stream_failed = True
                logger.exception(f"[{request_id}] Streaming dispatch failed")
                import json as _j

                err = {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": scrub_error_for_user(exc, request_id, 502),
                    },
                }
                yield f"event: error\ndata: {_j.dumps(err)}\n\n".encode()
            finally:
                latency_ms = int((time.time() - start) * 1000)
                status_code = 502 if stream_failed else 200
                API_MODEL_REQUESTS.labels(
                    model=normalize_model_label(canonical),
                    provider=normalize_provider_label(adapter.config.provider),
                    status_code=str(status_code),
                ).inc()
                if log_store:
                    _schedule_log_store_task(
                        log_store,
                        request_id=request_id,
                        model_id=canonical,
                        provider=adapter.config.provider,
                        usage=request_usage,
                        latency_ms=latency_ms,
                        status_code=status_code,
                        pricing=adapter.config.pricing
                        if hasattr(adapter.config, "pricing")
                        else {},
                        metadata=metadata,
                        params=params_for_log,
                        prompt=messages_for_log,
                        response=None,
                        ttft_ms=ttft_ms,
                    )

        return StreamingResponse(_gen(), media_type="text/event-stream", headers=sse_headers)

    try:
        resp = await adapter.messages(body, request_id=request_id, extra_headers=forwarded_headers)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))
    except Exception as exc:
        logger.exception(f"[{request_id}] Adapter messages() failed")
        return _anthropic_error(502, scrub_error_for_user(exc, request_id, 502))

    usage = (resp.get("usage") or {}) if isinstance(resp, dict) else {}
    usage_for_log = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0)),
    }
    latency_ms = int((time.time() - start) * 1000)
    provider = adapter.config.provider
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(canonical),
        provider=normalize_provider_label(provider),
        status_code="200",
    ).inc()
    if log_store:
        _schedule_log_store_task(
            log_store,
            request_id=request_id,
            model_id=canonical,
            provider=provider,
            usage=usage_for_log,
            latency_ms=latency_ms,
            status_code=200,
            pricing=adapter.config.pricing,
            metadata=metadata,
            params=params_for_log,
            prompt=messages_for_log,
            response=resp if isinstance(resp, dict) else None,
        )
    return JSONResponse(content=resp)
