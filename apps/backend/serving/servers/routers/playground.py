"""Admin-only API playground for testing LLM models interactively."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from routing.endpoints import endpoint_id_for_adapter
from routing.protocols import RoutingRequestOptions
from serving.servers.deps import get_router, require_role
from serving.stream import make_role_chunk, new_completion_id, stamp_completion_id

router = APIRouter(prefix="/internal/playground", tags=["Playground"])

# The router's internal `_routing` blob never reaches the client verbatim: it
# carries the raw upstream base_url, which on this deployment can be a LAN
# address or a URL with embedded credentials. But *which* endpoint actually
# served a request is the one thing a routing gateway's playground most needs
# to show, so `_sanitize_chunk` republishes an allow-listed, host-only summary
# under `_PLAYGROUND_ROUTE_KEY` instead of dropping the field outright.
#
# Admin-only by construction: this republish lives here, not in the shared
# `sanitize_chunk` used by the public completions path. The name deliberately
# avoids containing `_routing` as a substring so that "the internal blob never
# appears on the wire" stays checkable with a plain substring assertion.
_ROUTING_KEY = "_routing"
_PLAYGROUND_ROUTE_KEY = "_playground_route"


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
        if not getattr(route, "published", True) or not route.adapters:
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


def _routing_host(base_url: str) -> str | None:
    """Return ``host[:port]`` for a base_url, dropping path, query and userinfo.

    Anything unparseable is dropped rather than echoed — the point of this
    helper is that no raw base_url reaches the browser. Note that ``urlparse``
    itself accepts a malformed port; it is ``SplitResult.port`` that raises,
    so the attribute access has to sit inside the guard too.
    """
    if not base_url:
        return None
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    return f"{host}:{port}" if port else host


def _redact_routing(routing: Any) -> dict[str, Any] | None:
    """Reduce the router's ``_routing`` blob to an admin-safe summary.

    Allow-list, not deny-list: fields the router adds later stay internal
    until someone deliberately surfaces them here.
    """
    if not isinstance(routing, dict):
        return None
    provider = str(routing.get("provider") or "")
    # Mirror `endpoint_id_for_adapter`: `config.endpoint_id` is optional and
    # the provider label is its canonical fallback.
    endpoint_id = str(routing.get("endpoint_id") or provider)
    if not endpoint_id:
        # Unreachable on the router the playground actually uses: `get_router`
        # hands back the FixedRouter, and `routing_chunk()` always stamps a
        # provider. RouteWise is the exception — its final decision frame is
        # enrichment-only (fallback + failed_attempts, no provider/endpoint,
        # see routewise/router.py `_attach_decision_info`) and would be dropped
        # here. It cannot reach this path because the playground never consults
        # `model_router_registry`. Whoever wires the playground to per-model
        # routers must make this branch merge into the running summary instead
        # of discarding it, and teach the UI merge- rather than replace-
        # semantics — otherwise RouteWise fallbacks show up as clean successes.
        return None

    summary: dict[str, Any] = {"provider": provider, "endpoint_id": endpoint_id}
    host = _routing_host(str(routing.get("base_url") or ""))
    if host:
        summary["host"] = host
    if routing.get("fallback"):
        summary["fallback"] = True

    attempts = routing.get("failed_attempts")
    if isinstance(attempts, list) and attempts:
        # Only the *shape* of each failed attempt travels. The raw `error`
        # string can carry an upstream response body, so it stays server-side
        # until the in-band error-frame work gives it a vetted channel.
        summary["failed_attempts"] = [
            {
                "provider": str(attempt.get("provider") or ""),
                "endpoint_id": str(attempt.get("endpoint_id") or attempt.get("provider") or ""),
                "error_type": str(attempt.get("error_type") or ""),
            }
            for attempt in attempts
            if isinstance(attempt, dict)
        ]
    return summary


def _sanitize_chunk(chunk: str, completion_id: str) -> str:
    """Swap internal routing metadata for an admin-safe summary.

    Also relabels the frame with this response's ``completion_id``, since
    adapters mint one id per chunk and clients group by it.

    Only the router's own synthetic routing frame — ``routing_chunk()``, the
    one with an empty ``choices`` list — is republished. Every other carrier of
    ``_routing`` is stripped outright: ``make_final_usage_chunk`` attaches a
    provider/base_url pair for cost accounting with no endpoint identity in it,
    and since it is the *last* frame of the stream, republishing it would
    overwrite a precise ``glm-4.6:zai-api`` badge with a bare ``zai``.

    Total by contract: this must never raise. `_routing` is popped before the
    summary is built, so a redaction failure costs the badge, never the
    redaction — the caller yields whatever comes back.
    """
    if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
        return chunk
    try:
        obj = json.loads(chunk[6:])
    except Exception:
        return chunk
    if not isinstance(obj, dict):
        return chunk

    stamp_completion_id(obj, completion_id)

    if _ROUTING_KEY in obj:
        routing = obj.pop(_ROUTING_KEY)
        if not obj.get("choices"):
            try:
                summary = _redact_routing(routing)
            except Exception:
                summary = None
            if summary:
                obj[_PLAYGROUND_ROUTE_KEY] = summary
    return f"data: {json.dumps(obj)}\n\n"


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
        completion_id = new_completion_id()
        yield make_role_chunk(model=body.model, completion_id=completion_id)
        kwargs: dict[str, Any] = {
            "temperature": body.temperature,
            "max_tokens": body.max_tokens,
        }
        async for chunk in router_exec.stream_chat_completion(
            body.model,
            effective_messages,
            routing_options=RoutingRequestOptions(pin_provider=body.provider),
            **kwargs,
        ):
            # Deliberately unguarded: `suppress` here would fail *open* —
            # a raise inside the redactor would leave `chunk` at its original
            # value and hand the raw `_routing` blob, base_url and all, to the
            # browser. `_sanitize_chunk` is total instead.
            yield _sanitize_chunk(chunk, completion_id)

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
