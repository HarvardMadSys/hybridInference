"""Compatibility wrappers for legacy completion endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request, Response

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_log_store,
    get_model_router_registry,
    get_operational_store,
    get_router,
)

from .completions import chat_completions

router = APIRouter()


@router.post("/completion")
async def single_completion(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    op_store=Depends(get_operational_store),
    model_router_registry=Depends(get_model_router_registry),
    completions_logger=Depends(get_completions_logger),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    """Compatibility alias for single-shot completion requests.

    Forwards to /v1/chat/completions using the provided payload.
    """
    return await chat_completions(
        request,
        http_response,
        authorization=authorization,
        user_ctx=user_ctx,
        router_exec=router_exec,
        log_store=log_store,
        op_store=op_store,
        model_router_registry=model_router_registry,
        completions_logger=completions_logger,
    )


@router.post("/v1/completions")
async def legacy_completions(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    op_store=Depends(get_operational_store),
    model_router_registry=Depends(get_model_router_registry),
    completions_logger=Depends(get_completions_logger),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    """OpenAI-style legacy completions endpoint: convert to chat format."""
    body = await request.json()
    prompt = body.get("prompt", "")
    messages = [{"role": "user", "content": prompt}]
    body["messages"] = messages
    body.pop("prompt", None)
    request._body = json.dumps(body).encode()  # type: ignore[attr-defined]
    return await chat_completions(
        request,
        http_response,
        authorization=authorization,
        user_ctx=user_ctx,
        router_exec=router_exec,
        log_store=log_store,
        op_store=op_store,
        model_router_registry=model_router_registry,
        completions_logger=completions_logger,
    )
