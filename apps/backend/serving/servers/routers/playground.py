"""Admin-only API playground for testing LLM models interactively."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from routing.endpoints import endpoint_id_for_adapter
from serving.servers.deps import get_router, require_role
from serving.stream import make_role_chunk

router = APIRouter(prefix="/internal/playground", tags=["Playground"])

# Keys that should never be sent to the client (internal routing metadata).
_INTERNAL_KEYS = frozenset({"_routing"})


class PlaygroundProviderItem(BaseModel):
    """One routable provider backend for a model."""

    id: str
    name: str


class PlaygroundModelItem(BaseModel):
    """Canonical model metadata exposed to the admin playground."""

    id: str
    name: str
    provider: str
    providers: list[PlaygroundProviderItem] = []


# Human-friendly display names for provider kinds.
_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "chutes": "Chutes",
    "featherless": "Featherless AI",
    "ollama": "Ollama",
    "zai": "ZAI",
    "openai_compat": "OpenAI Compatible",
    "staging": "Staging (OpenAI Compatible)",
    "sglang": "SGLang",
    "deepseek": "DeepSeek",
    "minimax": "Minimax",
}


_BASE_URL_DISPLAY_NAMES: dict[str, str] = {
    "api.minimax.io": "Minimax",
    "llm.chutes.ai": "Chutes",
    "api.featherless.ai": "Featherless AI",
    "ollama.com": "Ollama",
    "api.z.ai": "ZAI",
    "api.together.ai": "Together AI",
}


def _provider_display_name(endpoint_id: str, base_url: str = "") -> str:
    """Return a human-friendly provider name.

    Tries base_url host matching first (more specific), then falls back
    to the endpoint_id mapping.
    """
    if base_url:
        from urllib.parse import urlparse

        host = urlparse(base_url).netloc.lower().split(":")[0]
        for pattern, name in _BASE_URL_DISPLAY_NAMES.items():
            if pattern in host:
                return name
    return _PROVIDER_DISPLAY_NAMES.get(endpoint_id, endpoint_id)


@router.get("/models")
async def list_models(
    _admin: dict[str, Any] = Depends(require_role("internal")),
    router_exec=Depends(get_router),
) -> dict[str, Any]:
    """Return canonical model metadata for the playground selector."""
    canonical_models: dict[str, PlaygroundModelItem] = {}

    for route in router_exec.routes.values():
        if not route.adapters:
            continue

        primary_cfg = route.adapters[0][0].config
        canonical_id = primary_cfg.id
        if canonical_id in canonical_models:
            continue

        providers: list[PlaygroundProviderItem] = []
        seen: set[str] = set()
        for adapter, weight in route.adapters:
            if weight <= 0:
                continue
            eid = endpoint_id_for_adapter(adapter)
            if eid in seen:
                continue
            seen.add(eid)
            base_url = getattr(adapter.config, "base_url", "")
            providers.append(
                PlaygroundProviderItem(
                    id=eid,
                    name=_provider_display_name(eid, base_url),
                )
            )

        canonical_models[canonical_id] = PlaygroundModelItem(
            id=canonical_id,
            name=primary_cfg.name,
            provider=primary_cfg.provider,
            providers=providers,
        )

    models = sorted(
        canonical_models.values(),
        key=lambda model: (model.name.lower(), model.id.lower()),
    )
    return {"models": [model.model_dump() for model in models]}


class PlaygroundChatRequest(BaseModel):
    """Request body for playground chat completion."""

    model: str
    system_prompt: str = ""
    messages: list[dict[str, str]]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    provider: str | None = None


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
    _admin: dict[str, Any] = Depends(require_role("internal")),
    router_exec=Depends(get_router),
) -> StreamingResponse:
    """Stream a chat completion for admin testing."""
    effective_messages = body.messages
    if body.system_prompt.strip():
        effective_messages = [
            {"role": "system", "content": body.system_prompt.strip()},
            *body.messages,
        ]

    async def _generate():
        yield make_role_chunk(model=body.model)
        kwargs: dict[str, Any] = {
            "temperature": body.temperature,
            "max_tokens": body.max_tokens,
        }
        async for chunk in router_exec.stream_chat_completion(
            body.model,
            effective_messages,
            pin_provider=body.provider,
            **kwargs,
        ):
            with suppress(Exception):
                chunk = _sanitize_chunk(chunk)
            yield chunk

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            # `no-transform` stops intermediary CDNs (e.g. Cloudflare) from
            # buffering the stream to compress it, which collapses TTFT.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
