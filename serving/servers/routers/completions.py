from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from serving.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
)
from serving.servers.deps import get_router, get_rate_limiter, get_db_logger
from serving.servers.rate_limiter import TokenCounter
from utils.logging_utils import get_logger


logger = get_logger(__name__)
router = APIRouter()


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
    authorization: Optional[str] = Header(None),
    router_exec = Depends(get_router),
    rate_limiter = Depends(get_rate_limiter),
    db_logger = Depends(get_db_logger),
) -> Dict[str, Any]:
    """Handle chat completion requests with routing and fallback.

    Parses the request body using Pydantic for validation, then routes
    to the appropriate adapter. Streaming and non-streaming flows are
    both supported.
    """
    try:
        body = await request.json()
        payload = ChatCompletionRequest.model_validate(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON or schema in request body")

    model = payload.model
    messages = [m.model_dump() for m in payload.messages]

    # Check if model has routing configured
    if model not in router_exec.routes:
        raise HTTPException(404, f"Model '{model}' not found")

    # Extract parameters
    params: Dict[str, Any] = {}
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
    if payload.tools is not None:
        params["tools"] = payload.tools
    if payload.tool_choice is not None:
        params["tool_choice"] = payload.tool_choice
    if payload.response_format is not None:
        params["response_format"] = payload.response_format.model_dump()

    # Rate limit check with advanced features
    if rate_limiter:
        priority = 1 if authorization else 0  # Higher priority for authenticated requests
        success, meta = await rate_limiter.acquire_tokens(
            model_id=model,
            messages=messages,
            max_tokens=params.get("max_tokens"),
            priority=priority,
            timeout=30.0,
        )

        if not success:
            error_detail: Dict[str, Any] = {
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

            headers: Dict[str, str] = {
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

            raise HTTPException(status_code=429, detail=error_detail, headers=headers)

    # Generate request ID and metadata
    request_id = f"req_{int(time.time() * 1000000)}"
    start_time = time.time()
    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "ip": request.client.host if request.client else None,
        "authorization": bool(authorization),
    }

    # Streaming path
    if payload.stream:
        async def stream_generator():
            try:
                async for chunk in router_exec.stream_chat_completion(model, messages, **params):
                    # Forward adapter SSE chunks directly. Adapters emit final usage chunk.
                    yield chunk

                if db_logger:
                    await db_logger.log_request(
                        request_id=request_id,
                        model_id=model,
                        provider="router",
                        prompt=messages,
                        response={"stream": True},
                        usage=None,
                        latency_ms=int((time.time() - start_time) * 1000),
                        status_code=200,
                        params=params,
                        metadata=metadata,
                    )
            except Exception as exc:
                if db_logger:
                    await db_logger.log_request(
                        request_id=request_id,
                        model_id=model,
                        provider="router",
                        prompt=messages,
                        response=None,
                        usage=None,
                        latency_ms=int((time.time() - start_time) * 1000),
                        status_code=500,
                        error=str(exc),
                        params=params,
                        metadata=metadata,
                    )
                if rate_limiter:
                    estimated_tokens = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
                    await rate_limiter.release_tokens(model, estimated_tokens)

                error_chunk = {
                    "error": {"message": str(exc), "type": "server_error", "code": 500}
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming path
    try:
        response = await router_exec.chat_completion(model, messages, **params)

        if db_logger:
            provider = "router"
            if "_routing" in response:
                provider = response["_routing"]["provider"]
                metadata.update(response["_routing"])  # type: ignore[arg-type]
                del response["_routing"]

            await db_logger.log_request(
                request_id=request_id,
                model_id=model,
                provider=provider,
                prompt=messages,
                response=response,
                usage=response.get("usage"),
                latency_ms=int((time.time() - start_time) * 1000),
                status_code=200,
                params=params,
                metadata=metadata,
            )

        if rate_limiter:
            actual_tokens = response.get("usage", {}).get("total_tokens")
            if actual_tokens:
                estimated = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
                if abs(actual_tokens - estimated) > estimated * 0.2:
                    logger.warning(
                        f"Token estimation variance for {model}: estimated {estimated}, actual {actual_tokens}"
                    )

        return response

    except Exception as exc:
        if rate_limiter:
            estimated_tokens = TokenCounter.estimate_tokens(messages, params.get("max_tokens"))
            await rate_limiter.release_tokens(model, estimated_tokens)
        if db_logger:
            await db_logger.log_request(
                request_id=request_id,
                model_id=model,
                provider="router",
                prompt=messages,
                response=None,
                usage=None,
                latency_ms=int((time.time() - start_time) * 1000),
                status_code=500,
                error=str(exc),
                params=params,
                metadata=metadata,
            )
        raise HTTPException(500, str(exc))

