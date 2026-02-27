"""OpenAI-compatible chat completions endpoint with routing and auth."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from serving.config.settings import RoutingStrategy, get_settings
from serving.observability.metrics import (
    API_MODEL_REQUESTS,
    API_TOKEN_ANOMALIES,
    API_TOKENS,
    NIMBUS_PREDICTION_ERROR,
    normalize_model_label,
    normalize_provider_label,
)
from serving.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
)
from serving.servers.auth import verify_api_key
from serving.servers.deps import (
    get_db_logger,
    get_nimbus_router,
    get_rate_limiter,
    get_router,
)
from serving.servers.rate_limiter import TokenCounter
from serving.utils.logging import get_logger
from serving.utils.token_utils import normalize_usage

logger = get_logger(__name__)
router = APIRouter()


def _schedule_db_log_task(db_logger, request_id: str, log_data: dict[str, Any]) -> None:
    """Schedule a background task to log request to database without blocking HTTP response.

    Args:
        db_logger: Database logger instance
        request_id: Request identifier for logging
        log_data: Dictionary containing all log request parameters
    """

    async def log_to_db_background():
        """Background task to log request to database."""
        try:
            await db_logger.log_request(**log_data)
            logger.debug(f"Background DB logging completed for request {request_id}")
        except Exception as e:
            # Log error but don't fail the request - it's already sent to client
            logger.error(
                f"Background DB logging failed for request {request_id}: {e}",
                exc_info=True,
            )

    # Fire-and-forget background task for non-blocking DB logging
    # We intentionally don't store the reference as we don't need to await it
    asyncio.create_task(log_to_db_background())  # noqa: RUF006


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        404: {"model": ErrorResponse, "description": "Model Not Found"},
        429: {"model": ErrorResponse, "description": "Rate Limit Exceeded"},
        500: {"model": ErrorResponse, "description": "Server Error"},
    },
)
async def chat_completions(
    request: Request,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    nimbus_router=Depends(get_nimbus_router),
    rate_limiter=Depends(get_rate_limiter),
    db_logger=Depends(get_db_logger),
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
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label("unknown"),
            provider=normalize_provider_label("router"),
            status_code="400",
        ).inc()
        raise HTTPException(400, "Invalid JSON or schema in request body") from e

    model = payload.model
    messages = [m.model_dump() for m in payload.messages]

    # Debug-only: log inbound message roles to verify client behavior.
    # Note: We intentionally avoid logging message contents to protect privacy.
    try:
        roles = [msg.get("role") for msg in messages]
        tool_count = sum(1 for msg in messages if msg.get("role") == "tool")
        logger.debug(
            f"Inbound roles: model={model}, roles={roles}, tool_messages={tool_count}, total={len(messages)}"
        )
    except Exception:
        # Swallow any logging issues to avoid impacting request handling.
        pass

    # Check if model has routing configured
    if model not in router_exec.routes:
        # Record 404 error for model not found
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model),
            provider=normalize_provider_label("router"),
            status_code="404",
        ).inc()
        raise HTTPException(404, f"Model '{model}' not found")

    # Extract parameters
    params: dict[str, Any] = {}
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
    if payload.ignore_eos is not None:
        params["ignore_eos"] = payload.ignore_eos
    if payload.frequency_penalty is not None:
        params["frequency_penalty"] = payload.frequency_penalty
    if payload.presence_penalty is not None:
        params["presence_penalty"] = payload.presence_penalty
    if payload.tools is not None:
        params["tools"] = payload.tools
    if payload.tool_choice is not None:
        params["tool_choice"] = payload.tool_choice
    if payload.response_format is not None:
        params["response_format"] = payload.response_format.model_dump(by_alias=True)
    # Always record whether this request is streaming for DB analytics
    params["stream"] = bool(payload.stream)

    # Rate limit check with advanced features
    if rate_limiter:
        # Higher priority for authenticated requests (via Authorization or X-API-Key)
        priority = 1 if user_ctx.get("authenticated") else 0
        success, meta = await rate_limiter.acquire_tokens(
            model_id=model,
            messages=messages,
            max_tokens=params.get("max_tokens"),
            priority=priority,
            timeout=30.0,
        )

        if not success:
            error_detail: dict[str, Any] = {
                "error": {
                    "type": "rate_limit_exceeded",
                    "message": meta.get("error", "Rate limit exceeded"),
                    "model": model,
                    "retry_after": meta.get("retry_after", 60),
                }
            }
            if "tokens_requested" in meta:
                error_detail["error"]["tokens_requested"] = meta["tokens_requested"]
            if "queue_size" in meta:
                error_detail["error"]["queue_size"] = meta["queue_size"]

            headers: dict[str, str] = {
                "X-RateLimit-RetryAfter": str(meta.get("retry_after", 60)),
                "X-RateLimit-Model": model,
            }
            status = rate_limiter.get_status(model)
            if status.get("configured"):
                headers.update(
                    {
                        "X-RateLimit-Limit": str(status.get("capacity")),
                        "X-RateLimit-Remaining": str(int(status.get("tokens_available", 0))),
                        "X-RateLimit-Window": str(int(status.get("window_seconds", 0))),
                    }
                )

            # Record 429 rate limit error
            API_MODEL_REQUESTS.labels(
                model=normalize_model_label(model),
                provider=normalize_provider_label("router"),
                status_code="429",
            ).inc()
            raise HTTPException(status_code=429, detail=error_detail, headers=headers)

    # Generate request ID and metadata
    request_id = f"req_{int(time.time() * 1000000)}"
    # Propagate request_id so downstream routers (e.g. OutsourcingRouter) can use it
    params["request_id"] = request_id
    start_time = time.time()
    is_authenticated = bool(user_ctx.get("authenticated"))
    # Initialize provider early to avoid UnboundLocalError in exception handlers
    provider = "router"
    # Extract a stable session identifier from a single, canonical header.
    # Clients are expected to send X-Session-ID. Starlette headers are case-insensitive.
    session_id = request.headers.get("X-Session-ID")

    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "ip": request.client.host if request.client else None,
        # Preserve legacy field but treat either auth header as authenticated
        "authorization": bool(authorization) or is_authenticated,
        "authenticated": is_authenticated,
        "user_id": user_ctx.get("user_id"),
    }
    if session_id:
        metadata["session_id"] = session_id

    # Routing Strategy Selection
    settings = get_settings()
    strategy = settings.get_routing_strategy()

    # Use Nimbus if strategy is set to nimbus and router is available
    use_nimbus = (
        strategy == RoutingStrategy.NIMBUS
        and nimbus_router is not None
        and model in nimbus_router.outsourcing_routers
    )

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

    # Streaming path
    if payload.stream:

        async def stream_generator():
            # State for DB logging (captured in finally)
            usage_data = None
            routing_info = None
            chunk_count = 0
            final_text = ""
            finish_reason_for_db = "stop"
            tool_calls_map: dict[int, dict[str, Any]] = {}
            ttft_ms: int | None = None
            # Track provider from request context (fallback when routing_info is not available)
            provider_from_ctx: str | None = None
            status_code = 200
            error_msg_for_db: str | None = None
            stream_provider = "router"
            try:
                # Emit initial assistant role chunk for client compatibility (e.g., Cursor)
                from serving.stream import make_role_chunk

                role_chunk = make_role_chunk(model=model)
                logger.debug(f"Yielding initial role chunk: {role_chunk[:150]}")
                yield role_chunk

                logger.debug(
                    f"Starting to consume stream for model: {model} (Strategy: {'Nimbus' if use_nimbus else 'Fixed'})"
                )

                # Select stream source based on strategy
                if use_nimbus:
                    stream_source = nimbus_router.stream_chat_completion(model, messages, **params)
                else:
                    stream_source = router_exec.stream_chat_completion(model, messages, **params)

                async for chunk in stream_source:
                    chunk_count += 1

                    # Extract provider from request context on first chunk (context is active during streaming)
                    if provider_from_ctx is None:
                        from serving.utils import context as req_ctx

                        ctx = req_ctx.get()
                        if ctx and "provider" in ctx:
                            provider_from_ctx = ctx["provider"]
                            logger.debug(f"Extracted provider from context: {provider_from_ctx}")

                    # Forward adapter SSE chunks with sanitization. Adapters may emit final usage chunk.
                    if chunk_count <= 10 or chunk_count % 10 == 0:
                        logger.debug(f"Chunk {chunk_count} received from adapter: {chunk[:200]}")

                    # Extract usage and routing info from chunks; sanitize before yielding
                    if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                        try:
                            chunk_json = json.loads(chunk[6:])
                            if chunk_json.get("usage"):
                                usage_data = chunk_json["usage"]
                                logger.debug(
                                    f"Extracted usage from chunk {chunk_count}: {usage_data}"
                                )
                            # Streaming adapters may also include _routing in final chunk
                            if "_routing" in chunk_json:
                                routing_info = chunk_json.get("_routing")
                                logger.debug(
                                    f"Extracted routing from chunk {chunk_count}: {routing_info}"
                                )
                                # Never leak routing info to clients
                                with suppress(Exception):
                                    del chunk_json["_routing"]

                            # Record TTFT at the first meaningful delta (content or tool_calls)
                            if ttft_ms is None:
                                try:
                                    choices_local = chunk_json.get("choices", [])
                                    if choices_local:
                                        delta_local = choices_local[0].get("delta", {})
                                        has_content = bool(delta_local.get("content"))
                                        has_tool_calls = bool(delta_local.get("tool_calls"))
                                        if has_content or has_tool_calls:
                                            ttft_ms = int((time.time() - start_time) * 1000)
                                            logger.debug(
                                                f"TTFT recorded (first delta): {ttft_ms}ms"
                                            )
                                except Exception:
                                    # Best effort only; do not impact streaming on errors.
                                    pass

                            # Accumulate content and finish_reason for DB logging
                            choices = chunk_json.get("choices", [])
                            if choices:
                                choice = choices[0]
                                delta = choice.get("delta", {})

                                # Accumulate content
                                content_piece = delta.get("content")
                                if content_piece:
                                    # Record TTFT at the first actual content token
                                    if ttft_ms is None:
                                        ttft_ms = int((time.time() - start_time) * 1000)
                                        logger.debug(f"TTFT recorded: {ttft_ms}ms")
                                    final_text += content_piece

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
                                                "function": {"name": "", "arguments": ""},
                                            }

                                        # Merge id if present
                                        if "id" in tc_delta:
                                            tool_calls_map[idx]["id"] = tc_delta["id"]

                                        # Merge type if present
                                        if "type" in tc_delta:
                                            tool_calls_map[idx]["type"] = tc_delta["type"]

                                        # Merge function delta
                                        if "function" in tc_delta:
                                            fn_delta = tc_delta["function"]
                                            if "name" in fn_delta:
                                                tool_calls_map[idx]["function"]["name"] = fn_delta[
                                                    "name"
                                                ]
                                            if "arguments" in fn_delta:
                                                # Arguments are streamed incrementally
                                                tool_calls_map[idx]["function"]["arguments"] += (
                                                    fn_delta["arguments"]
                                                )

                                # Update finish_reason if present
                                fr = choice.get("finish_reason")
                                if fr:
                                    finish_reason_for_db = fr

                                # Filter out non-standard fields from delta for OpenAI compatibility
                                # Some providers (e.g., Zhipu GLM-4.6) return reasoning_content which
                                # is not part of the OpenAI API spec and may break clients like Codex
                                if "reasoning_content" in delta:
                                    # Create a sanitized copy of the chunk without reasoning_content
                                    chunk_json = json.loads(json.dumps(chunk_json))  # Deep copy
                                    if chunk_json.get("choices") and chunk_json["choices"]:
                                        sanitized_delta = {
                                            k: v
                                            for k, v in chunk_json["choices"][0]
                                            .get("delta", {})
                                            .items()
                                            if k != "reasoning_content"
                                        }
                                        chunk_json["choices"][0]["delta"] = sanitized_delta

                            # Yield sanitized chunk to client
                            sanitized_chunk = f"data: {json.dumps(chunk_json)}\n\n"
                            logger.debug(
                                f"Yielding sanitized chunk {chunk_count} to client: {sanitized_chunk[:150]}"
                            )
                            yield sanitized_chunk
                            continue
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning(f"Failed to parse chunk {chunk_count}: {e}")
                            # Fall through to yield original chunk unmodified

                    # Non-JSON or [DONE] chunks pass through
                    logger.debug(f"Yielding chunk {chunk_count} to client: {chunk[:150]}")
                    yield chunk

                logger.info(f"Stream complete: total_chunks={chunk_count}")

            except asyncio.CancelledError:
                # Client disconnected or request was cancelled
                status_code = 499  # Client Closed Request (nginx convention)
                error_msg_for_db = "Client disconnected"
                finish_reason_for_db = "cancelled"
                logger.warning(f"Stream cancelled for request {request_id}")
                if rate_limiter:
                    estimated_tokens = TokenCounter.estimate_tokens(
                        messages, params.get("max_tokens")
                    )
                    await rate_limiter.release_tokens(model, estimated_tokens)
                # Don't yield anything - client is gone
                # Re-raise to properly signal cancellation
                raise

            except Exception as exc:
                status_code = 500
                error_msg_for_db = str(exc)
                if rate_limiter:
                    estimated_tokens = TokenCounter.estimate_tokens(
                        messages, params.get("max_tokens")
                    )
                    await rate_limiter.release_tokens(model, estimated_tokens)

                error_chunk = {"error": {"message": str(exc), "type": "server_error", "code": 500}}
                error_msg = f"data: {json.dumps(error_chunk)}\n\n"
                logger.error(f"Yielding error chunk: {error_msg}")
                yield error_msg

            finally:
                # Emit TTFT prediction error metric (independent of DB logger)
                if routing_info and ttft_ms is not None:
                    est_ttft = (routing_info.get("outsourcing") or {}).get("est_ttft_seconds")
                    if est_ttft is not None:
                        try:
                            error = est_ttft - (ttft_ms / 1000.0)
                            NIMBUS_PREDICTION_ERROR.labels(
                                model=normalize_model_label(model)
                            ).observe(error)
                        except Exception:
                            pass  # best-effort; never break streaming

                # Always log to DB using UPSERT with asyncio.shield to prevent cancellation
                if db_logger:
                    # Get pricing from actual provider used
                    pricing = None
                    if routing_info:
                        stream_provider = routing_info.get("provider", "router")
                        base_url = routing_info.get("base_url")
                        pricing = get_pricing_for_provider(stream_provider, base_url)
                        metadata.update(routing_info)
                    elif provider_from_ctx:
                        stream_provider = provider_from_ctx
                        pricing = get_pricing_for_provider(stream_provider, None)

                    # Reconstruct response for DB logging
                    response_for_db: dict[str, Any] = {
                        "id": request_id,
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": final_text if final_text else None,
                                },
                                "finish_reason": finish_reason_for_db,
                            }
                        ],
                    }
                    if tool_calls_map:
                        tool_calls_list = [tc for _, tc in sorted(tool_calls_map.items())]
                        response_for_db["choices"][0]["message"]["tool_calls"] = tool_calls_list
                    if usage_data:
                        response_for_db["usage"] = normalize_usage(usage_data) or usage_data

                    try:
                        await asyncio.shield(
                            db_logger.upsert_request(
                                request_id=request_id,
                                model_id=model,
                                provider=stream_provider,
                                prompt=messages,
                                response=response_for_db if status_code == 200 else None,
                                usage=response_for_db.get("usage") if status_code == 200 else None,
                                latency_ms=int((time.time() - start_time) * 1000),
                                status_code=status_code,
                                error=error_msg_for_db,
                                params=params,
                                metadata=metadata,
                                ttft_ms=ttft_ms,
                                pricing=pricing,
                            )
                        )
                    except Exception as e:
                        logger.error(f"Failed to upsert request {request_id}: {e}")

        logger.debug(f"Creating StreamingResponse for model: {model}")

        # Record 200 for streaming response (HTTP layer success)
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model),
            provider=normalize_provider_label(provider),
            status_code="200",
        ).inc()

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming path
    try:
        if use_nimbus:
            response = await nimbus_router.chat_completion(model, messages, **params)
        else:
            response = await router_exec.chat_completion(model, messages, **params)

        # Always sanitize internal routing metadata from response to client
        provider = "router"
        base_url = None
        if isinstance(response, dict) and "_routing" in response:
            try:
                provider = response["_routing"].get("provider", "router")
                base_url = response["_routing"].get("base_url")
                # Enrich metadata for analytics; safe to skip if no DB logger
                metadata.update(response["_routing"])  # type: ignore[arg-type]
            except Exception:
                # Do not let metadata processing impact client response
                pass
            # Never leak internal routing details to clients
            with suppress(Exception):
                del response["_routing"]
        else:
            # Fallback: get provider from request context when _routing is not available
            from serving.utils import context as req_ctx

            ctx = req_ctx.get()
            if ctx and "provider" in ctx:
                provider = ctx["provider"]
                logger.debug(
                    f"Using provider from context for non-streaming DB logging: {provider}"
                )

        # Log request to DB using UPSERT.
        if db_logger:
            pricing = get_pricing_for_provider(provider, base_url)
            # Normalize usage to extract reasoning_tokens from nested locations
            normalized_usage = normalize_usage(response.get("usage"))
            log_params = dict(params)
            if log_params.get("max_tokens") is None:
                log_params["max_tokens"] = getattr(
                    get_adapter_config_for_provider(provider, base_url),
                    "max_output_length",
                    None,
                )

            await db_logger.upsert_request(
                request_id=request_id,
                model_id=model,
                provider=provider,
                prompt=messages,
                response=response,
                usage=normalized_usage,
                latency_ms=int((time.time() - start_time) * 1000),
                status_code=200,
                params=log_params,
                metadata=metadata,
                pricing=pricing,
            )

        # Emit token counters when usage is available, with anomaly checks
        # Normalize usage to extract reasoning_tokens from nested locations
        raw_usage = response.get("usage", {}) if isinstance(response, dict) else {}
        usage = normalize_usage(raw_usage) or {}
        if usage:
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

        if rate_limiter:
            actual_tokens = response.get("usage", {}).get("total_tokens")
            if actual_tokens:
                estimated = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
                if abs(actual_tokens - estimated) > estimated * 0.2:
                    logger.warning(
                        f"Token estimation variance for {model}: estimated {estimated}, actual {actual_tokens}"
                    )

        # Record 200 for non-streaming response
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model),
            provider=normalize_provider_label(provider),
            status_code="200",
        ).inc()

        return response

    except asyncio.CancelledError:
        # Client disconnected or request was cancelled
        logger.warning(f"Non-streaming request cancelled: {request_id}")
        if rate_limiter:
            estimated_tokens = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
            await rate_limiter.release_tokens(model, estimated_tokens)
        if db_logger:
            try:
                await asyncio.shield(
                    db_logger.upsert_request(
                        request_id=request_id,
                        model_id=model,
                        provider=provider,
                        prompt=messages,
                        response=None,
                        usage=None,
                        latency_ms=int((time.time() - start_time) * 1000),
                        status_code=499,
                        error="Client disconnected",
                        params=params,
                        metadata=metadata,
                    )
                )
            except Exception as e:
                logger.error(f"Failed to upsert cancelled request {request_id}: {e}")
        raise

    except Exception as exc:
        # Best-effort extraction of status code from different SDK exception shapes.
        exc_status_code = None
        if hasattr(exc, "status_code") and exc.status_code is not None:
            exc_status_code = exc.status_code
        elif hasattr(exc, "response") and exc.response is not None:
            if hasattr(exc.response, "status_code"):
                exc_status_code = exc.response.status_code
            elif hasattr(exc.response, "status"):
                exc_status_code = exc.response.status
        elif hasattr(exc, "status") and exc.status is not None:
            exc_status_code = exc.status
        elif hasattr(exc, "code") and exc.code is not None:
            exc_status_code = exc.code
        if exc_status_code is None:
            exc_status_code = 500

        from serving.utils import context as req_ctx

        ctx = req_ctx.get()
        provider_for_error = ctx.get("provider", provider) if ctx else provider

        if rate_limiter:
            estimated_tokens = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
            await rate_limiter.release_tokens(model, estimated_tokens)

        if db_logger:
            try:
                await db_logger.upsert_request(
                    request_id=request_id,
                    model_id=model,
                    provider=provider_for_error,
                    prompt=messages,
                    response=None,
                    usage=None,
                    latency_ms=int((time.time() - start_time) * 1000),
                    status_code=exc_status_code,
                    error=str(exc),
                    params=params,
                    metadata=metadata,
                )
            except Exception as db_exc:
                logger.error(f"Failed to upsert failed request {request_id}: {db_exc}")

        API_MODEL_REQUESTS.labels(
            model=normalize_model_label(model),
            provider=normalize_provider_label(provider_for_error),
            status_code=str(exc_status_code),
        ).inc()

        raise HTTPException(exc_status_code, str(exc)) from exc
