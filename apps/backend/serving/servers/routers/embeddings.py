"""OpenAI-compatible embeddings endpoint."""

from __future__ import annotations

import time
import uuid
from typing import Any

import aiohttp
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from serving.schemas import EmbeddingRequest, EmbeddingResponse, ErrorResponse
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
)
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

logger = get_logger(__name__)
router = APIRouter()


def _response_summary(response: Any, model: str) -> dict[str, Any]:
    """Build a compact log-friendly summary of an embeddings response.

    Full embedding vectors are large (hundreds/thousands of floats per input)
    and not useful in the request log, so we persist only counts and the
    reported usage rather than the raw ``data`` array.

    Fully defensive against unexpected response shapes: this runs on the
    success path, so a crash here must never turn a successful embedding into
    a 500 for the client.
    """
    if not isinstance(response, dict):
        return {
            "object": "list",
            "model": model,
            "data_count": 0,
            "dimensions": None,
            "usage": None,
        }

    data = response.get("data")
    data_list = data if isinstance(data, list) else []
    dimensions: int | None = None
    if data_list:
        first = data_list[0].get("embedding") if isinstance(data_list[0], dict) else None
        if isinstance(first, list):
            dimensions = len(first)
    return {
        "object": response.get("object", "list"),
        "model": response.get("model", model),
        "data_count": len(data_list),
        "dimensions": dimensions,
        "usage": response.get("usage"),
    }


@router.post(
    "/v1/embeddings",
    response_model=EmbeddingResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        404: {"model": ErrorResponse, "description": "Model Not Found"},
        500: {"model": ErrorResponse, "description": "Server Error"},
    },
)
async def create_embeddings(
    request: EmbeddingRequest,
    http_request: Request,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    log_store=Depends(get_log_store),
    completions_logger=Depends(get_completions_logger),
    _concurrency_slot=Depends(enforce_user_concurrency),
) -> dict[str, Any]:
    """Create embeddings for the given input text(s).

    Routes to the appropriate embedding adapter based on the requested model.
    The request is logged to the shared ``api_logs`` store (tagged
    ``request_type=embedding``) so it surfaces in the dashboards alongside
    chat/completions traffic.
    """
    model = request.model
    request_id = f"emb_{uuid.uuid4().hex}"
    start_time = time.time()
    is_authenticated = bool(user_ctx.get("authenticated"))
    session_id = http_request.headers.get("X-Session-ID")

    metadata: dict[str, Any] = {
        "request_type": "embedding",
        "user_agent": http_request.headers.get("user-agent"),
        "ip": get_client_ip(http_request),
        "authorization": bool(authorization) or is_authenticated,
        "authenticated": is_authenticated,
        "user_id": user_ctx.get("user_id"),
    }
    if session_id:
        metadata["session_id"] = session_id

    def _schedule_log(
        *,
        provider: str,
        status_code: int,
        response: dict[str, Any] | None,
        usage: dict[str, Any] | None,
        pricing: dict[str, str] | None,
        error: str | None,
    ) -> None:
        if log_store is None or completions_logger is None:
            return
        completions_logger.schedule_log(
            request_id,
            {
                "request_id": request_id,
                "model_id": model,
                "provider": provider,
                "prompt": request.input,
                "response": response,
                "usage": usage,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status_code": status_code,
                "error": error,
                "params": {
                    "encoding_format": request.encoding_format,
                    "dimensions": request.dimensions,
                },
                "metadata": metadata,
                "pricing": pricing,
                "request_payload": request.model_dump(exclude_none=True),
            },
        )

    if model not in embedding_adapters:
        _schedule_log(
            provider="router",
            status_code=404,
            response=None,
            usage=None,
            pricing=None,
            error=f"Embedding model '{model}' not found",
        )
        raise HTTPException(404, f"Embedding model '{model}' not found")

    adapter = embedding_adapters[model]
    provider = getattr(getattr(adapter, "config", None), "provider", None) or "unknown"
    pricing = getattr(getattr(adapter, "config", None), "pricing", None)

    params: dict[str, Any] = {}
    if request.encoding_format is not None:
        params["encoding_format"] = request.encoding_format
    if request.dimensions is not None:
        params["dimensions"] = request.dimensions

    try:
        response = await adapter.embeddings(request.input, **params)
        _schedule_log(
            provider=provider,
            status_code=200,
            response=_response_summary(response, model),
            usage=response.get("usage") if isinstance(response, dict) else None,
            pricing=pricing,
            error=None,
        )
        return response
    except aiohttp.ClientResponseError as exc:
        logger.error(f"Embedding request failed for model={model}: {exc.status} {exc.message}")
        _schedule_log(
            provider=provider,
            status_code=exc.status,
            response=None,
            usage=None,
            pricing=None,
            error=f"{exc.status} {exc.message}",
        )
        raise HTTPException(exc.status, "Embedding service error") from exc
    except Exception as exc:
        logger.error(f"Embedding request failed for model={model}: {exc}")
        _schedule_log(
            provider=provider,
            status_code=500,
            response=None,
            usage=None,
            pricing=None,
            error=str(exc),
        )
        raise HTTPException(500, "Embedding service error") from exc
