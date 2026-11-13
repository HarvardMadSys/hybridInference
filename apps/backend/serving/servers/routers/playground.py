"""API playground for testing LLM models interactively, for internal users and above."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from routing.endpoints import endpoint_id_for_adapter
from serving.config.runtime_settings import get_runtime_settings
from serving.config.settings import has_role
from serving.model_access import model_access_denial
from serving.model_effort_capabilities import model_effort_domain
from serving.servers.auth import (
    _account_quotas,
    _daily_quota_usd,
    _spend_check,
    publish_caller_role,
)
from serving.servers.concurrency import (
    _admit_spend,
    _slot_for_canonical,
    _track_interactive_request,
    canonical_model_id,
)
from serving.servers.deps import (
    get_completions_logger,
    get_cost_tracker,
    get_current_user,
    get_embedding_adapters,
    get_log_store,
    get_model_concurrency_resolver,
    get_model_router_registry,
    get_model_visibility_resolver,
    get_operational_store,
    get_pricing_lookup,
    get_response_store,
    get_router,
    get_user_concurrency_limiter,
    require_role,
)
from serving.servers.request_body import json_body_after, json_body_schema
from serving.servers.routers.anthropic_messages import (
    MALFORMED_BODY,
    MessagesConstraints,
    MessagesServices,
    MessagesStream,
    _unstarted_stream_release,
    execute_messages,
    render_messages_http,
)
from serving.servers.routers.completions import (
    ChatOptions,
    ChatServices,
    ChatStream,
    execute_chat,
    release_unstarted_chat_streams,
    render_chat_http,
)
from serving.servers.routers.responses import (
    ResponsesServices,
    ResponsesStream,
    execute_responses,
)
from serving.servers.surface_http import client_origin, timeout_probe
from serving.spend_ledger import SPEND_CHECK_KEY
from serving.surfaces.context import (
    ExecutionContext,
    InteractiveAdmission,
    admission_from,
    read_only,
)
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from serving.spend_ledger import Reservation

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/playground", tags=["Playground"])

# The router's internal `_routing` blob never reaches the client verbatim: it
# carries the raw upstream base_url, which on this deployment can be a LAN
# address or a URL with embedded credentials. But *which* endpoint actually
# served a request is the one thing a routing gateway's playground most needs
# to show, so `_route_frame` republishes an allow-listed, host-only summary
# under `_PLAYGROUND_ROUTE_KEY` instead of dropping the field outright.
#
# Internal-only by construction: this republish lives here, not in the shared
# `sanitize_chunk` used by the public completions path, and the upstream's
# host:port in it is shown to administrators only. The name deliberately avoids
# containing `_routing` as a substring so that "the internal blob never appears
# on the wire" stays checkable with a plain substring assertion.
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
    reasoning_efforts: list[str] = []


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


def _authenticated_caller(user: dict[str, Any]) -> dict[str, Any]:
    """The console session's caller, marked authenticated for logs and billing.

    ``get_current_user`` verifies the session but its context is built for the
    dashboard, not for inference: without this flag every playground row would
    log ``authenticated: false`` and cost the caller like an anonymous request.
    """
    return {**user, "authenticated": True}


async def _request_payload(request: Request) -> Any:
    """Read the request body once; None when it is not JSON.

    The route's own ``request.json()`` reuses Starlette's cached decode. Reading
    it here is what lets the admission reserve the request's own cost estimate,
    as the API surfaces do.
    """
    try:
        return await request.json()
    except Exception:
        return None


async def _reserve_playground_spend(
    request: Request,
    user: dict[str, Any],
    role: str,
    op_store: Any,
    router: Any,
    embedding_adapters: dict[str, Any] | None,
) -> Reservation | None:
    """Check the session user's account budget and reserve this request's estimate.

    The playground runs on the console session, not an API key, so it cannot
    reuse ``verify_api_key``'s gate. This is the ``X-On-Behalf-Of`` path: the
    user's account quotas, with no key attributed, and the same 429 the API
    surfaces return when over a limit. The check is then handed to the public
    admission (:func:`serving.servers.concurrency._admit_spend`) with the
    body's own model, so the reservation is the request's estimate rather than
    the flat minimum. ``_playground_slot`` releases it once the response, stream
    included, has been sent.
    """
    if op_store is None:
        return None
    user_id = str(user["user_id"])
    read_at = time.monotonic()
    daily, monthly = await _account_quotas(op_store, {"id": user_id, "role": role})
    identity = {
        "user_id": user_id,
        "role": role,
        "quota_daily_cost_usd": daily,
        "quota_monthly_cost_usd": monthly,
    }
    user[SPEND_CHECK_KEY] = await _spend_check(
        identity,
        api_key_id=None,
        op_store=op_store,
        quota_daily_cost_usd=_daily_quota_usd(identity),
        spent_today_usd=await op_store.get_user_cost_today(user_id) or 0.0,
        read_at=read_at,
    )
    payload = getattr(request, "_json", None)
    model = payload.get("model") if isinstance(payload, dict) else None
    return _admit_spend(
        request,
        user,
        model=model if isinstance(model, str) else None,
        router=router,
        embedding_adapters=embedding_adapters,
    )


async def _playground_slot(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    limiter: Any = Depends(get_user_concurrency_limiter),
    op_store: Any = Depends(get_operational_store),
    router_exec: Any = Depends(get_router),
    concurrency_resolver: Any = Depends(get_model_concurrency_resolver),
    embedding_adapters: dict[str, Any] | None = Depends(get_embedding_adapters),
) -> AsyncGenerator[InteractiveAdmission, None]:
    """Hold a slot, interactive load and spend reservation until the request ends.

    The playground runs on the console session, not an API key, so the API
    surfaces' admission (``servers.concurrency.enforce_user_concurrency``) never
    reaches it. This buys the same things from the same source: the per-model
    (or exempt) slot, the interactive-load count batch work yields to, and the
    spend reservation against the session user's quotas. FastAPI tears the
    dependency down once the response has been sent, the whole stream included.
    """
    payload = await _request_payload(request)
    model = payload.get("model") if isinstance(payload, dict) else None
    model = model if isinstance(model, str) else None
    canonical = canonical_model_id(model, router=router_exec, embedding_adapters=embedding_adapters)
    user_id = str(user["user_id"])
    role = str(user.get("role") or "free")
    slot_key = user_id
    if limiter is not None:
        slot_key, max_concurrent = await _slot_for_canonical(
            user, model, canonical, concurrency_resolver
        )
        granted, limit, _role_label = await limiter.try_acquire(
            slot_key,
            role,
            bool(user.get("is_admin", False)),
            max_concurrent,
        )
        if not granted:
            raise HTTPException(
                status_code=429,
                detail=f"Too many concurrent requests (limit: {limit}).",
                headers={"Retry-After": "1"},
            )
    reservation: Reservation | None = None
    try:
        reservation = await _reserve_playground_spend(
            request, user, role, op_store, router_exec, embedding_adapters
        )
        with _track_interactive_request(canonical) as tracker:
            yield InteractiveAdmission(tracker)
    finally:
        if reservation is not None:
            reservation.release()
        if limiter is not None:
            limiter.release(slot_key)


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
            reasoning_efforts=model_effort_domain(adapter for adapter, _ in route.adapters) or [],
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
    reasoning_effort: str | None = None
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
        # The chat route consults `model_router_registry`, so RouteWise can
        # serve it. Its final decision frame is enrichment-only (fallback +
        # failed_attempts, no provider/endpoint, see routewise/router.py
        # `_attach_decision_info`); the merged stream state keeps whatever an
        # earlier attempt announced, so that frame does not erase the badge.
        # A turn with no announced endpoint at all still has none to show.
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


def _is_done_frame(frame: Any) -> bool:
    """Whether *frame* is the SSE terminator of a chat stream."""
    if isinstance(frame, bytes):
        return frame.startswith(b"data: [DONE]")
    return isinstance(frame, str) and frame.startswith("data: [DONE]")


def _route_frame(stream: ChatStream, *, show_host: bool) -> str | None:
    """The frame carrying the redacted upstream identity, or None without one."""
    try:
        summary = _redact_routing(stream.session.served_route, show_host=show_host)
    except Exception:
        summary = None
    if not summary:
        return None
    return f"data: {json.dumps({_PLAYGROUND_ROUTE_KEY: summary})}\n\n"


def _with_route_frame(
    response: StreamingResponse, stream: ChatStream, *, show_host: bool
) -> StreamingResponse:
    """Append the routing badge to a chat stream, ahead of its ``[DONE]``.

    The badge reports the attempt that actually served the turn, so it is built
    once the stream has run, not when it starts. A response that never reaches
    ``[DONE]`` still gets one, after its last frame.
    """
    inner = response.body_iterator

    async def frames() -> AsyncIterator[Any]:
        sent = False
        async for frame in inner:
            if not sent and _is_done_frame(frame):
                badge = _route_frame(stream, show_host=show_host)
                if badge is not None:
                    yield badge
                sent = True
            yield frame
        if not sent:
            badge = _route_frame(stream, show_host=show_host)
            if badge is not None:
                yield badge

    response.body_iterator = frames()
    return response


#: The playground's caller check, shared by the routes and the body read below.
_require_internal = require_role("internal")
_chat_body = json_body_after(_require_internal, PlaygroundChatRequest)


def _public_chat_request(body: PlaygroundChatRequest) -> dict[str, Any]:
    """The public Chat Completions body this playground turn stands for."""
    system = body.system_prompt.strip()
    payload: dict[str, Any] = {
        "model": body.model,
        "messages": (
            [{"role": "system", "content": system}, *body.messages] if system else body.messages
        ),
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "stream": True,
    }
    if body.reasoning_effort is not None:
        payload["reasoning_effort"] = body.reasoning_effort
    return payload


# A pasted document can make a long prompt, so this route keeps the general
# body limit rather than the small one other /internal paths get
# (RequestBodyLimitMiddleware). Its body is therefore not declared, which
# FastAPI would read and decode before any dependency, the role check
# included: a dependency reads it once the caller is known.
@router.post("/chat", response_model=None, openapi_extra=json_body_schema(PlaygroundChatRequest))
async def playground_chat(
    request: Request,
    http_response: Response,
    body: PlaygroundChatRequest = Depends(_chat_body),
    caller: dict[str, Any] = Depends(_require_internal),
    _slot: InteractiveAdmission = Depends(_playground_slot),
    router_exec=Depends(get_router),
    model_router_registry=Depends(get_model_router_registry),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    runtime_settings=Depends(get_runtime_settings),
    log_store=Depends(get_log_store),
    completions_logger=Depends(get_completions_logger),
    pricing_lookup=Depends(get_pricing_lookup),
    cost_tracker=Depends(get_cost_tracker),
    _streams: list[ChatStream] = Depends(release_unstarted_chat_streams, scope="request"),
) -> Any:
    """Stream a chat completion through the public surface's own execution.

    Model access, pricing, routing, logging, billing and the admin provider pin
    are the public route's. This supplies the console session's identity and
    appends the playground's routing badge.
    """
    is_admin = _is_admin(caller)
    if body.provider and not is_admin:
        # A pin skips circuit admission and reaches routes no weighted draw
        # would choose, a staging route included.
        raise HTTPException(403, "Choosing the route a request takes requires the admin role.")
    caller = _authenticated_caller(caller)
    # The key pool reads it to keep upstream keys reserved for a higher tier
    # away from this caller, as on the API surfaces.
    publish_caller_role(caller)
    origin = client_origin(request)
    if body.provider:
        origin = origin.with_header("X-Route-Pin", body.provider)
    outcome = await execute_chat(
        _public_chat_request(body),
        ExecutionContext(
            user_ctx=read_only(caller),
            origin=origin,
            admission=admission_from(_slot),
            batch=None,
            timeout_probe=timeout_probe(request),
        ),
        ChatServices(
            router=router_exec,
            model_router_registry=model_router_registry,
            visibility=model_visibility_resolver,
            log_store=log_store,
            completions_logger=completions_logger,
            pricing_lookup=pricing_lookup,
            cost_tracker=cost_tracker,
            app_services=getattr(request.app.state, "services", None),
        ),
        ChatOptions.online(runtime_settings),
    )
    if isinstance(outcome, ChatStream):
        _streams.append(outcome)
    response = await render_chat_http(outcome, request, http_response)
    if isinstance(outcome, ChatStream) and isinstance(response, StreamingResponse):
        return _with_route_frame(response, outcome, show_host=is_admin)
    return response


_RESPONSES_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


@router.post("/responses", response_model=None)
async def playground_responses(
    request: Request,
    caller: dict[str, Any] = Depends(_require_internal),
    _slot: InteractiveAdmission = Depends(_playground_slot),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    model_router_registry=Depends(get_model_router_registry),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    completions_logger=Depends(get_completions_logger),
    pricing_lookup=Depends(get_pricing_lookup),
    cost_tracker=Depends(get_cost_tracker),
    response_store=Depends(get_response_store),
    _streams: list[ChatStream] = Depends(release_unstarted_chat_streams, scope="request"),
) -> JSONResponse | StreamingResponse:
    """Serve an OpenAI Responses request for the console playground.

    Runs the same execution as ``/v1/responses`` under the console session, so
    the playground exercises the real translation, routing, logging and billing
    with the caller's own identity instead of an API key.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON in request body") from exc
    caller = _authenticated_caller(caller)
    publish_caller_role(caller)
    result = await execute_responses(
        body,
        ExecutionContext(
            user_ctx=read_only(caller),
            origin=client_origin(request),
            admission=admission_from(_slot),
            batch=None,
            timeout_probe=timeout_probe(request),
        ),
        ResponsesServices(
            chat=ChatServices(
                router=router_exec,
                model_router_registry=model_router_registry,
                visibility=model_visibility_resolver,
                log_store=log_store,
                completions_logger=completions_logger,
                pricing_lookup=pricing_lookup,
                cost_tracker=cost_tracker,
                app_services=getattr(request.app.state, "services", None),
            ),
            response_store=response_store,
        ),
    )
    if isinstance(result, ResponsesStream):
        if result.chat_stream is not None:
            _streams.append(result.chat_stream)
        return StreamingResponse(
            result.events,
            media_type="text/event-stream",
            headers=_RESPONSES_SSE_HEADERS,
        )
    return JSONResponse(content=result.body)


@router.post("/messages", response_model=None)
async def playground_messages(
    request: Request,
    caller: dict[str, Any] = Depends(_require_internal),
    _slot: InteractiveAdmission = Depends(_playground_slot),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    op_store=Depends(get_operational_store),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    completions_logger=Depends(get_completions_logger),
    _streams: list[MessagesStream] = Depends(_unstarted_stream_release, scope="request"),
) -> JSONResponse | StreamingResponse:
    """Serve an Anthropic Messages request for the console playground.

    Same execution as ``/v1/messages``, on the console session rather than an
    API key, so the real translation, routing, logging and billing apply.
    """
    try:
        body: Any = await request.json()
    except Exception:
        body = MALFORMED_BODY
    caller = _authenticated_caller(caller)
    publish_caller_role(caller)
    outcome = await execute_messages(
        body,
        ExecutionContext(
            user_ctx=read_only(caller),
            origin=client_origin(request),
            admission=admission_from(_slot),
            batch=None,
            timeout_probe=timeout_probe(request),
        ),
        MessagesServices(
            router=router_exec,
            log_store=log_store,
            op_store=op_store,
            visibility=model_visibility_resolver,
            completions_logger=completions_logger,
            app_services=getattr(request.app.state, "services", None),
        ),
        MessagesConstraints.online(),
    )
    if isinstance(outcome, MessagesStream):
        _streams.append(outcome)
    return render_messages_http(outcome)
