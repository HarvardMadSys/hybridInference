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
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from serving.config.settings import has_role
from serving.model_access import is_model_disabled_for_user
from serving.schemas import ModelItem, ModelList
from serving.servers.auth import optional_verify_api_key
from serving.servers.deps import get_embedding_adapters, get_model_visibility_resolver, get_router

router = APIRouter()

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


async def _format_anthropic_model_list(
    router_exec: Any,
    user_role: str,
    model_visibility_resolver: Any | None = None,
    user_ctx: dict[str, Any] | None = None,
) -> dict:
    """Build the Anthropic GET /v1/models response shape from the model registry.

    Returns a dict with ``data``, ``has_more``, ``first_id``, and ``last_id``
    keys matching the Anthropic list-models API response.
    """
    data: list[dict] = []
    emitted: set[str] = set()
    for _model_id, route in router_exec.routes.items():
        configs = [adapter.config for adapter, _ in route.adapters]
        if not configs:
            continue
        canonical_id = configs[0].id
        required = route.required_role or ("admin" if route.admin_only else "free")
        if model_visibility_resolver is not None:
            required = await model_visibility_resolver.get_effective_required_role(
                canonical_id, required
            )
        if not has_role(user_role, required):
            continue
        if is_model_disabled_for_user(canonical_id, user_ctx):
            continue
        primary = configs[0]
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
):
    """List available models with metadata similar to OpenRouter schema.

    When multiple adapters are registered for a model, the server advertises
    conservative limits (minimum across adapters) to ensure compatibility
    regardless of the routed backend.

    Anthropic-family clients calling ``/v1/models`` receive the Anthropic list
    response shape instead.  The ``/models`` and ``/openrouter/models`` paths
    always return the OpenAI/OpenRouter shape.
    """
    raw_role = (user_ctx or {}).get("role", "free")
    user_role = raw_role
    if request.url.path == "/v1/models" and _is_anthropic_client(request):
        return JSONResponse(
            await _format_anthropic_model_list(
                router_exec, user_role, model_visibility_resolver, user_ctx
            )
        )
    return await _build_model_list_async(
        router_exec,
        embedding_adapters,
        user_role,
        model_visibility_resolver,
        user_ctx=user_ctx,
    )


@router.get("/anthropic/v1/models")
async def list_models_anthropic(
    router_exec=Depends(get_router),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    user_ctx: dict | None = Depends(optional_verify_api_key),
):
    """Return the Anthropic-format model list (always).

    Mirrors the Anthropic GET /v1/models response shape unconditionally,
    regardless of request headers or User-Agent.
    """
    raw_role = (user_ctx or {}).get("role", "free")
    user_role = raw_role
    return JSONResponse(
        await _format_anthropic_model_list(
            router_exec, user_role, model_visibility_resolver, user_ctx
        )
    )


def build_model_list(
    router_exec: Any,
    embedding_adapters: dict[str, Any],
    user_role: str,
    user_ctx: dict[str, Any] | None = None,
) -> ModelList:
    """Build the model catalog visible to the given user role."""
    models: list[ModelItem] = []
    emitted_ids: set[str] = set()

    for model_id, route in router_exec.routes.items():
        required = route.required_role or ("admin" if route.admin_only else "free")
        if not has_role(user_role, required):
            continue
        configs = [adapter.config for adapter, _ in route.adapters]
        if not configs:
            continue
        canonical_id = configs[0].id
        if is_model_disabled_for_user(canonical_id, user_ctx):
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
            pricing=primary_cfg.pricing,
            supported_sampling_parameters=supported_sampling_parameters,
            supported_features=supported_features,
        )
        # Optional OpenRouter-specific metadata
        if model_id != canonical_id:
            model_entry.openrouter = {"slug": model_id}
        models.append(model_entry)

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
                pricing=cfg.pricing,
            )
        )

    return ModelList(data=models)


async def _build_model_list_async(
    router_exec: Any,
    embedding_adapters: dict[str, Any],
    user_role: str,
    model_visibility_resolver: Any | None = None,
    user_ctx: dict[str, Any] | None = None,
) -> ModelList:
    """Build the model catalog visible to the given user role with optional runtime overrides."""
    if model_visibility_resolver is None:
        return build_model_list(router_exec, embedding_adapters, user_role, user_ctx)

    models: list[ModelItem] = []
    emitted_ids: set[str] = set()

    for model_id, route in router_exec.routes.items():
        configs = [adapter.config for adapter, _ in route.adapters]
        if not configs:
            continue
        primary_cfg = configs[0]
        canonical_id = primary_cfg.id
        required = route.required_role or ("admin" if route.admin_only else "free")
        required = await model_visibility_resolver.get_effective_required_role(
            canonical_id, required
        )
        if not has_role(user_role, required):
            continue
        if is_model_disabled_for_user(canonical_id, user_ctx):
            continue

        context_length = min(cfg.context_length for cfg in configs)
        max_output_length = min(cfg.max_output_length for cfg in configs)

        supported_params_sets = [set(cfg.supported_params) for cfg in configs]
        if supported_params_sets:
            supported_sampling_parameters = sorted(set.intersection(*supported_params_sets))
        else:
            supported_sampling_parameters = []

        supported_features: list[str] = []
        if any(cfg.supports_tools for cfg in configs):
            supported_features.append("tools")
        if any(cfg.supports_structured_output for cfg in configs):
            supported_features.append("json_mode")
            supported_features.append("structured_outputs")

        if canonical_id in emitted_ids:
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
            pricing=primary_cfg.pricing,
            supported_sampling_parameters=supported_sampling_parameters,
            supported_features=supported_features,
        )
        if model_id != canonical_id:
            model_entry.openrouter = {"slug": model_id}
        models.append(model_entry)

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
                pricing=cfg.pricing,
            )
        )

    return ModelList(data=models)
