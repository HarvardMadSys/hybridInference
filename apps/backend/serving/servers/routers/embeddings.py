"""OpenAI-compatible embeddings endpoint."""

from __future__ import annotations

from typing import Any

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from serving.schemas import EmbeddingRequest, EmbeddingResponse, ErrorResponse
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import get_embedding_adapters
from serving.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


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
    user_ctx: dict = Depends(verify_api_key),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    _concurrency_slot=Depends(enforce_user_concurrency),
) -> dict[str, Any]:
    """Create embeddings for the given input text(s).

    Routes to the appropriate embedding adapter based on the requested model.
    """
    model = request.model

    if model not in embedding_adapters:
        raise HTTPException(404, f"Embedding model '{model}' not found")

    adapter = embedding_adapters[model]

    params: dict[str, Any] = {}
    if request.encoding_format is not None:
        params["encoding_format"] = request.encoding_format
    if request.dimensions is not None:
        params["dimensions"] = request.dimensions

    try:
        response = await adapter.embeddings(request.input, **params)
        return response
    except aiohttp.ClientResponseError as exc:
        logger.error(f"Embedding request failed for model={model}: {exc.status} {exc.message}")
        raise HTTPException(502, "Embedding service error") from exc
    except Exception as exc:
        logger.error(f"Embedding request failed for model={model}: {exc}")
        raise HTTPException(500, "Embedding service error") from exc
