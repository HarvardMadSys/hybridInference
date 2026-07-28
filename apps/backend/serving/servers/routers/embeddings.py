"""OpenAI-compatible embeddings endpoint."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import aiohttp
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError

from serving.config.runtime_settings import get_runtime_settings
from serving.observability.tracked_tasks import tracked_task
from serving.schemas import EmbeddingRequest, EmbeddingResponse, ErrorResponse
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_operational_store,
)
from serving.storage.utils import calculate_cost
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip
from serving.utils.token_utils import normalize_usage

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
    cost = calculate_cost(normalize_usage(usage) or usage, pricing)
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
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
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
    chat/completions traffic.
    """
    model = request.model
    request_id = f"emb_{uuid.uuid4().hex}"
    start_time = time.time()
    is_authenticated = bool(user_ctx.get("authenticated"))
    session_id = http_request.headers.get("X-Session-ID")

    # Synthetic health-probe traffic is suppressed from api_logs unless the
    # ``log_synthetic_probes`` toggle opts it in — mirrors the chat-completions
    # path so probes don't pollute the dashboards or get billed. A setting read
    # failure defaults to suppression.
    is_synthetic_probe = http_request.headers.get("x-probe", "").lower() == "synthetic"
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

    metadata: dict[str, Any] = {
        "request_type": "embedding",
        "user_agent": http_request.headers.get("user-agent"),
        "referer": http_request.headers.get("referer"),
        "ip": get_client_ip(http_request),
        "authorization": bool(authorization) or is_authenticated,
        "authenticated": is_authenticated,
        "user_id": user_ctx.get("user_id"),
        # Agent-sandbox attribution (issue #1041); None for ordinary traffic.
        "agent_job_id": user_ctx.get("agent_job_id"),
    }
    if is_synthetic_probe:
        metadata["synthetic_probe"] = True
    if session_id:
        metadata["session_id"] = session_id

    def _schedule_log(
        *,
        provider: str,
        status_code: int,
        response: dict[str, Any] | None,
        usage: dict[str, Any] | None,
        pricing: dict[str, str] | None,
        error: str | None,
    ) -> None:
        if log_store is None or completions_logger is None or suppress_synthetic_logging:
            return
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
                "metadata": metadata,
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
        raise HTTPException(404, f"Embedding model '{model}' not found")

    adapter = embedding_adapters[model]
    provider = getattr(getattr(adapter, "config", None), "provider", None) or "unknown"
    pricing = getattr(getattr(adapter, "config", None), "pricing", None)

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
            pricing = getattr(serving_cfg, "pricing", None)
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
        return response
    except HTTPException:
        raise
    except aiohttp.ClientResponseError as exc:
        logger.error(f"Embedding request failed for model={model}: {exc.status} {exc.message}")
        _schedule_log(
            provider=provider,
            status_code=exc.status,
            response=None,
            usage=None,
            pricing=None,
            error=f"{exc.status} {exc.message}",
        )
        raise HTTPException(exc.status, "Embedding service error") from exc
    except Exception as exc:
        logger.error(f"Embedding request failed for model={model}: {exc}")
        _schedule_log(
            provider=provider,
            status_code=500,
            response=None,
            usage=None,
            pricing=None,
            error=str(exc),
        )
        raise HTTPException(500, "Embedding service error") from exc
