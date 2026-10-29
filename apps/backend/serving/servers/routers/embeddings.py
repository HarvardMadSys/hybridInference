"""OpenAI-compatible embeddings endpoint."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError

from routing.endpoints import endpoint_id_for_config
from serving.adapters.key_pool import KeyPoolExhausted
from serving.batch.attempts import note_request_id, observe_billing
from serving.config.runtime_settings import get_runtime_settings
from serving.grant_auth import ledger_attribution
from serving.model_access import ModelAccessDenial, model_access_denial
from serving.observability.tracked_tasks import tracked_task
from serving.pricing import effective_pricing, require_embedding_pricing
from serving.schemas import EmbeddingRequest, EmbeddingResponse, ErrorResponse
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.model_availability import model_not_found_detail
from serving.servers.routers.served_identity import (
    served_identity_headers,
    should_include_served_identity_headers,
)
from serving.servers.surface_http import client_origin, timeout_probe
from serving.storage.utils import calculate_cost
from serving.surfaces.context import ExecutionContext, admission_from, read_only
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import derive_affinity_key
from serving.utils.session_identity import session_identity
from serving.utils.synthetic_probe import is_trusted_probe_headers
from serving.utils.token_utils import normalize_usage

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)
router = APIRouter()

# Retains in-flight cost-increment tasks so the event loop's garbage collector
# can't cancel a fire-and-forget increment before it commits.
_increment_tasks: set[asyncio.Task[Any]] = set()


def _schedule_cost_increment(
    op_store: Any,
    user_id: str | None,
    usage: dict[str, Any] | None,
    pricing: dict[str, str] | None,
) -> None:
    """Fire-and-forget the daily quota cost increment for a billed embedding.

    Mirrors the chat path's ``CostTracker.schedule_increment`` effect: the
    ``api_logs.cost_usd`` column alone is not read by ``verify_api_key`` for
    quota enforcement, so paid embeddings must also bump the operational
    counter. Uses the same ``calculate_cost`` helper as the log row, so the
    increment and the logged cost stay in lockstep. No-ops for unauthenticated
    callers, missing stores, or zero-cost (free) models.
    """
    if op_store is None or not user_id:
        return
    normalized_usage = normalize_usage(usage)
    cost = calculate_cost(normalized_usage if normalized_usage is not None else usage, pricing)
    if not cost or cost <= 0:
        return

    async def _increment() -> None:
        try:
            await op_store.increment_user_cost(user_id, cost)
        except Exception as exc:
            logger.warning(f"Failed to increment embedding cost counter for {user_id}: {exc}")
            raise  # let tracked_task record the failure

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    task = tracked_task(_increment(), name="embedding_cost_increment")
    _increment_tasks.add(task)
    task.add_done_callback(_increment_tasks.discard)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class EmbeddingServices:
    """What :func:`execute_embeddings` serves a request with."""

    #: The embedding adapters by the names a request may use (canonical and aliases).
    embedding_adapters: Mapping[str, Any]
    #: The router, read only for the access gates it records (``embedding_routes``).
    router: Any
    visibility: Any
    log_store: Any
    completions_logger: Any
    op_store: Any
    #: Read only for the ``log_synthetic_probes`` toggle; None reads as off.
    runtime_settings: Any


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
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    router_exec=Depends(get_router),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    log_store=Depends(get_log_store),
    completions_logger=Depends(get_completions_logger),
    op_store=Depends(get_operational_store),
    runtime_settings=Depends(get_runtime_settings),
    _concurrency_slot=Depends(enforce_user_concurrency),
) -> dict[str, Any]:
    """Create embeddings for the given input text(s).

    Routes to the appropriate embedding adapter based on the requested model.
    The request is logged to the shared ``api_logs`` store (tagged
    ``request_type=embedding``) so it surfaces in the dashboards alongside
    chat/completions traffic. Served by :func:`execute_embeddings`, which the
    batch worker calls too.
    """
    # The validated model drops anything the embeddings schema does not
    # declare, so the body is read back raw for the session declaration --
    # FastAPI has already parsed and cached it, so this costs nothing and
    # cannot fail on a request that got this far.
    try:
        raw_body: Any = await http_request.json()
    except Exception:  # pragma: no cover - validation would have rejected it
        raw_body = None
    response, identity_headers = await _execute_embeddings(
        request,
        raw_body,
        ExecutionContext(
            user_ctx=read_only(user_ctx),
            origin=client_origin(http_request, authorization=authorization),
            admission=admission_from(_concurrency_slot),
            batch=None,
            timeout_probe=timeout_probe(http_request),
        ),
        EmbeddingServices(
            embedding_adapters=embedding_adapters,
            router=router_exec,
            visibility=model_visibility_resolver,
            log_store=log_store,
            completions_logger=completions_logger,
            op_store=op_store,
            runtime_settings=runtime_settings,
        ),
    )
    http_response.headers.update(identity_headers)
    return response


async def execute_embeddings(
    request: EmbeddingRequest,
    raw_body: Any,
    ctx: ExecutionContext,
    services: EmbeddingServices,
) -> dict[str, Any]:
    """Execute an embedding request and return only its API payload.

    This is the shared entry point used by batch execution; HTTP metadata is
    attached by the route handler, outside the execution boundary.
    """
    response, _identity_headers = await _execute_embeddings(request, raw_body, ctx, services)
    return response


async def _execute_embeddings(
    request: EmbeddingRequest,
    raw_body: Any,
    ctx: ExecutionContext,
    services: EmbeddingServices,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Serve one embeddings request, online or as a batch item.

    Holds the request to the model's access gate, dispatches it to the model's
    own embedding adapter (a fallback chain falls back inside the adapter),
    logs it and charges it. Raises ``HTTPException`` with the status and
    message the route answers with. ``raw_body`` is the request as sent, read
    only for a session declared in it. Interactive admission is the route's:
    this surface has no auto model, so it never retargets the load count.
    """
    user_ctx = ctx.user_ctx
    embedding_adapters = services.embedding_adapters
    router_exec = services.router
    model_visibility_resolver = services.visibility
    log_store = services.log_store
    completions_logger = services.completions_logger
    op_store = services.op_store
    runtime_settings = services.runtime_settings
    model = request.model
    request_id = f"emb_{uuid.uuid4().hex}"
    # The log row's id, for a batch attempt running this request.
    note_request_id(request_id)
    start_time = time.time()
    is_authenticated = bool(user_ctx.get("authenticated"))
    # Resolved the same way as on the chat surfaces, so a client that labels its
    # session one way does not have to label it another way here.
    declared_session = session_identity(ctx.origin.headers, raw_body)

    # Synthetic health-probe traffic is suppressed from api_logs unless the
    # ``log_synthetic_probes`` toggle opts it in — mirrors the chat-completions
    # path so probes don't pollute the dashboards. Only a trusted caller's
    # marker counts (authenticated internal/admin, never a grant; see
    # serving/utils/synthetic_probe.py) — and billing is unconditional either
    # way. A setting read failure defaults to suppression.
    is_synthetic_probe = is_trusted_probe_headers(ctx.origin.headers, user_ctx)
    log_synthetic_probes = False
    if (
        is_synthetic_probe
        and runtime_settings is not None
        and hasattr(runtime_settings, "get_bool")
    ):
        try:
            log_synthetic_probes = await runtime_settings.get_bool("log_synthetic_probes")
        except Exception:
            log_synthetic_probes = False
    suppress_synthetic_logging = is_synthetic_probe and not log_synthetic_probes

    # Per-caller identity for multi-key rotation, published before any dispatch.
    # Pinned to the specific hyi-xxx key in use (not user_id — a user may hold
    # several), or the caller's IP bucket when unauthenticated. Without it every
    # caller on this surface shared one process-wide ``_anon`` binding, so nobody
    # kept a stable upstream key.
    ip_info = ctx.origin.ip
    auth_key_hash = user_ctx.get("auth_key_hash")
    req_ctx.update(
        {
            "auth_key_hash": auth_key_hash or "_anon",
            "affinity_key": derive_affinity_key(auth_key_hash, ip_info),
            # Trusted-probe verdict for downstream readers that never see
            # user_ctx: RequestLogMiddleware demotes probe request lines to
            # DEBUG from this, mirroring the chat-completions publish.
            "synthetic_probe": is_synthetic_probe,
        }
    )

    metadata: dict[str, Any] = {
        "request_type": "embedding",
        "user_agent": ctx.origin.user_agent,
        "referer": ctx.origin.referer,
        "ip": ip_info.client_ip,
        "peer_ip": ip_info.peer_ip,
        "ip_source": ip_info.source,
        "authorization": ctx.origin.authorization_header or is_authenticated,
        "authenticated": is_authenticated,
        "user_id": user_ctx.get("user_id"),
        # Agent-sandbox attribution (issue #1041); None for ordinary traffic.
        "agent_job_id": user_ctx.get("agent_job_id"),
    }
    metadata.update(ledger_attribution(user_ctx, started_at=start_time))
    # Batch attribution (serving/batch/dispatch.py); nothing for other traffic.
    metadata.update(ctx.batch_log_metadata())
    if is_synthetic_probe:
        metadata["synthetic_probe"] = True
    if declared_session is not None:
        metadata["session_id"] = declared_session.session_id
        metadata["session_id_source"] = declared_session.source

    def _schedule_log(
        *,
        provider: str,
        status_code: int,
        response: dict[str, Any] | None,
        usage: dict[str, Any] | None,
        pricing: dict[str, str] | None,
        error: str | None,
        endpoint_id: str | None = None,
    ) -> None:
        if log_store is None or completions_logger is None or suppress_synthetic_logging:
            return
        # ``LogStore.log_request`` recovers ``api_logs.served_endpoint_id`` from
        # ``metadata["endpoint_id"]``. This surface dispatches to an adapter
        # looked up by model id rather than through FixedRouter, so no
        # ``_routing`` blob ever supplies it and the column would otherwise fall
        # through to the bare provider label ("sglang") — indistinguishable
        # across the endpoints serving the same embedding model. Left unset on
        # the pre-dispatch 404, where no endpoint was ever selected.
        log_metadata = {**metadata, "endpoint_id": endpoint_id} if endpoint_id else metadata
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
                "metadata": log_metadata,
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
        req_ctx.mark_model_not_found()
        raise HTTPException(
            404, model_not_found_detail(model, user_ctx=user_ctx, noun="Embedding model")
        )

    # The same gate every inference surface applies (serving/model_access.py):
    # the model's published state and tier declaration from models.yaml.
    # Embedding models never enter the weighted route table where the chat
    # surface reads it, so the registry records their gate on the router
    # (``embedding_routes``) for this surface to enforce; without it the
    # declaration was dead config and any authenticated caller could use the
    # model. Denials answer exactly like an unknown model.
    #
    # Fail-closed: the gate is mandatory, not optional. An adapter that is
    # registered while its access-gate record is missing is a registry
    # inconsistency (dynamic registration, a partial rebuild, a future
    # refactor) — the one state this surface must never serve through, since
    # dispatching it would skip tier/visibility/grant enforcement entirely.
    # It is refused exactly like an unknown model, with an internal
    # ``embedding_access_policy_missing`` error naming the gap. The error
    # carries only the model id: the caller sees the standard 404 either way.
    embedding_routes = getattr(router_exec, "embedding_routes", None)
    route = embedding_routes.get(model) if embedding_routes else None
    if route is None:
        logger.error(
            "embedding_access_policy_missing: embedding adapter registered without "
            "an access-gate record; refusing the request",
            extra={"model": model},
        )
        _schedule_log(
            provider="router",
            status_code=404,
            response=None,
            usage=None,
            pricing=None,
            error=f"Embedding model '{model}' not found",
        )
        req_ctx.mark_model_not_found()
        raise HTTPException(
            404, model_not_found_detail(model, user_ctx=user_ctx, noun="Embedding model")
        )
    denial = await model_access_denial(
        route, model, user_ctx, visibility_resolver=model_visibility_resolver
    )
    if denial is not None:
        if denial is ModelAccessDenial.ROLE:
            logger.info(
                "Insufficient role for embedding model",
                extra={
                    "model": model,
                    "user_id": user_ctx.get("user_id"),
                    "role": user_ctx.get("role", "free"),
                },
            )
        # An unpublished route is refused without a request log row, as
        # on the chat surface.
        if denial is not ModelAccessDenial.UNPUBLISHED:
            _schedule_log(
                provider="router",
                status_code=404,
                response=None,
                usage=None,
                pricing=None,
                error=f"Embedding model '{model}' not found",
            )
        req_ctx.mark_model_not_found()
        raise HTTPException(
            404,
            model_not_found_detail(model, user_ctx=user_ctx, noun="Embedding model"),
        )

    adapter = embedding_adapters[model]
    require_embedding_pricing(adapter)
    adapter_config = getattr(adapter, "config", None)
    provider = getattr(adapter_config, "provider", None) or "unknown"
    endpoint_id = endpoint_id_for_config(adapter_config)
    pricing = effective_pricing(adapter_config)

    params: dict[str, Any] = {}
    if request.encoding_format is not None:
        params["encoding_format"] = request.encoding_format
    if request.dimensions is not None:
        params["dimensions"] = request.dimensions

    try:
        response = await adapter.embeddings(request.input, **params)
        # Attribute the log row and cost increment to whichever backend actually
        # served. For a fallback chain (FallbackEmbeddingAdapter) this may be the
        # staging canary rather than the primary when the primary is down; plain
        # single-route adapters expose no ``serving_config`` so this is a no-op.
        serving_cfg = getattr(adapter, "serving_config", None)
        if serving_cfg is not None:
            provider = getattr(serving_cfg, "provider", None) or provider
            endpoint_id = endpoint_id_for_config(serving_cfg) or endpoint_id
            pricing = effective_pricing(serving_cfg)
            adapter_config = serving_cfg
        identity_headers: dict[str, str] = {}
        if should_include_served_identity_headers(ctx.origin.headers, user_ctx):
            identity_headers = served_identity_headers(
                requested_model=model,
                served_model=getattr(adapter_config, "id", None) or model,
                endpoint_id=endpoint_id,
                provider=provider,
                endpoint_publishable=bool(
                    getattr(adapter_config, "endpoint_identity_publishable", False)
                ),
            )
        # Validate against the response schema *before* recording any success
        # side effects. ``response_model=EmbeddingResponse`` is only enforced
        # after the handler returns, so a malformed (but non-raising) upstream
        # response would otherwise be logged as a billable 200 and increment
        # the quota even though the client receives a 500.
        try:
            validated = EmbeddingResponse.model_validate(response)
        except ValidationError as exc:
            logger.error(f"Malformed embedding response for model={model}: {exc}")
            _schedule_log(
                provider=provider,
                endpoint_id=endpoint_id,
                status_code=500,
                response=None,
                usage=None,
                pricing=None,
                error=f"invalid embedding response: {exc}",
            )
            raise HTTPException(500, "Embedding service error") from exc

        # Use the *validated* usage (type-coerced ints) rather than the raw
        # upstream dict: an OpenAI-compatible server may report coercible
        # values like ``"prompt_tokens": "5"`` that Pydantic accepts but that
        # would fail the integer-column bind in the background log insert while
        # the quota increment still succeeds — leaving a billed request missing
        # from the dashboards.
        usage = validated.usage.model_dump()
        _schedule_log(
            provider=provider,
            endpoint_id=endpoint_id,
            status_code=200,
            response=_response_summary(response, model),
            usage=usage,
            pricing=pricing,
            error=None,
        )
        # Bump the daily quota cost counter for paid embedding models. This is
        # intentionally NOT gated on ``is_synthetic_probe``: the ``x-probe``
        # header is caller-controlled, so exempting it from billing would let
        # any authenticated client bypass quota by setting it. The probe flag
        # affects log suppression only — never cost/quota.
        _schedule_cost_increment(op_store, user_ctx.get("user_id"), usage, pricing)
        # What this request is charged, for a batch attempt watching it; the
        # same inputs as the increment above. Unpriced reads as unknown.
        observe_billing(
            cost_usd=calculate_cost(normalize_usage(usage) or usage, pricing) if pricing else None,
            usage=usage,
            request_id=request_id,
        )
        return response, identity_headers
    except HTTPException:
        raise
    except aiohttp.ClientResponseError as exc:
        logger.error(f"Embedding request failed for model={model}: {exc.status} {exc.message}")
        # Publish the upstream attribution, not just log it as a value: the DB row
        # is handed ``provider`` below, but RequestLogMiddleware and the
        # in-process alert rules can only read it from req_ctx. This endpoint
        # relays the upstream status verbatim, so without attribution an upstream
        # 401 — the gateway's own key refused by the embedding backend — arrives
        # at both consumers as a bare 401 and is filed as routine client auth
        # churn: logged at DEBUG and excluded from the failed-request rate. The
        # local embedding proxy shares its API key with the chat proxies, so this
        # is the same credential outage the chat path now alerts on.
        req_ctx.publish_upstream_provider(provider)
        _schedule_log(
            provider=provider,
            endpoint_id=endpoint_id,
            status_code=exc.status,
            response=None,
            usage=None,
            pricing=None,
            error=f"{exc.status} {exc.message}",
        )
        raise HTTPException(exc.status, "Embedding service error") from exc
    except KeyPoolExhausted as exc:
        # Every key the adapter could use is muted or reserved above this
        # caller's tier — raised before anything was sent, a local
        # precondition rather than a broken embedding backend. Answer 429 so
        # clients back off for the mute window and the burst stays out of the
        # failed-request rule, instead of filing it as a 500 outage
        # (issue #1440; the anthropic surface maps the same condition to 429).
        logger.warning(f"Embedding request rate-limited for model={model}: key pool exhausted")
        req_ctx.publish_upstream_provider(provider)
        _schedule_log(
            provider=provider,
            endpoint_id=endpoint_id,
            status_code=429,
            response=None,
            usage=None,
            pricing=None,
            error=str(exc),
        )
        raise HTTPException(429, "Embedding service error") from exc
    except Exception as exc:
        logger.error(f"Embedding request failed for model={model}: {exc}")
        # Reached only after dispatch, so an upstream is always attributable here
        # too. The resulting 500 already counts as a failure, but the label is
        # what names the offending backend in the alert's provider breakdown.
        req_ctx.publish_upstream_provider(provider)
        _schedule_log(
            provider=provider,
            endpoint_id=endpoint_id,
            status_code=500,
            response=None,
            usage=None,
            pricing=None,
            error=str(exc),
        )
        raise HTTPException(500, "Embedding service error") from exc
