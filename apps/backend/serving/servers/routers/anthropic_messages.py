"""Anthropic Messages API northbound router.

Serves both /v1/messages and /anthropic/v1/messages via two decorators on the
same handler.

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
import json
import time
from typing import Any

import aiohttp
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
from serving.observability.rejection_log import log_rejection
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
    error: str | None = None,
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
                error=error,
            )
        except Exception:
            logger.debug(f"Background log store task failed for {request_id}", exc_info=True)

    asyncio.create_task(_log())  # noqa: RUF006


# --- Anthropic SSE accumulator ---------------------------------------------


def _apply_sse_event(acc: dict | None, event_type: str, data: str) -> dict | None:
    """Update *acc* in-place given one Anthropic SSE event; return the (possibly new) accumulator.

    Best-effort: any malformed event is swallowed so streaming logging never
    impacts the forwarded client stream.
    """
    if event_type in ("ping", "message_stop", "error", ""):
        return acc
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return acc
    if not isinstance(payload, dict):
        return acc

    try:
        if event_type == "message_start":
            msg = payload.get("message") or {}
            if not isinstance(msg, dict):
                return acc
            return {
                "id": msg.get("id"),
                "type": "message",
                "role": msg.get("role", "assistant"),
                "model": msg.get("model"),
                "content": list(msg.get("content") or []),
                "stop_reason": msg.get("stop_reason"),
                "stop_sequence": msg.get("stop_sequence"),
                "usage": dict(msg.get("usage") or {}),
            }

        if acc is None:
            return acc

        if event_type == "content_block_start":
            idx = payload.get("index")
            block_in = payload.get("content_block")
            if not isinstance(idx, int) or not isinstance(block_in, dict):
                return acc
            block = dict(block_in)
            if block.get("type") == "text":
                block.setdefault("text", "")
            elif block.get("type") == "tool_use":
                block.setdefault("input", {})
                block["_partial_json"] = ""
            content = acc["content"]
            while len(content) <= idx:
                content.append(None)
            content[idx] = block

        elif event_type == "content_block_delta":
            idx = payload.get("index")
            delta = payload.get("delta") or {}
            if not isinstance(idx, int) or not isinstance(delta, dict):
                return acc
            content = acc["content"]
            if 0 <= idx < len(content) and isinstance(content[idx], dict):
                block = content[idx]
                dtype = delta.get("type")
                if dtype == "text_delta":
                    block["text"] = block.get("text", "") + (delta.get("text") or "")
                elif dtype == "input_json_delta":
                    block["_partial_json"] = block.get("_partial_json", "") + (
                        delta.get("partial_json") or ""
                    )
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + (delta.get("thinking") or "")

        elif event_type == "content_block_stop":
            idx = payload.get("index")
            if not isinstance(idx, int):
                return acc
            content = acc["content"]
            if 0 <= idx < len(content) and isinstance(content[idx], dict):
                _finalize_block(content[idx])

        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            if isinstance(delta, dict):
                if "stop_reason" in delta:
                    acc["stop_reason"] = delta["stop_reason"]
                if "stop_sequence" in delta:
                    acc["stop_sequence"] = delta["stop_sequence"]
            extra_usage = payload.get("usage") or {}
            if isinstance(extra_usage, dict):
                if "output_tokens" in extra_usage:
                    acc["usage"]["output_tokens"] = extra_usage["output_tokens"]
                for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                    if k in extra_usage:
                        acc["usage"][k] = extra_usage[k]
    except Exception:
        # Logging must never disrupt the forwarded stream; drop this event.
        pass

    return acc


def _finalize_block(block: dict) -> None:
    """Resolve any partial-JSON buffer on a tool_use block; remove sentinel keys."""
    if block.get("type") == "tool_use" and "_partial_json" in block:
        raw = block.pop("_partial_json")
        try:
            block["input"] = json.loads(raw) if raw else block.get("input") or {}
        except (json.JSONDecodeError, ValueError):
            if raw:
                block["input"] = raw  # type: ignore[assignment]


def _finalize_response_acc(acc: dict | None) -> dict | None:
    """Normalize the accumulator for persistence: finalize partial blocks, drop sentinels."""
    if not isinstance(acc, dict):
        return acc
    for block in acc.get("content") or []:
        if isinstance(block, dict):
            _finalize_block(block)
    return acc


def _log_failure(
    log_store,
    *,
    request_id: str,
    canonical: str,
    adapter,
    metadata: dict[str, Any],
    params_for_log: dict[str, Any],
    messages_for_log,
    start: float,
    status_code: int,
    error_message: str,
) -> None:
    latency_ms = int((time.time() - start) * 1000)
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
            usage={},
            latency_ms=latency_ms,
            status_code=status_code,
            pricing=adapter.config.pricing,
            metadata=metadata,
            params=params_for_log,
            prompt=messages_for_log,
            response=None,
            error=error_message,
        )


_SSE_LEFTOVER_CAP = 65536


def _parse_sse_chunk(buffer: bytes, raw: bytes) -> tuple[list[tuple[str, str]], bytes]:
    r"""Extract complete SSE events from *buffer* + *raw*; return (events, leftover).

    Events are delimited by a blank line; both ``\n\n`` and ``\r\n\r\n``
    are recognized. The trailing partial event is returned as *leftover* so the
    caller can prepend it to the next chunk. Leftover is capped at
    ``_SSE_LEFTOVER_CAP`` bytes; a malformed stream without separators will be
    discarded rather than grow without bound.
    """
    buffer = buffer + raw
    events: list[tuple[str, str]] = []
    pos = 0
    while True:
        sep_n = buffer.find(b"\n\n", pos)
        sep_r = buffer.find(b"\r\n\r\n", pos)
        if sep_n >= 0 and (sep_r < 0 or sep_n < sep_r):
            sep, sep_len = sep_n, 2
        elif sep_r >= 0:
            sep, sep_len = sep_r, 4
        else:
            break
        block = buffer[pos:sep].decode("utf-8", errors="replace").strip()
        pos = sep + sep_len
        if not block:
            continue
        event_type = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            events.append((event_type, "\n".join(data_lines)))
    leftover = buffer[pos:]
    if len(leftover) > _SSE_LEFTOVER_CAP:
        leftover = b""
    return events, leftover


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
        services = getattr(request.app.state, "services", None)
        log_store_ = getattr(services, "log_store", None) if services else None
        runtime_settings_ = getattr(services, "runtime_settings", None) if services else None
        asyncio.create_task(
            log_rejection(
                log_store=log_store_,
                runtime_settings=runtime_settings_,
                request=request,
                status_code=exc.status_code,
                error_code="model_not_found",
                reason=str(exc.detail),
                user={
                    "user_id": user_ctx.get("user_id"),
                    "role": user_ctx.get("role"),
                },
                model_id=model_id,
            )
        )
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
            stream_status_code: int = 200
            stream_error_message: str | None = None
            ttft_ms: int | None = None
            ttft_buffer = b""
            response_acc: dict | None = None
            sse_buffer = b""
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
                    events, sse_buffer = _parse_sse_chunk(sse_buffer, chunk)
                    for event_type, data in events:
                        response_acc = _apply_sse_event(response_acc, event_type, data)
                    yield chunk
            except aiohttp.ClientResponseError as exc:
                stream_failed = True
                stream_status_code = exc.status
                stream_error_message = scrub_error_for_user(exc, request_id, exc.status)
                logger.exception(f"[{request_id}] Streaming dispatch failed")
                err = {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": stream_error_message,
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
            except Exception as exc:
                stream_failed = True
                stream_status_code = 502
                stream_error_message = scrub_error_for_user(exc, request_id, 502)
                logger.exception(f"[{request_id}] Streaming dispatch failed")
                err = {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": stream_error_message,
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
            finally:
                latency_ms = int((time.time() - start) * 1000)
                API_MODEL_REQUESTS.labels(
                    model=normalize_model_label(canonical),
                    provider=normalize_provider_label(adapter.config.provider),
                    status_code=str(stream_status_code),
                ).inc()
                if log_store:
                    _schedule_log_store_task(
                        log_store,
                        request_id=request_id,
                        model_id=canonical,
                        provider=adapter.config.provider,
                        usage=request_usage,
                        latency_ms=latency_ms,
                        status_code=stream_status_code,
                        pricing=adapter.config.pricing,
                        metadata=metadata,
                        params=params_for_log,
                        prompt=messages_for_log,
                        response=_finalize_response_acc(response_acc),
                        ttft_ms=ttft_ms,
                        error=stream_error_message if stream_failed else None,
                    )

        return StreamingResponse(_gen(), media_type="text/event-stream", headers=sse_headers)

    try:
        resp = await adapter.messages(body, request_id=request_id, extra_headers=forwarded_headers)
    except HTTPException as exc:
        error_message = str(exc.detail)
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            start=start,
            status_code=exc.status_code,
            error_message=error_message,
        )
        return _anthropic_error(exc.status_code, error_message)
    except aiohttp.ClientResponseError as exc:
        error_message = scrub_error_for_user(exc, request_id, exc.status)
        logger.exception(f"[{request_id}] Adapter messages() failed")
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            start=start,
            status_code=exc.status,
            error_message=error_message,
        )
        return _anthropic_error(exc.status, error_message)
    except Exception as exc:
        error_message = scrub_error_for_user(exc, request_id, 502)
        logger.exception(f"[{request_id}] Adapter messages() failed")
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            start=start,
            status_code=502,
            error_message=error_message,
        )
        return _anthropic_error(502, error_message)

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
