"""API playground for testing LLM models interactively, for internal users and above."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from routing.endpoints import endpoint_id_for_adapter
from routing.protocols import RoutingRequestOptions
from serving.config.settings import has_role
from serving.exceptions import scrub_error_for_user
from serving.model_access import model_access_denial
from serving.pricing import require_route_pricing
from serving.servers.auth import publish_caller_role
from serving.servers.deps import (
    get_current_user,
    get_model_visibility_resolver,
    get_router,
    get_user_concurrency_limiter,
    require_role,
)
from serving.servers.model_availability import model_not_found_detail
from serving.servers.request_body import json_body_after, json_body_schema
from serving.servers.routers.completions_stream import _extract_exception_status_code
from serving.stream import (
    StreamQueue,
    make_role_chunk,
    new_completion_id,
    pump_stream,
    stamp_completion_id,
)
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/playground", tags=["Playground"])

# How the stream stays answerable, as on the public surfaces
# (``completions_stream``, ``anthropic_messages``). The console reaches this
# route through the Next.js rewrite proxy, which drops an upstream that sends no
# byte for 30s (its default ``proxyTimeout``) and, once a response has started,
# never passes an upstream disconnect on to the browser. A quiet prefill, or any
# failure -- the router raises once a stream has started, including "all
# providers failed" after the role chunk -- therefore left the page
# "generating" until the user pressed Stop. So: a comment line every
# ``_KEEPALIVE_INTERVAL`` keeps the proxy reading, a failure ends the stream
# with an in-band error frame instead of a dropped connection, and
# ``_MAX_IDLE_S`` bounds how long a silent upstream is waited on -- the
# heartbeat would otherwise keep a stalled request "generating" until the
# hour-long stream cap. 300s is the first-frame ceiling of the Anthropic surface,
# whose comment explains why a long prefill needs that much.
_KEEPALIVE_INTERVAL = 15
_MAX_IDLE_S = 300
_STREAM_END: Any = object()

# The router's internal `_routing` blob never reaches the client verbatim: it
# carries the raw upstream base_url, which on this deployment can be a LAN
# address or a URL with embedded credentials. But *which* endpoint actually
# served a request is the one thing a routing gateway's playground most needs
# to show, so `_sanitize_chunk` republishes an allow-listed, host-only summary
# under `_PLAYGROUND_ROUTE_KEY` instead of dropping the field outright.
#
# Internal-only by construction: this republish lives here, not in the shared
# `sanitize_chunk` used by the public completions path, and the upstream's
# host:port in it is shown to administrators only. The name deliberately avoids
# containing `_routing` as a substring so that "the internal blob never appears
# on the wire" stays checkable with a plain substring assertion.
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


def _is_admin(user: dict[str, Any]) -> bool:
    """Return whether the playground's caller is an administrator."""
    return has_role(str(user.get("role") or "free"), "admin")


async def _playground_slot(
    user: dict[str, Any] = Depends(get_current_user),
    limiter: Any = Depends(get_user_concurrency_limiter),
) -> AsyncGenerator[None, None]:
    """Hold one of the caller's concurrency slots for the request, or refuse with 429.

    The playground runs on the console session, not an API key, so the API
    surfaces' admission (``servers.concurrency.enforce_user_concurrency``) never
    reaches it. It takes a slot from the same per-user budget instead, and
    FastAPI tears this dependency down once the response has been sent, the
    whole stream included, so the slot is held exactly as long as the upstream
    work it admits.
    """
    if limiter is None:
        yield
        return
    user_id = str(user["user_id"])
    granted, limit, _role_label = await limiter.try_acquire(
        user_id,
        str(user.get("role") or "free"),
        bool(user.get("is_admin", False)),
        user.get("max_concurrent_requests"),
    )
    if not granted:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent requests (limit: {limit}).",
            headers={"Retry-After": "1"},
        )
    try:
        yield
    finally:
        limiter.release(user_id)


@router.get("/models")
async def list_models(
    _admin: dict[str, Any] = Depends(require_role("internal")),
    router_exec=Depends(get_router),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
) -> dict[str, Any]:
    """Return canonical model metadata for the playground selector.

    Lists the models the caller may use, by the check every inference surface
    applies (``model_access_denial``). A model's routes, which a request may
    name to pin itself to one, are listed for administrators only, the only
    callers who may pin.
    """
    canonical_models: dict[str, PlaygroundModelItem] = {}
    show_routes = _is_admin(_admin)

    for model_id, route in router_exec.routes.items():
        if not getattr(route, "published", True) or not route.adapters:
            continue

        primary_cfg = route.adapters[0][0].config
        canonical_id = primary_cfg.id
        if canonical_id in canonical_models:
            continue
        denial = await model_access_denial(
            route,
            model_id,
            _admin,
            visibility_resolver=model_visibility_resolver,
            grant_scope=False,
        )
        if denial is not None:
            continue

        providers: list[PlaygroundProviderItem] = []
        seen: set[str] = set()
        for adapter, weight in route.adapters if show_routes else ():
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


def _redact_routing(routing: Any, *, show_host: bool = False) -> dict[str, Any] | None:
    """Reduce the router's ``_routing`` blob to a summary safe for the caller.

    Allow-list, not deny-list: fields the router adds later stay internal
    until someone deliberately surfaces them here. The upstream's host:port is
    included only with ``show_host``, for an administrator: it can be a LAN
    address, and the playground is open to internal users too.
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
    host = _routing_host(str(routing.get("base_url") or "")) if show_host else None
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


def _sanitize_chunk(chunk: str, completion_id: str, *, show_host: bool = False) -> str:
    """Swap internal routing metadata for a summary safe for the caller.

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
    redaction — the caller yields whatever comes back. ``show_host`` is passed
    to :func:`_redact_routing`.
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
                summary = _redact_routing(routing, show_host=show_host)
            except Exception:
                summary = None
            if summary:
                obj[_PLAYGROUND_ROUTE_KEY] = summary
    return f"data: {json.dumps(obj)}\n\n"


def _error_frame(message: str, code: int) -> str:
    """Return the in-band error frame that ends a failed stream."""
    error = {"message": message, "type": "server_error", "code": code}
    return f"data: {json.dumps({'error': error})}\n\n"


#: The playground's caller check, shared by the route and the body read below.
_require_internal = require_role("internal")
_chat_body = json_body_after(_require_internal, PlaygroundChatRequest)


# A pasted document can make a long prompt, so this route keeps the general
# body limit rather than the small one other /internal paths get
# (RequestBodyLimitMiddleware). Its body is therefore not declared, which
# FastAPI would read and decode before any dependency, the role check
# included: a dependency reads it once the caller is known.
@router.post("/chat", openapi_extra=json_body_schema(PlaygroundChatRequest))
async def playground_chat(
    body: PlaygroundChatRequest = Depends(_chat_body),
    _admin: dict[str, Any] = Depends(_require_internal),
    router_exec=Depends(get_router),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    _slot: None = Depends(_playground_slot),
) -> StreamingResponse:
    """Stream a chat completion for admin testing."""
    # The access check every inference surface applies, answered the same way
    # for every reason: the model does not exist. Internal users reach the
    # playground, and an admin-only model must stay out of their reach here too.
    route = router_exec.routes.get(body.model)
    if (
        route is None
        or await model_access_denial(
            route, body.model, _admin, visibility_resolver=model_visibility_resolver
        )
        is not None
    ):
        req_ctx.mark_model_not_found()
        raise HTTPException(404, model_not_found_detail(body.model, user_ctx=_admin))
    is_admin = _is_admin(_admin)
    if body.provider and not is_admin:
        # A pin skips circuit admission and reaches routes no weighted draw
        # would choose, a staging route included.
        raise HTTPException(403, "Choosing the route a request takes requires the admin role.")
    require_route_pricing(route)
    # The key pool reads it to keep upstream keys reserved for a higher tier
    # away from this caller, as on the API surfaces.
    publish_caller_role(_admin)
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
        # A reader task drains the router into a queue, so a heartbeat can go
        # out while it waits without cancelling the upstream read. Bounded: a
        # client that stops reading stops the reads from the upstream too.
        chunks = StreamQueue()
        # Nothing runs until the reader's first read, selection included.
        router_chunks = router_exec.stream_chat_completion(
            body.model,
            effective_messages,
            routing_options=RoutingRequestOptions(pin_provider=body.provider),
            **kwargs,
        )
        reader = asyncio.create_task(pump_stream(router_chunks, chunks, end=_STREAM_END))
        idle_s = 0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(chunks.get(), timeout=_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    idle_s += _KEEPALIVE_INTERVAL
                    if idle_s >= _MAX_IDLE_S:
                        logger.warning(
                            f"Playground stream idle for {idle_s}s, giving up: model={body.model}"
                        )
                        yield _error_frame(
                            f"The model sent nothing for {_MAX_IDLE_S}s, so the request "
                            "was stopped.",
                            504,
                        )
                        return
                    yield ": keepalive\n\n"
                    continue
                if item is _STREAM_END:
                    return
                if isinstance(item, Exception):
                    # exc_info: the frame carries only the scrubbed message.
                    logger.error(
                        f"Playground stream failed: model={body.model}",
                        exc_info=item,
                        extra={"event": "playground_stream_failed", "model": body.model},
                    )
                    code = _extract_exception_status_code(item)
                    request_id = req_ctx.get().get("request_id")
                    yield _error_frame(scrub_error_for_user(item, request_id, code), code)
                    return
                idle_s = 0
                # Deliberately unguarded: `suppress` here would fail *open* —
                # a raise inside the redactor would leave `chunk` at its original
                # value and hand the raw `_routing` blob, base_url and all, to the
                # browser. `_sanitize_chunk` is total instead.
                yield _sanitize_chunk(item, completion_id, show_host=is_admin)
        finally:
            # Also the client-disconnect path: cancelling the reader closes the
            # router's generator, which releases its lease and probe claims.
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader

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
