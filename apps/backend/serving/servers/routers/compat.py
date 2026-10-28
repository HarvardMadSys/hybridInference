"""Compatibility wrappers for legacy completion endpoints.

Both are served by the chat execution (``completions.execute_chat``) under
their own concurrency dependency, with the runtime settings the chat route
uses, and answered in the chat route's shape.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from serving.config.runtime_settings import RuntimeSettings, get_runtime_settings
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_cost_tracker,
    get_log_store,
    get_model_router_registry,
    get_model_visibility_resolver,
    get_pricing_lookup,
    get_router,
)

from .completions import ChatServices, serve_chat_http

router = APIRouter()


@router.post("/completion")
async def single_completion(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    model_router_registry=Depends(get_model_router_registry),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    runtime_settings: RuntimeSettings | None = Depends(get_runtime_settings),
    completions_logger=Depends(get_completions_logger),
    pricing_lookup=Depends(get_pricing_lookup),
    cost_tracker=Depends(get_cost_tracker),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    """Compatibility alias for single-shot completion requests.

    Served as /v1/chat/completions serves the provided payload.
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, "Invalid JSON or schema in request body") from e
    return await serve_chat_http(
        body,
        request,
        http_response,
        authorization=authorization,
        user_ctx=user_ctx,
        concurrency_slot=_concurrency_slot,
        services=_chat_services(
            request,
            router_exec,
            log_store,
            model_router_registry,
            model_visibility_resolver,
            completions_logger,
            pricing_lookup,
            cost_tracker,
        ),
        runtime_settings=runtime_settings,
    )


@router.post("/v1/completions")
async def legacy_completions(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    model_router_registry=Depends(get_model_router_registry),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    runtime_settings: RuntimeSettings | None = Depends(get_runtime_settings),
    completions_logger=Depends(get_completions_logger),
    pricing_lookup=Depends(get_pricing_lookup),
    cost_tracker=Depends(get_cost_tracker),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    """OpenAI-style legacy completions endpoint: convert to chat format."""
    body = await request.json()
    prompt = body.get("prompt", "")
    messages = [{"role": "user", "content": prompt}]
    body["messages"] = messages
    body.pop("prompt", None)
    return await serve_chat_http(
        body,
        request,
        http_response,
        authorization=authorization,
        user_ctx=user_ctx,
        concurrency_slot=_concurrency_slot,
        services=_chat_services(
            request,
            router_exec,
            log_store,
            model_router_registry,
            model_visibility_resolver,
            completions_logger,
            pricing_lookup,
            cost_tracker,
        ),
        runtime_settings=runtime_settings,
    )


def _chat_services(
    request: Request,
    router_exec,
    log_store,
    model_router_registry,
    model_visibility_resolver,
    completions_logger,
    pricing_lookup,
    cost_tracker,
) -> ChatServices:
    return ChatServices(
        router=router_exec,
        model_router_registry=model_router_registry,
        visibility=model_visibility_resolver,
        log_store=log_store,
        completions_logger=completions_logger,
        pricing_lookup=pricing_lookup,
        cost_tracker=cost_tracker,
        app_services=getattr(request.app.state, "services", None),
    )
