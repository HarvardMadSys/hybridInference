"""Admin-only API playground for testing LLM models interactively."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from serving.servers.deps import get_router, require_admin
from serving.stream import make_role_chunk

router = APIRouter(prefix="/internal/playground", tags=["Playground"])

# Keys that should never be sent to the client (internal routing metadata).
_INTERNAL_KEYS = frozenset({"_routing"})


@router.get("/models")
async def list_models(
    _admin: dict[str, Any] = Depends(require_admin),
    router_exec=Depends(get_router),
) -> dict[str, Any]:
    """Return available model IDs for the playground."""
    model_ids = sorted(router_exec.routes.keys())
    return {"models": model_ids}


class PlaygroundChatRequest(BaseModel):
    """Request body for playground chat completion."""

    model: str
    messages: list[dict[str, str]]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=32768)


def _sanitize_chunk(chunk: str) -> str:
    """Strip internal metadata from an SSE chunk before sending to the client."""
    if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
        return chunk
    try:
        obj = json.loads(chunk[6:])
        changed = False
        for key in _INTERNAL_KEYS:
            if key in obj:
                del obj[key]
                changed = True
        if changed:
            return f"data: {json.dumps(obj)}\n\n"
    except Exception:
        pass
    return chunk


@router.post("/chat")
async def playground_chat(
    body: PlaygroundChatRequest,
    _admin: dict[str, Any] = Depends(require_admin),
    router_exec=Depends(get_router),
) -> StreamingResponse:
    """Stream a chat completion for admin testing."""

    async def _generate():
        yield make_role_chunk(model=body.model)
        async for chunk in router_exec.stream_chat_completion(
            body.model,
            body.messages,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        ):
            with suppress(Exception):
                chunk = _sanitize_chunk(chunk)
            yield chunk

    return StreamingResponse(_generate(), media_type="text/event-stream")
