"""Anthropic Messages API northbound surface.

Identity surface translator: both client and upstream speak the Anthropic
Messages API, so format translation is trivially the identity function.
See docs/anthropic-proxy-design.md for full design rationale.

Responsibilities:
1. Client auth via ``verify_api_key`` dependency
2. Model resolution (public ID → provider_model_id) with provider eligibility
3. Credential injection from shared ``AccountPool``
4. Raw byte forwarding (streaming) or JSON forwarding (non-streaming)
5. Best-effort usage extraction for DB cost logging
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from serving.adapters.claude_pool import get_shared_pool
from serving.adapters.claude_sub import _ANTHROPIC_BETA, _ANTHROPIC_VERSION
from serving.adapters.codex_token import NoHealthyAccountError
from serving.config.settings import has_role
from serving.observability.metrics import (
    API_MODEL_REQUESTS,
    normalize_model_label,
    normalize_provider_label,
)
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import get_db_logger, get_router
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.executor import RouteExecutor
    from serving.storage.database import DatabaseLogger

logger = get_logger(__name__)
router = APIRouter()

_UPSTREAM_BASE = "https://api.anthropic.com"
_UPSTREAM_MESSAGES_URL = f"{_UPSTREAM_BASE}/v1/messages?beta=true"
_PROVIDER_NAME = "claude_sub"

# Required system prompt prefix for OAuth subscription access to Sonnet/Opus.
_REQUIRED_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


# ------------------------------------------------------------------
# Model resolution
# ------------------------------------------------------------------


def _resolve_model(
    model_id: str,
    router_exec: RouteExecutor,
    user_ctx: dict | None = None,
) -> tuple[str, dict[str, str]]:
    """Map a public model ID to its upstream provider_model_id.

    Returns:
        (provider_model_id, pricing_dict)

    Raises:
        HTTPException 404: model not found, not eligible, or admin-only.
    """
    route = router_exec.routes.get(model_id)
    if route is None:
        raise HTTPException(404, f"Model '{model_id}' not found")

    required = route.required_role or ("admin" if route.admin_only else "free")
    user_role = (user_ctx or {}).get("role", "free")
    if not has_role(user_role, required):
        raise HTTPException(404, f"Model '{model_id}' not found")

    for adapter, _ in route.adapters:
        cfg = getattr(adapter, "config", None)
        if cfg is None:
            continue
        if cfg.provider != _PROVIDER_NAME:
            continue
        upstream_model = cfg.provider_model_id or cfg.id
        return upstream_model, cfg.pricing

    raise HTTPException(
        404,
        f"Model '{model_id}' is not eligible for /anthropic/v1/messages "
        f"(requires provider={_PROVIDER_NAME})",
    )


# ------------------------------------------------------------------
# Upstream header builder
# ------------------------------------------------------------------


def _build_upstream_headers(token: str, *, streaming: bool) -> dict[str, str]:
    """Build headers for the upstream Anthropic request."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Anthropic-Version": _ANTHROPIC_VERSION,
        "Anthropic-Beta": _ANTHROPIC_BETA,
        "Anthropic-Dangerous-Direct-Browser-Access": "true",
        "X-App": "cli",
        "User-Agent": "claude-cli/2.1.63 (external, cli)",
    }
    if streaming:
        headers["Accept"] = "text/event-stream"
        headers["Accept-Encoding"] = "identity"
    else:
        headers["Accept"] = "application/json"
    return headers


# ------------------------------------------------------------------
# Streaming usage extraction
# ------------------------------------------------------------------


def _extract_usage_from_sse(raw: bytes, usage: dict[str, int]) -> None:
    """Best-effort parse of SSE frames to extract usage counters.

    Mutates *usage* in-place.  Never raises — failures are silently ignored
    so that the forwarded stream is never affected.

    Anthropic sends **cumulative** values, not per-event deltas:
    - ``message_start.message.usage.input_tokens`` — total input tokens
    - ``message_delta.usage.output_tokens``         — total output tokens so far

    We therefore *assign* (not accumulate) each value, matching the
    semantics in ``claude_sub.py`` (lines 359-362).
    """
    try:
        text = raw.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            obj = json.loads(payload)
            event_type = obj.get("type", "")

            if event_type == "message_start":
                msg_usage = obj.get("message", {}).get("usage", {})
                usage["input_tokens"] = msg_usage.get("input_tokens", 0)
                usage["cache_creation_input_tokens"] = msg_usage.get(
                    "cache_creation_input_tokens", 0
                )
                usage["cache_read_input_tokens"] = msg_usage.get("cache_read_input_tokens", 0)
            elif event_type == "message_delta":
                delta_usage = obj.get("usage", {})
                usage["output_tokens"] = delta_usage.get("output_tokens", 0)
    except Exception:
        pass


# ------------------------------------------------------------------
# DB logging (fire-and-forget)
# ------------------------------------------------------------------


def _schedule_db_log(
    db_logger: DatabaseLogger,
    *,
    request_id: str,
    model_id: str,
    account_id: str,
    usage: dict[str, int],
    latency_ms: int,
    status_code: int,
    pricing: dict[str, str],
    metadata: dict[str, Any],
) -> None:
    """Schedule a background DB log task (same pattern as completions.py)."""

    async def _log() -> None:
        try:
            await db_logger.log_request(
                request_id=request_id,
                model_id=model_id,
                provider=_PROVIDER_NAME,
                prompt=[],  # We don't log prompt content for the proxy
                response=None,
                usage={
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
                },
                latency_ms=latency_ms,
                status_code=status_code,
                params={"surface": "anthropic_proxy", "account_id": account_id},
                metadata=metadata,
                pricing=pricing,
            )
        except Exception:
            logger.debug(f"Background DB log failed for {request_id}", exc_info=True)

    asyncio.create_task(_log())  # noqa: RUF006


# ------------------------------------------------------------------
# Anthropic-format error helper
# ------------------------------------------------------------------


def _anthropic_error(status: int, message: str) -> JSONResponse:
    """Return an error in Anthropic Messages API format."""
    error_type = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
        500: "api_error",
        502: "api_error",
        503: "overloaded_error",
    }.get(status, "api_error")
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


# ------------------------------------------------------------------
# Account acquisition with token-refresh retry
# ------------------------------------------------------------------


async def _acquire_with_retry(cred_provider, account_pool):
    """Acquire an account and get a valid token, retrying on refresh failure.

    If ``get_valid_token`` raises (e.g. refresh_token expired), marks the
    account unhealthy and tries the next one, until all are exhausted.

    Returns:
        (account, token) tuple.

    Raises:
        NoHealthyAccountError: If no account can produce a valid token.
    """
    last_err = None
    for _ in range(len(account_pool._accounts)):
        account = await account_pool.acquire()
        try:
            token = await cred_provider.get_valid_token(account)
            return account, token
        except Exception as exc:
            logger.warning(f"[AnthropicProxy] Token acquisition failed for {account.id}: {exc}")
            account_pool.report_failure(account.id, 401)
            last_err = exc
    raise NoHealthyAccountError(f"All accounts failed token acquisition: {last_err}")


# ------------------------------------------------------------------
# System prompt injection
# ------------------------------------------------------------------


def _ensure_system_prefix(body: dict[str, Any]) -> None:
    """Ensure the required Claude Code system prompt prefix is present.

    Anthropic's OAuth subscription endpoints require the system prompt to
    begin with the Claude Code identity string.  If the client already
    supplies a system prompt we prepend the prefix; otherwise we inject it.
    """
    prefix = _REQUIRED_SYSTEM_PREFIX
    system = body.get("system")

    # OAuth mode requires array-of-blocks format for system prompts.
    if isinstance(system, list):
        # Already array — check first text block for prefix
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                if not block.get("text", "").startswith(prefix):
                    block["text"] = f"{prefix}\n\n{block['text']}"
                return
        # No text block found — prepend one
        system.insert(0, {"type": "text", "text": prefix})
    elif isinstance(system, str):
        # Convert string → array format (required for OAuth)
        if system.startswith(prefix):
            body["system"] = [{"type": "text", "text": system}]
        else:
            body["system"] = [
                {"type": "text", "text": prefix},
                {"type": "text", "text": system},
            ]
    else:
        # No system prompt at all
        body["system"] = [{"type": "text", "text": prefix}]


# ------------------------------------------------------------------
# Route handler
# ------------------------------------------------------------------


@router.post("/anthropic/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    db_logger=Depends(get_db_logger),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    """Forward an Anthropic Messages API request through subscription credentials."""
    request_id = f"aprx_{int(time.time() * 1000000)}"
    start_time = time.time()

    # --- Parse body -------------------------------------------------
    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Invalid JSON in request body")

    model_id: str = body.get("model", "")
    is_streaming: bool = body.get("stream", False)

    if not model_id:
        return _anthropic_error(400, "Missing required field: model")

    # --- Model resolution -------------------------------------------
    try:
        upstream_model, pricing = _resolve_model(model_id, router_exec, user_ctx)
    except HTTPException as exc:
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model_id),
            provider=normalize_provider_label(_PROVIDER_NAME),
            status_code=str(exc.status_code),
        ).inc()
        return _anthropic_error(exc.status_code, exc.detail)

    # --- Acquire account + token ------------------------------------
    try:
        cred_provider, account_pool = get_shared_pool()
        account, token = await _acquire_with_retry(cred_provider, account_pool)
    except NoHealthyAccountError:
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model_id),
            provider=normalize_provider_label(_PROVIDER_NAME),
            status_code="503",
        ).inc()
        return _anthropic_error(503, "All subscription accounts are currently unavailable")

    # --- Build upstream request -------------------------------------
    body["model"] = upstream_model
    _ensure_system_prefix(body)
    headers = _build_upstream_headers(token, streaming=is_streaming)
    url = _UPSTREAM_MESSAGES_URL

    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "ip": get_client_ip(request),
        "authenticated": bool(user_ctx.get("authenticated")),
        "user_id": user_ctx.get("user_id"),
        "surface": "anthropic_proxy",
    }

    # --- Forward ----------------------------------------------------
    if is_streaming:
        return await _forward_streaming(
            url=url,
            body=body,
            headers=headers,
            account=account,
            cred_provider=cred_provider,
            account_pool=account_pool,
            model_id=model_id,
            request_id=request_id,
            start_time=start_time,
            pricing=pricing,
            metadata=metadata,
            db_logger=db_logger,
        )
    else:
        return await _forward_non_streaming(
            url=url,
            body=body,
            headers=headers,
            account=account,
            cred_provider=cred_provider,
            account_pool=account_pool,
            model_id=model_id,
            request_id=request_id,
            start_time=start_time,
            pricing=pricing,
            metadata=metadata,
            db_logger=db_logger,
        )


# ------------------------------------------------------------------
# Non-streaming forward
# ------------------------------------------------------------------


async def _forward_non_streaming(
    *,
    url: str,
    body: dict,
    headers: dict[str, str],
    account,
    cred_provider,
    account_pool,
    model_id: str,
    request_id: str,
    start_time: float,
    pricing: dict[str, str],
    metadata: dict[str, Any],
    db_logger,
) -> JSONResponse:
    """Forward a non-streaming request and return the JSON response."""
    from serving.http import AsyncHTTPClient

    http = AsyncHTTPClient.shared()

    async def _do_request(hdrs: dict[str, str]) -> dict:
        return await http.json_post_with_retry(
            url, json=body, headers=hdrs, timeout=None, retries=2
        )

    try:
        data = await _do_request(headers)
    except aiohttp.ClientResponseError as exc:
        if exc.status == 401:
            # Force-refresh token and retry once.
            logger.info(f"[AnthropicProxy] 401 for {account.id}, refreshing token")
            token = await cred_provider.get_valid_token(account, force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            try:
                data = await _do_request(headers)
            except aiohttp.ClientResponseError as retry_exc:
                account_pool.report_failure(account.id, retry_exc.status)
                return _forward_upstream_error(retry_exc, model_id)
        else:
            account_pool.report_failure(account.id, exc.status)
            return _forward_upstream_error(exc, model_id)
    except (aiohttp.ClientError, TimeoutError) as exc:
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model_id),
            provider=normalize_provider_label(_PROVIDER_NAME),
            status_code="502",
        ).inc()
        return _anthropic_error(502, f"Upstream connection failed: {exc}")

    account_pool.report_success(account.id)

    # Usage extraction
    usage = data.get("usage", {})
    usage_for_log = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
    }

    latency_ms = int((time.time() - start_time) * 1000)
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(model_id),
        provider=normalize_provider_label(_PROVIDER_NAME),
        status_code="200",
    ).inc()

    if db_logger:
        _schedule_db_log(
            db_logger,
            request_id=request_id,
            model_id=model_id,
            account_id=account.id,
            usage=usage_for_log,
            latency_ms=latency_ms,
            status_code=200,
            pricing=pricing,
            metadata=metadata,
        )

    return JSONResponse(content=data)


# ------------------------------------------------------------------
# Streaming forward
# ------------------------------------------------------------------


async def _forward_streaming(
    *,
    url: str,
    body: dict,
    headers: dict[str, str],
    account,
    cred_provider,
    account_pool,
    model_id: str,
    request_id: str,
    start_time: float,
    pricing: dict[str, str],
    metadata: dict[str, Any],
    db_logger,
) -> StreamingResponse | JSONResponse:
    """Forward a streaming request with raw byte pass-through."""
    from serving.http import AsyncHTTPClient

    http = AsyncHTTPClient.shared()
    session = await http._ensure_session()
    timeout = aiohttp.ClientTimeout(total=None)

    try:
        resp = await session.post(url, json=body, headers=headers, timeout=timeout)
    except (aiohttp.ClientError, TimeoutError) as exc:
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model_id),
            provider=normalize_provider_label(_PROVIDER_NAME),
            status_code="502",
        ).inc()
        return _anthropic_error(502, f"Upstream connection failed: {exc}")

    # Handle non-2xx before streaming.
    if resp.status == 401:
        await resp.release()
        logger.info(f"[AnthropicProxy] 401 stream for {account.id}, refreshing token")
        token = await cred_provider.get_valid_token(account, force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        try:
            resp = await session.post(url, json=body, headers=headers, timeout=timeout)
        except (aiohttp.ClientError, TimeoutError) as exc:
            return _anthropic_error(502, f"Upstream connection failed on retry: {exc}")
        if resp.status >= 400:
            account_pool.report_failure(account.id, resp.status)
            error_body = await resp.text()
            await resp.release()
            return _forward_raw_error(resp.status, error_body, model_id)

    if resp.status >= 400:
        account_pool.report_failure(account.id, resp.status)
        error_body = await resp.text()
        await resp.release()
        return _forward_raw_error(resp.status, error_body, model_id)

    # Do NOT report_success here — wait until the stream completes
    # without error, matching claude_sub.py behaviour.

    usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    stream_failed = False

    async def _stream() -> AsyncIterator[bytes]:
        nonlocal stream_failed
        try:
            async for chunk in resp.content.iter_any():
                _extract_usage_from_sse(chunk, usage)
                yield chunk
        except Exception as exc:
            stream_failed = True
            account_pool.report_failure(account.id, 502)
            # Mid-stream error: emit an Anthropic-format error event.
            error_payload = json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"Stream interrupted: {exc}",
                    },
                }
            )
            yield f"event: error\ndata: {error_payload}\n\n".encode()
        finally:
            if not stream_failed:
                account_pool.report_success(account.id)
            await resp.release()
            latency_ms = int((time.time() - start_time) * 1000)
            API_MODEL_REQUESTS.labels(
                model=normalize_model_label(model_id),
                provider=normalize_provider_label(_PROVIDER_NAME),
                status_code="200" if not stream_failed else "502",
            ).inc()
            if db_logger:
                _schedule_db_log(
                    db_logger,
                    request_id=request_id,
                    model_id=model_id,
                    account_id=account.id,
                    usage=usage,
                    latency_ms=latency_ms,
                    status_code=200 if not stream_failed else 502,
                    pricing=pricing,
                    metadata=metadata,
                )

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------
# Error forwarding helpers
# ------------------------------------------------------------------


def _forward_upstream_error(exc: aiohttp.ClientResponseError, model_id: str) -> JSONResponse:
    """Forward an aiohttp upstream error as an Anthropic-format response."""
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(model_id),
        provider=normalize_provider_label(_PROVIDER_NAME),
        status_code=str(exc.status),
    ).inc()
    # Try to return the upstream error body verbatim.
    error_body = getattr(exc, "error_body", None)
    if error_body:
        try:
            return JSONResponse(status_code=exc.status, content=json.loads(error_body))
        except (json.JSONDecodeError, TypeError):
            pass
    return _anthropic_error(exc.status, exc.message or "Upstream error")


def _forward_raw_error(status: int, body: str, model_id: str) -> JSONResponse:
    """Forward a raw upstream error (from streaming pre-check)."""
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(model_id),
        provider=normalize_provider_label(_PROVIDER_NAME),
        status_code=str(status),
    ).inc()
    try:
        return JSONResponse(status_code=status, content=json.loads(body))
    except (json.JSONDecodeError, TypeError):
        return _anthropic_error(status, body or "Upstream error")
