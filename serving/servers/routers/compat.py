from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Request, Depends, Header

from .completions import chat_completions
from serving.servers.deps import (
    get_router,
    get_rate_limiter,
    get_db_logger,
)


router = APIRouter()


@router.post("/completion")
async def single_completion(
    request: Request,
    authorization: Optional[str] = Header(None),
    router_exec = Depends(get_router),
    rate_limiter = Depends(get_rate_limiter),
    db_logger = Depends(get_db_logger),
):
    """Compatibility alias for single-shot completion requests.
    Forwards to /v1/chat/completions using the provided payload.
    """
    return await chat_completions(
        request,
        authorization,
        router_exec,
        rate_limiter,
        db_logger,
    )


@router.post("/v1/completions")
async def legacy_completions(
    request: Request,
    authorization: Optional[str] = Header(None),
    router_exec = Depends(get_router),
    rate_limiter = Depends(get_rate_limiter),
    db_logger = Depends(get_db_logger),
):
    """OpenAI-style legacy completions endpoint: convert to chat format.

    Converts {prompt: "..."} into messages list and forwards to chat endpoint.
    """
    body = await request.json()
    prompt = body.get("prompt", "")
    messages = [{"role": "user", "content": prompt}]
    body["messages"] = messages
    body.pop("prompt", None)
    request._body = json.dumps(body).encode()  # type: ignore[attr-defined]
    return await chat_completions(
        request,
        authorization,
        router_exec,
        rate_limiter,
        db_logger,
    )
