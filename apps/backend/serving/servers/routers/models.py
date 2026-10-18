"""OpenRouter-compatible models listing routes.

This module exposes endpoints that return a consolidated list of models in an
OpenRouter/OpenAI-compatible schema:

- `/models`
- `/openrouter/models`
- `/v1/models`

The router aggregates multiple backend adapters per logical model and reports
conservative capabilities (e.g., minimum context length, intersection of
sampling parameters). The response is deterministic across concurrent requests;
for example, the `created` field is frozen at import time.

Anthropic-family clients that send an ``anthropic-version`` header (or a
``User-Agent`` starting with ``anthropic-`` / ``claude-cli`` / ``claude-sdk``)
receive the Anthropic list-models response shape from ``/v1/models``.
The dedicated ``/anthropic/v1/models`` route always returns that shape.

While auto mode is on, the auto models (``auto``, ``auto-<tier>``) are listed
after the models, each to a caller who may use any of its tier's models.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from routing.model_selection import AUTO_MODEL_ID, AUTO_MODEL_PREFIX
from serving.auto_mode import AutoMode, auto_mode_enabled, candidate_info
from serving.model_access import model_access_denial
from serving.pricing import PricingUnavailableError, effective_pricing
from serving.schemas import ModelItem, ModelList
from serving.servers.auth import optional_verify_api_key
from serving.servers.deps import (
    get_auto_mode,
    get_embedding_adapters,
    get_model_visibility_resolver,
    get_router,
)

router = APIRouter()


def _catalog_pricing(config: Any) -> dict[str, str]:
    try:
        return effective_pricing(config) or {}
    except PricingUnavailableError:
        # A missing official quote is unknown, never a free rate. Keep the
        # model discoverable while inference fails before creating usage.
        return {}


# Fixed creation timestamp captured at import time to ensure
# deterministic responses across concurrent requests.
CREATED_TS = int(time.time())

# ISO-8601 timestamp used for the Anthropic ``created_at`` field.
_CREATED_AT_ISO = "1970-01-01T00:00:00Z"


def _is_anthropic_client(request: Request) -> bool:
    """Return True when the request looks like it comes from an Anthropic-family client.

    Detection rules (checked in order):
    - ``anthropic-version`` header is present, or
    - ``User-Agent`` starts with ``anthropic-``, ``claude-cli``, or ``claude-sdk``.
    """
    if request.headers.get("anthropic-version"):
        return True
    ua = (request.headers.get("user-agent") or "").lower()
    return ua.startswith(("anthropic-", "claude-cli", "claude-sdk"))


async def _listed_for(
    route: Any,
    model_id: str,
    user_ctx: dict[str, Any] | None,
    model_visibility_resolver: Any | None,
) -> bool:
    """Return whether a model listing shows ``route`` to ``user_ctx``.

    The inference surfaces' own access check, except that a listing does not
    apply an inference grant's model list.
    """
    denial = await model_access_denial(
        route,
        model_id,
        user_ctx,
        visibility_resolver=model_visibility_resolver,
        grant_scope=False,
    )
    return denial is None


async def _auto_models_for(
    auto_mode: AutoMode | None,
    router_exec: Any,
    model_visibility_resolver: Any | None,
    user_ctx: dict[str, Any] | None,
) -> list[tuple[str, str, list[Any]]]:
    """Return ``(id, tier, configs)`` for each auto model listed to ``user_ctx``.

    An auto model is listed while auto mode is on and the caller may use at
    least one of its tier's models, by the listings' own rule (no grant scope).
    ``configs`` are the adapter configs of those models.
    """
    if auto_mode is None or not await auto_mode_enabled():
        return []
    profiles = auto_mode.config.profiles
    names = [AUTO_MODEL_ID, *(f"{AUTO_MODEL_PREFIX}{name}" for name in profiles)]
    listed: list[tuple[str, str, list[Any]]] = []
    for name in names:
        reachable = await auto_mode.reachable(
            name, router_exec, user_ctx, model_visibility_resolver, grant_scope=False
        )
        if not reachable:
            continue
        configs = [
            adapter.config
            for model_id in reachable
            for adapter, _ in router_exec.routes[model_id].adapters
        ]
        listed.append((name, auto_mode.config.resolve(name).name, configs))
    return listed


def _auto_model_item(model_id: str, tier: str, configs: list[Any]) -> ModelItem:
    """Describe an auto model by what its tier's models offer between them.

    What selection routes on is offered when any model has it: a request is
    sent to a model with the window, the input kind or the tools it needs. The
    rest is offered only when every model has it, since any of them may serve.
    """
    by_model: dict[str, list[Any]] = {}
    for cfg in configs:
        by_model.setdefault(cfg.id, []).append(cfg)
    infos = [candidate_info(model_configs) for model_configs in by_model.values()]
    quantizations = {cfg.quantization for cfg in configs}
    features: list[str] = []
    if any(info.supports_tools for info in infos):
        features.append("tools")
    if all(cfg.supports_structured_output for cfg in configs):
        features.extend(["json_mode", "structured_outputs"])
    return ModelItem(
        id=model_id,
        name=f"Auto ({tier})",
        created=CREATED_TS,
        owned_by="auto",
        input_modalities=sorted(set().union(*(info.input_modalities for info in infos))),
        output_modalities=["text"],
        quantization=quantizations.pop() if len(quantizations) == 1 else "mixed",
        context_length=max(info.context_length for info in infos),
        max_output_length=max(info.max_output_length for info in infos),
        pricing={},
        supported_sampling_parameters=sorted(
            set.intersection(*(set(cfg.supported_params) for cfg in configs))
        ),
        supported_features=features,
        on_demand=any(cfg.on_demand for cfg in configs),
    )


async def _format_anthropic_model_list(
    router_exec: Any,
    model_visibility_resolver: Any | None = None,
    user_ctx: dict[str, Any] | None = None,
    auto_mode: AutoMode | None = None,
) -> dict:
    """Build the Anthropic GET /v1/models response shape from the model registry.

    Returns a dict with ``data``, ``has_more``, ``first_id``, and ``last_id``
    keys matching the Anthropic list-models API response.
    """
    data: list[dict] = []
    emitted: set[str] = set()
    for model_id, route in tuple(router_exec.routes.items()):
        configs = [adapter.config for adapter, _ in route.adapters]
        if not configs:
            continue
        if not await _listed_for(route, model_id, user_ctx, model_visibility_resolver):
            continue
        primary = configs[0]
        canonical_id = primary.id
        if canonical_id in emitted:
            continue
        emitted.add(canonical_id)
        data.append(
            {
                "type": "model",
                "id": canonical_id,
                "display_name": primary.name or canonical_id,
                "created_at": _CREATED_AT_ISO,
            }
        )
    for model_id, tier, _configs in await _auto_models_for(
        auto_mode, router_exec, model_visibility_resolver, user_ctx
    ):
        data.append(
            {
                "type": "model",
                "id": model_id,
                "display_name": f"Auto ({tier})",
                "created_at": _CREATED_AT_ISO,
            }
        )
    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }


@router.get("/models")
@router.get("/openrouter/models")
@router.get("/v1/models")
async def list_models(
    request: Request,
    router_exec=Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    user_ctx: dict | None = Depends(optional_verify_api_key),
    auto_mode: AutoMode | None = Depends(get_auto_mode),
):
    """List available models with metadata similar to OpenRouter schema.

    When multiple adapters are registered for a model, the server advertises
    conservative limits (minimum across adapters) to ensure compatibility
    regardless of the routed backend.

    Anthropic-family clients calling ``/v1/models`` receive the Anthropic list
    response shape instead.  The ``/models`` and ``/openrouter/models`` paths
    always return the OpenAI/OpenRouter shape.
    """
    if request.url.path == "/v1/models" and _is_anthropic_client(request):
        return JSONResponse(
            await _format_anthropic_model_list(
                router_exec, model_visibility_resolver, user_ctx, auto_mode
            )
        )
    return await _build_model_list_async(
        router_exec,
        embedding_adapters,
        model_visibility_resolver,
        user_ctx=user_ctx,
        auto_mode=auto_mode,
    )


@router.get("/anthropic/v1/models")
async def list_models_anthropic(
    router_exec=Depends(get_router),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    user_ctx: dict | None = Depends(optional_verify_api_key),
    auto_mode: AutoMode | None = Depends(get_auto_mode),
):
    """Return the Anthropic-format model list (always).

    Mirrors the Anthropic GET /v1/models response shape unconditionally,
    regardless of request headers or User-Agent.
    """
    return JSONResponse(
        await _format_anthropic_model_list(
            router_exec, model_visibility_resolver, user_ctx, auto_mode
        )
    )


async def _build_model_list_async(
    router_exec: Any,
    embedding_adapters: dict[str, Any],
    model_visibility_resolver: Any | None = None,
    user_ctx: dict[str, Any] | None = None,
    auto_mode: AutoMode | None = None,
) -> ModelList:
    """Build the model catalog visible to ``user_ctx``, with runtime overrides if any.

    The auto models are listed only when ``auto_mode`` is passed: ``/v1/models``
    passes it, the console's own list does not.
    """
    models: list[ModelItem] = []
    emitted_ids: set[str] = set()

    for model_id, route in tuple(router_exec.routes.items()):
        configs = [adapter.config for adapter, _ in route.adapters]
        if not configs:
            continue
        if not await _listed_for(route, model_id, user_ctx, model_visibility_resolver):
            continue

        # Conservative limits across all adapters for this model
        context_length = min(cfg.context_length for cfg in configs)
        max_output_length = min(cfg.max_output_length for cfg in configs)

        # Intersection of supported sampling params across adapters
        supported_params_sets = [set(cfg.supported_params) for cfg in configs]
        if supported_params_sets:
            supported_sampling_parameters = sorted(set.intersection(*supported_params_sets))
        else:
            supported_sampling_parameters = []

        # Supported features per OpenRouter provider doc
        supported_features: list[str] = []
        if any(cfg.supports_tools for cfg in configs):
            supported_features.append("tools")
        if any(cfg.supports_structured_output for cfg in configs):
            supported_features.append("json_mode")
            supported_features.append("structured_outputs")

        # Use the first config for display name/provider/pricing as canonical
        primary_cfg = configs[0]
        canonical_id = primary_cfg.id
        if canonical_id in emitted_ids:
            # Skip aliases; only emit one entry per canonical model id
            continue
        emitted_ids.add(canonical_id)

        model_entry = ModelItem(
            id=canonical_id,
            name=primary_cfg.name,
            created=CREATED_TS,
            owned_by=primary_cfg.provider,
            input_modalities=primary_cfg.input_modalities,
            output_modalities=primary_cfg.output_modalities,
            quantization=primary_cfg.quantization,
            context_length=context_length,
            max_output_length=max_output_length,
            pricing=_catalog_pricing(primary_cfg),
            supported_sampling_parameters=supported_sampling_parameters,
            supported_features=supported_features,
            # Any adapter marking the model on-demand makes the whole model
            # on-demand: a probe that hits the lazy route still pins its GPU.
            on_demand=any(cfg.on_demand for cfg in configs),
        )
        # Optional OpenRouter-specific metadata
        if model_id != canonical_id:
            model_entry.openrouter = {"slug": model_id}
        models.append(model_entry)

    for auto_id, tier, configs in await _auto_models_for(
        auto_mode, router_exec, model_visibility_resolver, user_ctx
    ):
        models.append(_auto_model_item(auto_id, tier, configs))

    # Append embedding models from the embedding_adapters dict
    emb_seen: set[str] = set()
    for _model_id, adapter in embedding_adapters.items():
        cfg = getattr(adapter, "config", None)
        if not cfg:
            continue
        canonical_id = cfg.id
        if canonical_id in emb_seen or canonical_id in emitted_ids:
            continue
        emb_seen.add(canonical_id)
        models.append(
            ModelItem(
                id=canonical_id,
                name=cfg.name,
                created=CREATED_TS,
                owned_by=cfg.provider,
                input_modalities=cfg.input_modalities,
                output_modalities=cfg.output_modalities,
                quantization=cfg.quantization,
                context_length=cfg.context_length,
                max_output_length=cfg.max_output_length,
                pricing=_catalog_pricing(cfg),
                # The catalog lists embedding models beside chat models with
                # the same modality defaults; this is the one signal a client
                # has that the model answers /v1/embeddings, not chat.
                supported_features=["embeddings"],
                on_demand=cfg.on_demand,
            )
        )

    return ModelList(data=models)
