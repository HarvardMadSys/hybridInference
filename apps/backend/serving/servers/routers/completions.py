"""OpenAI-compatible chat completions endpoint with routing and auth."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from routing.executor import ProviderPinError
from routing.routers import AllCircuitsOpenError, RoutingObservation
from serving.config.settings import has_role
from serving.exceptions import scrub_error_for_user
from serving.observability.metrics import (
    API_MODEL_REQUESTS,
    API_TOKEN_ANOMALIES,
    API_TOKENS,
    normalize_model_label,
    normalize_provider_label,
)
from serving.observability.tracked_tasks import tracked_task
from serving.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
)
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_log_store,
    get_model_router_registry,
    get_operational_store,
    get_router,
)
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip
from serving.utils.token_utils import normalize_usage

logger = get_logger(__name__)
router = APIRouter()


def _schedule_db_log_task(log_store, request_id: str, log_data: dict[str, Any]) -> None:
    """Schedule a background task to log request to database without blocking HTTP response.

    Args:
        log_store: LogStore instance
        request_id: Request identifier for logging
        log_data: Dictionary containing all log request parameters
    """

    async def log_to_db_background() -> None:
        """Background task to log request to database."""
        try:
            await log_store.log_request(**log_data)
            logger.debug(f"Background DB logging completed for request {request_id}")
        except Exception as e:
            # Log error context here (request_id) but re-raise so the
            # tracked_task wrapper records this as a failure event.
            logger.error(
                f"Background DB logging failed for request {request_id}: {e}",
                exc_info=True,
            )
            raise

    tracked_task(log_to_db_background(), name="request_log")


def _schedule_cost_increment(
    op_store: Any,
    user_id: str,
    usage: dict[str, int] | None,
    pricing: dict[str, str] | None,
) -> None:
    """Increment the user's daily cost counter for billed requests.

    Only called for successful responses where cost > 0. Runs as a
    fire-and-forget background task to avoid blocking the response.
    """
    from serving.storage.utils import calculate_cost

    cost = calculate_cost(usage, pricing)
    if not cost or cost <= 0 or not op_store:
        return

    async def _increment() -> None:
        try:
            await op_store.increment_user_cost(user_id, cost)
        except Exception as exc:
            logger.warning(f"Failed to increment cost counter for {user_id}: {exc}")
            raise  # let tracked_task record the failure

    tracked_task(_increment(), name="cost_increment")


def _record_routing_observation(
    active_router,
    model_id: str,
    routing_info: dict[str, Any] | None,
    *,
    ttft_ms: float | None,
    total_latency_ms: float,
    prompt_tokens: int,
    completion_tokens: int,
    success: bool,
) -> None:
    """Emit a RoutingObservation for online learning routers (RouteWise)."""
    ri = routing_info or {}
    # Prefer endpoint_id (RouteWise profile key) > base_url > provider as fallback.
    endpoint_id = ri.get("endpoint_id") or ri.get("base_url") or ri.get("provider", "unknown")
    rw = (routing_info or {}).get("routewise", {})
    obs = RoutingObservation(
        model_id=model_id,
        endpoint_id=endpoint_id,
        ttft_ms=ttft_ms,
        total_latency_ms=total_latency_ms,
        token_count=prompt_tokens + completion_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        success=success,
        quota_committed=rw.get("quota_committed", 0.0),
        selected_tier=rw.get("selected_tier"),
        sc_committed=rw.get("sc_committed", False),
        hedged=rw.get("hedged", False),
        backup_won=rw.get("backup_won", False),
        lp_status=rw.get("lp_status"),
    )
    active_router.record_observation(obs)


def _build_db_params(
    params: dict[str, Any],
    provider: str,
    base_url: str | None,
    get_adapter_config_for_provider: Any,
) -> dict[str, Any]:
    _params = dict(params)
    if _params.get("max_tokens") is None:
        # ``get_adapter_config_for_provider`` returns ``None`` for unregistered
        # providers (e.g., the synthetic "router" placeholder). ``getattr(None,
        # "x", default)`` raises ``AttributeError`` rather than returning the
        # default, so guard explicitly before reading the attribute.
        config = get_adapter_config_for_provider(provider, base_url)
        if config is not None:
            _params["max_tokens"] = getattr(config, "max_output_length", None)
    return _params


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    response_model_exclude_none=True,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        404: {"model": ErrorResponse, "description": "Model Not Found"},
        429: {"model": ErrorResponse, "description": "Too Many Requests"},
        500: {"model": ErrorResponse, "description": "Server Error"},
        503: {"model": ErrorResponse, "description": "Service Unavailable"},
    },
)
async def chat_completions(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    op_store=Depends(get_operational_store),
    model_router_registry=Depends(get_model_router_registry),
    _concurrency_slot=Depends(enforce_user_concurrency),
) -> dict[str, Any]:
    """Handle chat completion requests with routing and fallback.

    Parses the request body using Pydantic for validation, then routes
    to the appropriate adapter. Streaming and non-streaming flows are
    both supported.
    """
    try:
        body = await request.json()
        payload = ChatCompletionRequest.model_validate(body)
    except Exception as e:
        # Record 400 error for request parsing failures
        if request.headers.get("x-probe", "").lower() != "synthetic":
            API_MODEL_REQUESTS.labels(
                model=normalize_model_label("unknown"),
                provider=normalize_provider_label("router"),
                status_code="400",
            ).inc()
        raise HTTPException(400, "Invalid JSON or schema in request body") from e

    is_synthetic_probe = request.headers.get("x-probe", "").lower() == "synthetic"

    def record_model_request(status_code: str, provider_name: str) -> None:
        if is_synthetic_probe:
            return
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model),
            provider=normalize_provider_label(provider_name),
            status_code=status_code,
        ).inc()

    model = payload.model
    messages = [m.model_dump() for m in payload.messages]

    # Debug-only: log inbound message roles to verify client behavior.
    try:
        roles = [msg.get("role") for msg in messages]
        tool_count = sum(1 for msg in messages if msg.get("role") == "tool")
        logger.debug(
            f"Inbound roles: model={model}, roles={roles}, tool_messages={tool_count}, total={len(messages)}"
        )
    except Exception:
        pass

    request_id = f"req_{uuid.uuid4().hex}"
    start_time = time.time()
    is_authenticated = bool(user_ctx.get("authenticated"))
    provider = "router"
    session_id = request.headers.get("X-Session-ID")

    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "ip": get_client_ip(request),
        "authorization": bool(authorization) or is_authenticated,
        "authenticated": is_authenticated,
        "user_id": user_ctx.get("user_id"),
    }
    if is_synthetic_probe:
        metadata["synthetic_probe"] = True
    if session_id:
        metadata["session_id"] = session_id

    early_params: dict[str, Any] = {"stream": bool(payload.stream)}
    if session_id:
        early_params["session_id"] = session_id

    # Check if model has routing configured
    if model not in router_exec.routes:
        record_model_request("404", "router")
        if log_store and not is_synthetic_probe:
            _schedule_db_log_task(
                log_store,
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": "router",
                    "prompt": messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": 404,
                    "error": f"Model '{model}' not found",
                    "params": early_params,
                    "metadata": metadata,
                    "pricing": None,
                },
            )
        raise HTTPException(404, f"Model '{model}' not found")

    # Role-based model gate: insufficient role sees a 404 as if the model doesn't exist
    route = router_exec.routes[model]
    required = route.required_role or ("admin" if route.admin_only else "free")
    user_role = user_ctx.get("role", "free")
    if not has_role(user_role, required):
        logger.info(
            "Insufficient role for model",
            extra={"model": model, "user_id": user_ctx.get("user_id"), "role": user_role},
        )
        record_model_request("404", "router")
        if log_store and not is_synthetic_probe:
            _schedule_db_log_task(
                log_store,
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": "router",
                    "prompt": messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": 404,
                    "error": f"Model '{model}' not found",
                    "params": early_params,
                    "metadata": metadata,
                    "pricing": None,
                },
            )
        raise HTTPException(404, f"Model '{model}' not found")

    # Extract parameters
    params: dict[str, Any] = {"stream": bool(payload.stream)}
    if payload.temperature is not None:
        params["temperature"] = payload.temperature
    if payload.top_p is not None:
        params["top_p"] = payload.top_p
    if payload.top_k is not None:
        params["top_k"] = payload.top_k
    if payload.min_p is not None:
        params["min_p"] = payload.min_p
    if payload.max_tokens is not None:
        params["max_tokens"] = payload.max_tokens
    if payload.stop is not None:
        params["stop"] = payload.stop
    if payload.seed is not None:
        params["seed"] = payload.seed
    if payload.frequency_penalty is not None:
        params["frequency_penalty"] = payload.frequency_penalty
    if payload.presence_penalty is not None:
        params["presence_penalty"] = payload.presence_penalty
    if payload.reasoning_effort is not None:
        params["reasoning_effort"] = payload.reasoning_effort
    if payload.thinking is not None:
        params["thinking"] = payload.thinking
    if payload.tools is not None:
        params["tools"] = payload.tools
    if payload.tool_choice is not None:
        params["tool_choice"] = payload.tool_choice
    if payload.response_format is not None:
        params["response_format"] = payload.response_format.model_dump(by_alias=True)
    if session_id:
        params["session_id"] = session_id

    # Stable user identifier used by cost tracking
    user_id: str = user_ctx.get("user_id") or "anonymous"

    from serving.utils import context as req_ctx

    req_ctx.update({"auth_key_hash": user_ctx.get("auth_key_hash") or "_anon"})

    # Provider pinning: allows the harness (or admin tooling) to force routing
    # to a specific backend.  Only honoured for admin users to prevent abuse.
    pin_provider = request.headers.get("X-Route-Pin")
    if pin_provider and not user_ctx.get("is_admin", False):
        pin_provider = None

    # Helper function to get pricing for a specific provider
    def get_pricing_for_provider(
        provider_name: str, base_url: str | None = None
    ) -> dict[str, str] | None:
        """Find pricing from the actual adapter used (by provider + base_url)."""
        if model not in router_exec.routes:
            return None
        route_config = router_exec.routes[model]

        # Match adapter by provider and optionally base_url
        for adapter, _ in route_config.adapters:
            if not hasattr(adapter, "config"):
                continue
            if adapter.config.provider == provider_name:
                # If base_url provided, match it too (for same provider, different endpoints)
                if (
                    base_url
                    and hasattr(adapter.config, "base_url")
                    and adapter.config.base_url != base_url
                ):
                    continue
                # Found matching adapter
                if hasattr(adapter.config, "pricing"):
                    return adapter.config.pricing
        return None

    def get_adapter_config_for_provider(provider_name: str, base_url: str | None = None) -> Any:
        """Return the adapter config object for the provider/base_url used."""
        if model not in router_exec.routes:
            return None
        route_config = router_exec.routes[model]
        for adapter, _ in route_config.adapters:
            cfg = getattr(adapter, "config", None)
            if not cfg:
                continue
            if cfg.provider != provider_name:
                continue
            if base_url and getattr(cfg, "base_url", None) != base_url:
                continue
            return cfg
        return None

    def get_single_route_provider() -> str | None:
        """Return a provider name when the route has exactly one backend."""
        if len(route.adapters) != 1:
            return None
        adapter, _ = route.adapters[0]
        config = getattr(adapter, "config", None)
        return getattr(config, "provider", None)

    # Pre-flight: validate pin_provider before routing.  For streaming this is
    # critical (HTTP 200 is already committed once StreamingResponse starts),
    # but we check unconditionally so non-stream also gets a clean 400.
    if pin_provider:
        route = router_exec.routes.get(model)
        if not route or not any(
            (
                adapter.config.provider == pin_provider
                or (getattr(adapter.config, "endpoint_id", None) or adapter.config.provider)
                == pin_provider
            )
            and weight > 0
            for adapter, weight in route.adapters
        ):
            record_model_request("400", "router")
            raise HTTPException(
                status_code=400,
                detail=f"Pinned provider '{pin_provider}' not found for model {model}",
            )

    # Per-model routing strategy via ModelRouterRegistry.
    # pin_provider always bypasses RouteWise → goes direct to FixedRouter.
    active_router = router_exec
    if model_router_registry is not None and not pin_provider:
        active_router = model_router_registry.get_router(model)

    # Streaming path
    if payload.stream:

        async def stream_generator():
            usage_data = None
            routing_info = None
            chunk_count = 0
            # Accumulate streamed content for DB logging
            final_text = ""
            final_reasoning = ""
            finish_reason_for_db = "stop"
            # Properly handle tool_calls delta merging by index
            tool_calls_map: dict[int, dict[str, Any]] = {}
            # Track TTFT: time to first token
            ttft_ms: int | None = None
            # Track provider from request context (fallback when routing_info is not available)
            provider_from_ctx: str | None = None
            try:
                # Emit initial assistant role chunk for client compatibility (e.g., Cursor)
                from serving.openai_chat_serializer import resolve_mode, sanitize_chunk
                from serving.stream import make_role_chunk

                role_chunk = make_role_chunk(model=model)
                logger.debug(f"Yielding initial role chunk: {role_chunk[:150]}")
                yield role_chunk

                serializer_mode = resolve_mode(request.headers)
                if serializer_mode.value != "strict_openai":
                    logger.debug(
                        f"OpenAI chat serializer mode={serializer_mode.value} for model={model}"
                    )

                logger.debug(f"Starting to consume adapter stream for model: {model}")
                # Keepalive: a background task consumes the adapter stream and
                # feeds chunks into a queue.  The generator pulls from the queue
                # with a short timeout; on timeout it yields an SSE comment so
                # intermediate proxies (Cloudflare 100s, Nginx 120s) see activity
                # and don't close the connection during long upstream pauses
                # (e.g., reasoning).  This avoids cancelling the upstream read.
                _KEEPALIVE_INTERVAL = 30  # seconds
                _SENTINEL = object()  # marks end of adapter stream
                last_client_yield = time.monotonic()  # track last data sent to client
                chunk_queue: asyncio.Queue = asyncio.Queue()

                async def _adapter_reader():
                    try:
                        if active_router is router_exec:
                            stream = router_exec.stream_chat_completion(
                                model, messages, pin_provider=pin_provider, **params
                            )
                        else:
                            stream = active_router.stream_chat_completion(model, messages, **params)
                        async for item in stream:
                            await chunk_queue.put(item)
                    except Exception as exc:
                        await chunk_queue.put(exc)
                    finally:
                        await chunk_queue.put(_SENTINEL)

                reader_task = asyncio.create_task(_adapter_reader())
                try:
                    while True:
                        try:
                            item = await asyncio.wait_for(
                                chunk_queue.get(), timeout=_KEEPALIVE_INTERVAL
                            )
                        except asyncio.TimeoutError:
                            logger.debug(
                                f"Emitting SSE keepalive (no chunk in {_KEEPALIVE_INTERVAL}s) "
                                f"for model={model}"
                            )
                            yield ": keepalive\n\n"
                            last_client_yield = time.monotonic()
                            continue
                        if item is _SENTINEL:
                            break
                        if isinstance(item, Exception):
                            raise item
                        chunk = item
                        chunk_count += 1

                        # Extract provider from request context on first chunk
                        if provider_from_ctx is None:
                            from serving.utils import context as req_ctx

                            ctx = req_ctx.get()
                            if ctx and "provider" in ctx:
                                provider_from_ctx = ctx["provider"]
                                logger.debug(
                                    f"Extracted provider from context: {provider_from_ctx}"
                                )

                        # Forward adapter SSE chunks with sanitization.
                        if chunk_count <= 10 or chunk_count % 10 == 0:
                            logger.debug(
                                f"Chunk {chunk_count} received from adapter: {chunk[:200]}"
                            )

                        # Extract usage and routing; sanitize for public API contract
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                chunk_json = json.loads(chunk[6:])
                                result = sanitize_chunk(chunk_json, serializer_mode)

                                if result.usage_data:
                                    usage_data = result.usage_data
                                    logger.debug(
                                        f"Extracted usage from chunk {chunk_count}: {usage_data}"
                                    )
                                if result.routing_info:
                                    routing_info = result.routing_info
                                    logger.debug(
                                        f"Extracted routing from chunk {chunk_count}: {routing_info}"
                                    )

                                # Record TTFT at first meaningful delta
                                if ttft_ms is None:
                                    try:
                                        choices_local = chunk_json.get("choices", [])
                                        if choices_local:
                                            delta_local = choices_local[0].get("delta", {})
                                            has_content = bool(delta_local.get("content"))
                                            has_tool_calls = bool(delta_local.get("tool_calls"))
                                            has_reasoning = bool(
                                                delta_local.get("reasoning_content")
                                            )
                                            if has_content or has_tool_calls or has_reasoning:
                                                ttft_ms = int((time.time() - start_time) * 1000)
                                                logger.debug(
                                                    f"TTFT recorded (first delta): {ttft_ms}ms"
                                                )
                                    except Exception:
                                        pass

                                # Accumulate content and finish_reason for DB logging
                                choices = chunk_json.get("choices", [])
                                if choices:
                                    choice = choices[0]
                                    delta = choice.get("delta", {})

                                    content_piece = delta.get("content")
                                    if content_piece:
                                        if ttft_ms is None:
                                            ttft_ms = int((time.time() - start_time) * 1000)
                                            logger.debug(f"TTFT recorded: {ttft_ms}ms")
                                        final_text += content_piece

                                    reasoning_piece = delta.get("reasoning_content")
                                    if not (isinstance(reasoning_piece, str) and reasoning_piece):
                                        reasoning_piece = delta.get("reasoning")
                                    if isinstance(reasoning_piece, str) and reasoning_piece:
                                        final_reasoning += reasoning_piece

                                    # Handle tool_calls delta merging
                                    tool_calls_delta = delta.get("tool_calls")
                                    if tool_calls_delta:
                                        for tc_delta in tool_calls_delta:
                                            idx = tc_delta.get("index", 0)
                                            if idx not in tool_calls_map:
                                                tool_calls_map[idx] = {
                                                    "index": idx,
                                                    "id": tc_delta.get("id", ""),
                                                    "type": tc_delta.get("type", "function"),
                                                    "function": {
                                                        "name": "",
                                                        "arguments": "",
                                                    },
                                                }
                                            if "id" in tc_delta:
                                                tool_calls_map[idx]["id"] = tc_delta["id"]
                                            if "type" in tc_delta:
                                                tool_calls_map[idx]["type"] = tc_delta["type"]
                                            if "function" in tc_delta:
                                                fn_delta = tc_delta["function"]
                                                if "name" in fn_delta:
                                                    tool_calls_map[idx]["function"]["name"] = (
                                                        fn_delta["name"]
                                                    )
                                                if "arguments" in fn_delta:
                                                    tool_calls_map[idx]["function"][
                                                        "arguments"
                                                    ] += fn_delta["arguments"]

                                    fr = choice.get("finish_reason")
                                    if fr:
                                        finish_reason_for_db = fr

                                # Strict mode: reasoning-only chunks are not forwarded;
                                # emit keepalive when client idle long enough
                                if not result.should_forward:
                                    now = time.monotonic()
                                    if now - last_client_yield >= _KEEPALIVE_INTERVAL:
                                        logger.debug(
                                            f"Sending keepalive instead of "
                                            f"reasoning chunk {chunk_count} for model={model}"
                                        )
                                        yield ": keepalive\n\n"
                                        last_client_yield = now
                                    continue

                                # Yield chunk to client
                                out_json = result.chunk_json or chunk_json
                                sanitized_chunk = f"data: {json.dumps(out_json)}\n\n"
                                logger.debug(
                                    f"Yielding sanitized chunk {chunk_count} to client: "
                                    f"{sanitized_chunk[:150]}"
                                )
                                yield sanitized_chunk
                                last_client_yield = time.monotonic()
                                continue
                            except (json.JSONDecodeError, KeyError) as e:
                                logger.warning(f"Failed to parse chunk {chunk_count}: {e}")

                        # Non-JSON or [DONE] chunks pass through
                        logger.debug(f"Yielding chunk {chunk_count} to client: {chunk[:150]}")
                        yield chunk

                finally:
                    reader_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await reader_task

                logger.info(f"Stream complete: total_chunks={chunk_count}")

                if not final_text and not tool_calls_map:
                    logger.warning(
                        f"Stream completed with no visible content or tool_calls: "
                        f"model={model}, chunks={chunk_count}, request_id={request_id}"
                    )

                # Reconstruct a complete response object for DB logging
                message_for_db: dict[str, Any] = {
                    "role": "assistant",
                    "content": final_text if final_text else None,
                }
                if final_reasoning:
                    message_for_db["reasoning_content"] = final_reasoning
                response_for_db: dict[str, Any] = {
                    "id": request_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": message_for_db,
                            "finish_reason": finish_reason_for_db,
                        }
                    ],
                }

                # Add tool_calls if any were accumulated
                if tool_calls_map:
                    # Convert map to list, sorted by index
                    tool_calls_list = [tc for _, tc in sorted(tool_calls_map.items())]
                    response_for_db["choices"][0]["message"]["tool_calls"] = tool_calls_list

                # Add usage if available
                if usage_data:
                    response_for_db["usage"] = normalize_usage(usage_data) or usage_data

                # Get pricing from actual provider used
                # Fallback to provider from request context if routing_info is not available
                provider = "router"
                pricing = None
                if routing_info:
                    provider = routing_info.get("provider", "router")
                    base_url = routing_info.get("base_url")
                    # Prefer embedded pricing (e.g. adapter-internal fallback)
                    # before looking up from registered routes
                    pricing = routing_info.get("pricing") or get_pricing_for_provider(
                        provider, base_url
                    )
                    # Strip upstream_cost_usd from metadata JSONB; the dedicated column
                    # api_logs.upstream_cost_usd is the canonical store. Avoids leaking the
                    # internal cost into any future admin route that returns raw metadata.
                    metadata.update(
                        {k: v for k, v in routing_info.items() if k != "upstream_cost_usd"}
                    )
                elif provider_from_ctx:
                    # Fallback: use provider extracted from request context during streaming
                    provider = provider_from_ctx
                    pricing = get_pricing_for_provider(provider, None)
                    logger.debug(f"Using provider from context for DB logging: {provider}")

                # Prepare data for background database logging (don't await here!)
                if log_store and not is_synthetic_probe:
                    _schedule_db_log_task(
                        log_store,
                        request_id,
                        {
                            "request_id": request_id,
                            "model_id": model,
                            "provider": provider,
                            "prompt": messages,
                            "response": response_for_db,
                            "usage": response_for_db.get("usage")
                            if response_for_db
                            else usage_data,
                            "latency_ms": int((time.time() - start_time) * 1000),
                            "status_code": 200,
                            "params": _build_db_params(
                                params,
                                provider,
                                routing_info.get("base_url") if routing_info else None,
                                get_adapter_config_for_provider,
                            ),
                            "metadata": metadata,
                            "ttft_ms": ttft_ms,
                            "pricing": pricing,
                            "upstream_cost_usd": (routing_info or {}).get("upstream_cost_usd"),
                        },
                    )

                # Increment daily cost counter for billed requests
                if not is_synthetic_probe and routing_info:
                    _usage = response_for_db.get("usage") if response_for_db else usage_data
                    _s_pricing = routing_info.get("pricing") or get_pricing_for_provider(
                        provider, routing_info.get("base_url")
                    )
                    _schedule_cost_increment(op_store, user_id, _usage, _s_pricing)

                # Record routing observation for online learning (RouteWise)
                if not is_synthetic_probe:
                    stream_usage = normalize_usage(usage_data) if usage_data else {}
                    _record_routing_observation(
                        active_router,
                        model,
                        routing_info,
                        ttft_ms=float(ttft_ms) if ttft_ms is not None else None,
                        total_latency_ms=(time.time() - start_time) * 1000,
                        prompt_tokens=int(stream_usage.get("prompt_tokens", 0) or 0),
                        completion_tokens=int(stream_usage.get("completion_tokens", 0) or 0),
                        success=True,
                    )

            except Exception as exc:
                # Record failure observation for online learning (RouteWise)
                if not is_synthetic_probe:
                    exc_routing = getattr(exc, "_routing", None)
                    _record_routing_observation(
                        active_router,
                        model,
                        exc_routing or routing_info,
                        ttft_ms=float(ttft_ms) if ttft_ms is not None else None,
                        total_latency_ms=(time.time() - start_time) * 1000,
                        prompt_tokens=0,
                        completion_tokens=0,
                        success=False,
                    )

                # Prepare error data for background logging
                # Try to get actual provider from context even in error case
                from serving.utils import context as req_ctx

                ctx = req_ctx.get()
                provider_for_error = ctx.get("provider", "router") if ctx else "router"

                if log_store and not is_synthetic_probe:
                    _schedule_db_log_task(
                        log_store,
                        request_id,
                        {
                            "request_id": request_id,
                            "model_id": model,
                            "provider": provider_for_error,
                            "prompt": messages,
                            "response": None,
                            "usage": None,
                            "latency_ms": int((time.time() - start_time) * 1000),
                            "status_code": 500,
                            "error": str(exc),
                            "params": params,
                            "metadata": metadata,
                            "ttft_ms": ttft_ms,
                            "pricing": None,  # Error case - no pricing available
                        },
                    )

                user_msg = scrub_error_for_user(exc, request_id, 500)
                error_chunk = {"error": {"message": user_msg, "type": "server_error", "code": 500}}
                error_msg = f"data: {json.dumps(error_chunk)}\n\n"
                logger.error(f"Yielding error chunk: {error_msg}")
                yield error_msg

        logger.debug(f"Creating StreamingResponse for model: {model}")

        # Record 200 for streaming response (HTTP layer success)
        record_model_request("200", provider)
        response_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if is_synthetic_probe:
            provider_header = get_single_route_provider()
            if provider_header:
                response_headers["X-Provider"] = provider_header
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers=response_headers,
        )

    # Non-streaming path
    try:
        from serving.openai_chat_serializer import resolve_mode, sanitize_response

        if active_router is router_exec:
            response = await router_exec.chat_completion(
                model, messages, pin_provider=pin_provider, **params
            )
        else:
            response = await active_router.chat_completion(model, messages, **params)
        serializer_mode = resolve_mode(request.headers)

        # Apply serializer: strip _routing metadata and enforce reasoning_content
        # policy (strict / passthrough), mirroring the streaming path contract.
        provider = "router"
        base_url = None
        routing_pricing = None
        if isinstance(response, dict):
            sanitize_result = sanitize_response(response, serializer_mode)
            response = sanitize_result.response_json
            routing_info = sanitize_result.routing_info
            if routing_info:
                provider = routing_info.get("provider", "router")
                base_url = routing_info.get("base_url")
                routing_pricing = routing_info.get("pricing")
                # See comment above: strip upstream_cost_usd before merging into metadata JSONB.
                metadata.update({k: v for k, v in routing_info.items() if k != "upstream_cost_usd"})  # type: ignore[arg-type]
        else:
            # Fallback: get provider from request context when response is not a dict
            from serving.utils import context as req_ctx

            ctx = req_ctx.get()
            if ctx and "provider" in ctx:
                provider = ctx["provider"]
                logger.debug(
                    f"Using provider from context for non-streaming DB logging: {provider}"
                )

        # Move log_store.log_request() out of the stream_generator
        # and into a background task that runs after the response is sent.
        if log_store and not is_synthetic_probe:
            # Prefer embedded pricing (e.g. adapter-internal fallback)
            pricing = routing_pricing or get_pricing_for_provider(provider, base_url)
            _schedule_db_log_task(
                log_store,
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": provider,
                    "prompt": messages,
                    "response": response,
                    "usage": normalize_usage(response.get("usage"))
                    if isinstance(response, dict)
                    else None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": 200,
                    "params": _build_db_params(
                        params,
                        provider,
                        base_url,
                        get_adapter_config_for_provider,
                    ),
                    "metadata": metadata,
                    "pricing": pricing,
                    "upstream_cost_usd": (routing_info or {}).get("upstream_cost_usd"),
                },
            )

        # Increment daily cost counter for billed requests (non-streaming)
        if not is_synthetic_probe:
            _ns_usage = (
                normalize_usage(response.get("usage")) if isinstance(response, dict) else None
            )
            _ns_pricing = routing_pricing or get_pricing_for_provider(provider, base_url)
            _schedule_cost_increment(op_store, user_id, _ns_usage, _ns_pricing)

        # Emit token counters when usage is available, with anomaly checks
        # Normalize usage to extract reasoning_tokens from nested locations
        raw_usage = response.get("usage", {}) if isinstance(response, dict) else {}
        usage = normalize_usage(raw_usage) or {}
        if usage and not is_synthetic_probe:
            prompt_tokens_raw = usage.get("prompt_tokens")
            completion_tokens_raw = usage.get("completion_tokens")
            total_tokens_raw = usage.get("total_tokens")
            reasoning_tokens_raw = usage.get("reasoning_tokens")

            try:
                prompt_tokens = int(prompt_tokens_raw or 0)
                completion_tokens = int(completion_tokens_raw or 0)
                reasoning_tokens = int(reasoning_tokens_raw or 0)
                total_tokens = int(
                    total_tokens_raw or (prompt_tokens + completion_tokens + reasoning_tokens)
                )
            except Exception:
                API_TOKEN_ANOMALIES.labels(
                    model=normalize_model_label(model),
                    provider=normalize_provider_label(provider),
                    reason="non_integer",
                ).inc()
                logger.warning(f"Invalid token usage types for {model}/{provider}: {usage}")
                prompt_tokens = completion_tokens = reasoning_tokens = total_tokens = 0

            # Basic sanity: non-negative, totals consistent, and not absurdly large
            max_tokens_cap = 10_000_000
            sane = (
                0 <= prompt_tokens < max_tokens_cap
                and 0 <= completion_tokens < max_tokens_cap
                and 0 <= reasoning_tokens < max_tokens_cap
                and 0 <= total_tokens < max_tokens_cap
                and total_tokens >= prompt_tokens + completion_tokens + reasoning_tokens
            )
            if not sane:
                API_TOKEN_ANOMALIES.labels(
                    model=normalize_model_label(model),
                    provider=normalize_provider_label(provider),
                    reason="invalid_values",
                ).inc()
                logger.warning(f"Token usage anomaly for {model}/{provider}: {usage}")
            else:
                if prompt_tokens:
                    API_TOKENS.labels(
                        model=normalize_model_label(model),
                        provider=normalize_provider_label(provider),
                        direction="prompt",
                    ).inc(prompt_tokens)
                if completion_tokens:
                    API_TOKENS.labels(
                        model=normalize_model_label(model),
                        provider=normalize_provider_label(provider),
                        direction="completion",
                    ).inc(completion_tokens)
                if reasoning_tokens:
                    API_TOKENS.labels(
                        model=normalize_model_label(model),
                        provider=normalize_provider_label(provider),
                        direction="reasoning",
                    ).inc(reasoning_tokens)

        # Record routing observation for online learning (RouteWise)
        if not is_synthetic_probe:
            ns_usage = normalize_usage(raw_usage) or {}
            _record_routing_observation(
                active_router,
                model,
                routing_info if isinstance(response, dict) else None,
                ttft_ms=None,
                total_latency_ms=(time.time() - start_time) * 1000,
                prompt_tokens=int(ns_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(ns_usage.get("completion_tokens", 0) or 0),
                success=True,
            )

        # Record 200 for non-streaming response
        record_model_request("200", provider)
        if is_synthetic_probe and provider != "router":
            http_response.headers["X-Provider"] = provider
        return response

    except ProviderPinError as exc:
        record_model_request("400", "router")
        raise HTTPException(
            status_code=400,
            detail=scrub_error_for_user(exc, request_id, 400),
        ) from exc

    except AllCircuitsOpenError as exc:
        # Full provider outage: every circuit breaker for this model is open.
        # Surface this as 503 Service Unavailable so clients can distinguish
        # "we're temporarily overloaded / all upstreams down" from a generic
        # 500 server error.
        record_model_request("503", "router")
        raise HTTPException(
            status_code=503,
            detail=scrub_error_for_user(exc, request_id, 503),
        ) from exc

    except Exception as exc:
        # Record failure observation for online learning (RouteWise)
        if not is_synthetic_probe:
            exc_routing = getattr(exc, "_routing", None)
            _record_routing_observation(
                active_router,
                model,
                exc_routing,
                ttft_ms=None,
                total_latency_ms=(time.time() - start_time) * 1000,
                prompt_tokens=0,
                completion_tokens=0,
                success=False,
            )

        # Best-effort extraction of status code from exception
        # Different HTTP client libraries store status codes in different places:
        # - OpenAI/Anthropic SDK: exc.status_code
        # - httpx: exc.response.status_code
        # - aiohttp: exc.status
        # - requests: exc.response.status_code
        exc_status_code = None

        # Try direct status_code attribute (OpenAI, Anthropic SDKs)
        if hasattr(exc, "status_code") and exc.status_code is not None:
            exc_status_code = exc.status_code
        # Try response.status_code (httpx, requests)
        elif hasattr(exc, "response") and exc.response is not None:
            if hasattr(exc.response, "status_code"):
                exc_status_code = exc.response.status_code
            elif hasattr(exc.response, "status"):
                exc_status_code = exc.response.status
        # Try direct status attribute (aiohttp)
        elif hasattr(exc, "status") and exc.status is not None:
            exc_status_code = exc.status
        # Try code attribute (some custom exceptions)
        elif hasattr(exc, "code") and exc.code is not None:
            exc_status_code = exc.code

        # Default to 500 if we couldn't extract status code
        if exc_status_code is None:
            exc_status_code = 500

        # Move log_store.log_request() out of the stream_generator
        # and into a background task that runs after the response is sent.
        # Try to get actual provider from context even in error case
        from serving.utils import context as req_ctx

        ctx = req_ctx.get()
        provider_for_error = ctx.get("provider", "router") if ctx else "router"

        if log_store and not is_synthetic_probe:
            _schedule_db_log_task(
                log_store,
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": provider_for_error,
                    "prompt": messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": exc_status_code,
                    "error": str(exc),
                    "params": params,
                    "metadata": metadata,
                    "pricing": None,  # Error case - no pricing available
                },
            )
        # Record error status code
        record_model_request(str(exc_status_code), provider_for_error)

        raise HTTPException(
            exc_status_code,
            scrub_error_for_user(exc, request_id, exc_status_code),
        ) from exc
