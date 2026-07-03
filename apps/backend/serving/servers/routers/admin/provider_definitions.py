"""Admin endpoints for upstream provider registry definitions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from fastapi import APIRouter, Depends, HTTPException

from serving.adapters import dynamic_keys, provider_registry
from serving.schemas_admin import (
    CreateProviderDefinitionRequest,
    DeleteProviderDefinitionResponse,
    ListProviderDefinitionsResponse,
    ProbeProviderDefinitionRequest,
    ProbeProviderDefinitionResponse,
    ProviderDefinitionItem,
    UpdateProviderDefinitionRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, get_services, verify_admin_access
from serving.servers.registry import parse_openrouter_kind
from serving.servers.routers.admin.provider_routes import (
    PROVIDER_TARGETS,
    SELECTABLE_PROVIDER_TARGETS,
    _validate_base_url,
)

router = APIRouter(prefix="/admin")

PROVIDER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SELF_SERVE_ADAPTER_KINDS = {"openai_compat"}
PROBE_TIMEOUT_SEC = 30.0

DISPLAY_NAMES = {
    "deepseek": "DeepSeek",
    "kimi": "Kimi",
    "minimax": "MiniMax",
    "ollama": "Ollama",
    "openrouter": "OpenRouter",
    "sglang": "SGLang",
    "staging": "staging",
    "vllm": "vLLM",
    "zai": "ZAI",
}

ENV_TEMPLATE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$")
PROVIDER_DEFAULT_BASE_URLS = {
    "chutes": "https://llm.chutes.ai",
    "deepseek": "https://api.deepseek.com",
    "featherless": "https://api.featherless.ai",
    "kimi": "https://api.kimi.com/coding/v1",
    "minimax": "https://api.minimax.io/v1",
    "ollama": "https://ollama.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "sglang": "http://host.docker.internal:8001/v1",
    "staging": "https://staging.freeinference.org/v1",
    "vllm": "http://host.docker.internal:8002/v1",
    "zai": "https://api.z.ai/api/coding/paas/v4/",
}


@dataclass(frozen=True)
class ConfigProviderSpec:
    """Provider metadata declared by config/models.yaml, independent of runtime load."""

    provider: str
    adapter_kind: str
    default_base_url: str
    model_ids: frozenset[str]


def _validate_provider_slug(provider: str) -> str:
    slug = provider.strip()
    if not PROVIDER_SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=422,
            detail="provider must use lowercase letters, numbers, dashes, or underscores",
        )
    return slug


def _display_name(provider: str) -> str:
    if provider in PROVIDER_TARGETS:
        return PROVIDER_TARGETS[provider].label
    return DISPLAY_NAMES.get(provider, provider.replace("_", " ").replace("-", " ").title())


def _models_config_path() -> Path:
    configured = os.getenv("MODELS_CONFIG")
    if configured:
        return Path(configured)

    cwd_path = Path("config/models.yaml")
    if cwd_path.exists():
        return cwd_path

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config/models.yaml"
        if candidate.exists():
            return candidate
    return cwd_path


def _expand_config_string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    match = ENV_TEMPLATE_RE.fullmatch(raw)
    if match is None:
        return raw
    var_name, fallback = match.groups()
    return (os.getenv(var_name) or fallback or "").strip()


def _provider_spec_from_route_kind(
    kind: str,
    model_provider: str | None,
) -> tuple[str, str]:
    base_kind, _pinned_provider = parse_openrouter_kind(kind)
    if base_kind == "openrouter":
        return ("openrouter", "openrouter")
    if base_kind == "openai_compat" and model_provider:
        return (dynamic_keys.normalize_key_provider(model_provider), "openai_compat")

    provider = dynamic_keys.normalize_key_provider(base_kind)
    return (provider, provider)


def _base_url_with_provider_default(provider: str, base_url: str) -> str:
    return base_url or PROVIDER_DEFAULT_BASE_URLS.get(provider, "")


def _is_config_managed_provider(provider: str, config_specs: dict[str, ConfigProviderSpec]) -> bool:
    """Return True for built-in providers sourced from code or config/models.yaml.

    Deliberately excludes ``dynamic_keys.get_known_providers()``: custom
    providers register themselves there, so consulting it would misclassify a
    custom provider as built-in and make it un-editable and un-deletable.
    Built-in providers are read-only in this registry — they are managed via
    config/models.yaml, the Routing tab, and the Keys tab.
    """
    return (
        provider in PROVIDER_TARGETS
        or provider in SELECTABLE_PROVIDER_TARGETS
        or provider in config_specs
    )


def _merge_config_provider_spec(
    specs: dict[str, ConfigProviderSpec],
    *,
    provider: str,
    adapter_kind: str,
    default_base_url: str,
    model_id: str | None,
) -> None:
    if not provider:
        return
    default_base_url = _base_url_with_provider_default(provider, default_base_url)
    current = specs.get(provider)
    model_ids = frozenset([model_id]) if model_id else frozenset()
    if current is None:
        specs[provider] = ConfigProviderSpec(
            provider=provider,
            adapter_kind=adapter_kind,
            default_base_url=default_base_url,
            model_ids=model_ids,
        )
        return
    merged_adapter_kind = current.adapter_kind
    if current.adapter_kind == provider and adapter_kind != provider:
        merged_adapter_kind = adapter_kind
    merged_model_ids = current.model_ids | model_ids
    if (
        (not current.default_base_url and default_base_url)
        or (merged_adapter_kind != current.adapter_kind)
        or (merged_model_ids != current.model_ids)
    ):
        specs[provider] = ConfigProviderSpec(
            provider=provider,
            adapter_kind=merged_adapter_kind,
            default_base_url=current.default_base_url or default_base_url,
            model_ids=merged_model_ids,
        )


def _configured_provider_specs() -> dict[str, ConfigProviderSpec]:
    path = _models_config_path()
    if not path.exists():
        return {}

    data = yaml.safe_load(path.read_text()) or {}
    models = data.get("models") if isinstance(data, dict) else []
    if not isinstance(models, list):
        return {}

    specs: dict[str, ConfigProviderSpec] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "").strip()
        raw_model_provider = str(model.get("provider") or "").strip()
        model_provider = (
            dynamic_keys.normalize_key_provider(raw_model_provider) if raw_model_provider else None
        )
        if model_provider:
            _merge_config_provider_spec(
                specs,
                provider=model_provider,
                adapter_kind=model_provider,
                default_base_url=_expand_config_string(model.get("base_url")),
                model_id=model_id,
            )

        routes = model.get("route") or [
            {
                "kind": raw_model_provider,
                "base_url": model.get("base_url"),
            }
        ]
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            raw_kind = str(route.get("kind") or raw_model_provider or "").strip()
            if not raw_kind:
                continue
            provider, adapter_kind = _provider_spec_from_route_kind(raw_kind, model_provider)
            _merge_config_provider_spec(
                specs,
                provider=provider,
                adapter_kind=adapter_kind,
                default_base_url=_expand_config_string(route.get("base_url")),
                model_id=model_id,
            )
    return specs


def _strip_chat_completions(base_url: str) -> None:
    lowered = base_url.rstrip("/").lower()
    if lowered.endswith("/chat/completions"):
        raise HTTPException(
            status_code=422,
            detail="default_base_url must exclude /chat/completions",
        )


def _chat_completions_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _safe_preview(value: str) -> str:
    text = " ".join(value.replace("\x00", "").split())
    return text[:160] if text else ""


def _route_adapters(route: Any) -> list[Any]:
    raw_adapters = getattr(route, "raw_adapters", None)
    if raw_adapters:
        return [entry[0] for entry in raw_adapters]
    return [entry[0] for entry in getattr(route, "adapters", [])]


def _canonical_model_id(model_id: str, route: Any) -> str:
    adapters = _route_adapters(route)
    if not adapters:
        return model_id
    cfg = getattr(adapters[0], "config", None)
    return str(getattr(cfg, "id", model_id) or model_id)


def _provider_candidates_for_adapter(adapter: Any) -> set[str]:
    cfg = getattr(adapter, "config", None)
    metadata = getattr(cfg, "route_metadata", None) or {}
    candidates = {
        getattr(cfg, "provider", None),
        metadata.get("route_provider"),
        metadata.get("upstream_provider"),
        metadata.get("key_provider"),
    }
    normalized: set[str] = set()
    for value in candidates:
        if isinstance(value, str) and value.strip():
            normalized.add(dynamic_keys.normalize_key_provider(value.strip()))
    return normalized


def _models_by_provider(services) -> dict[str, set[str]]:
    by_provider: dict[str, set[str]] = defaultdict(set)
    router_obj = getattr(services, "router", None)
    routes = getattr(router_obj, "routes", {}) if router_obj is not None else {}
    for model_id, route in routes.items():
        canonical_model_id = _canonical_model_id(str(model_id), route)
        if str(model_id) != canonical_model_id:
            continue
        for adapter in _route_adapters(route):
            for provider in _provider_candidates_for_adapter(adapter):
                by_provider[provider].add(canonical_model_id)
    return by_provider


def _merge_config_models_by_provider(
    models_by_provider: dict[str, set[str]],
    config_specs: dict[str, ConfigProviderSpec],
) -> dict[str, set[str]]:
    for provider, spec in config_specs.items():
        models_by_provider[provider].update(spec.model_ids)
    return models_by_provider


async def _active_key_count(op_store, provider: str) -> int:
    db_rows = await op_store.list_provider_keys(provider)
    db_raw_keys = set(await op_store.list_provider_keys_full(provider))
    disabled_env_hashes = set(await op_store.list_disabled_provider_env_key_hashes(provider))
    count = sum(1 for row in db_rows if row.status == "active")

    env_keys: list[str] = []
    for pool in dynamic_keys.get_pools_for_provider(provider):
        for raw in pool.snapshot_keys():
            if raw not in env_keys:
                env_keys.append(raw)
    for raw in dynamic_keys.configured_env_keys_for_provider(provider):
        if raw not in env_keys:
            env_keys.append(raw)
    for raw in dynamic_keys.list_candidate_env_keys(provider):
        if raw not in env_keys:
            env_keys.append(raw)

    for raw in env_keys:
        if raw in db_raw_keys:
            continue
        if dynamic_keys.env_key_hash(raw) in disabled_env_hashes:
            continue
        count += 1
    return count


async def _build_provider_item(
    *,
    provider: str,
    custom_row,
    config_spec: ConfigProviderSpec | None,
    source: str,
    op_store,
    models_by_provider: dict[str, set[str]],
) -> ProviderDefinitionItem:
    runtime_base_urls = dynamic_keys.get_registered_base_urls(provider)
    runtime_base_url = runtime_base_urls[0] if runtime_base_urls else ""

    if custom_row is not None:
        display_name = custom_row.display_name
        adapter_kind = custom_row.adapter_kind
        base_url = custom_row.default_base_url
        status = custom_row.status
        created_at = custom_row.created_at
        updated_at = custom_row.updated_at
    elif provider in PROVIDER_TARGETS:
        target = PROVIDER_TARGETS[provider]
        display_name = target.label
        adapter_kind = target.kind
        base_url = (
            runtime_base_url
            or (config_spec.default_base_url if config_spec else "")
            or target.default_base_url
        )
        status = "active"
        created_at = None
        updated_at = None
    else:
        display_name = _display_name(provider)
        adapter_kind = config_spec.adapter_kind if config_spec else provider
        config_base_url = config_spec.default_base_url if config_spec else ""
        base_url = runtime_base_url or _base_url_with_provider_default(provider, config_base_url)
        status = "active"
        created_at = None
        updated_at = None

    return ProviderDefinitionItem(
        provider=provider,
        display_name=display_name,
        adapter_kind=adapter_kind,
        default_base_url=base_url,
        source=source,  # type: ignore[arg-type]
        status=status,
        keys_count=await _active_key_count(op_store, provider),
        models_count=len(models_by_provider.get(provider, set())),
        created_at=created_at,
        updated_at=updated_at,
    )


async def _probe_openai_compat(
    *,
    default_base_url: str,
    api_key: str,
    probe_model_id: str,
) -> ProbeProviderDefinitionResponse:
    cleaned_base_url = await _validate_base_url(default_base_url)
    _strip_chat_completions(cleaned_base_url)
    key = api_key.strip()
    model_id = probe_model_id.strip()
    if not key:
        raise HTTPException(status_code=422, detail="api_key must not be blank")
    if not model_id:
        raise HTTPException(status_code=422, detail="probe_model_id must not be blank")

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "temperature": 0,
        "max_tokens": 64,
    }
    started = time.perf_counter()
    first_event_ttft_ms: float | None = None
    first_content_ttft_ms: float | None = None
    preview = ""

    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SEC)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                _chat_completions_url(cleaned_base_url),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
                # A validated public host must not be able to 302 the probe to
                # an internal address, bypassing _validate_base_url's IP checks.
                allow_redirects=False,
            ) as response,
        ):
            if response.status >= 400:
                body = _safe_preview(await response.text())
                if response.status in {401, 403}:
                    detail = "Authentication failed. Check the API key."
                elif response.status == 404:
                    detail = "Endpoint not found. Check that Base URL excludes /chat/completions."
                else:
                    detail = f"Provider probe failed with HTTP {response.status}"
                if body:
                    detail = f"{detail} {body}"
                raise HTTPException(status_code=400, detail=detail)

            buffer = b""
            async for chunk in response.content.iter_chunked(1024):
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.strip()
                    if not line or not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        return ProbeProviderDefinitionResponse(
                            first_event_ttft_ms=first_event_ttft_ms,
                            first_content_ttft_ms=first_content_ttft_ms,
                            preview=_safe_preview(preview) or None,
                        )
                    if first_event_ttft_ms is None:
                        first_event_ttft_ms = (time.perf_counter() - started) * 1000
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content") or delta.get("reasoning_content") or ""
                    if isinstance(content, str) and content:
                        if first_content_ttft_ms is None:
                            first_content_ttft_ms = (time.perf_counter() - started) * 1000
                        preview += content
                        if len(preview) >= 160:
                            return ProbeProviderDefinitionResponse(
                                first_event_ttft_ms=first_event_ttft_ms,
                                first_content_ttft_ms=first_content_ttft_ms,
                                preview=_safe_preview(preview),
                            )
            if first_event_ttft_ms is None:
                raise HTTPException(
                    status_code=400,
                    detail="Provider did not return a streaming response.",
                )
            return ProbeProviderDefinitionResponse(
                first_event_ttft_ms=first_event_ttft_ms,
                first_content_ttft_ms=first_content_ttft_ms,
                preview=_safe_preview(preview) or None,
            )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Probe timed out. Check provider availability or model id.",
        ) from exc
    except aiohttp.ClientError as exc:
        raise HTTPException(status_code=400, detail=f"Provider probe failed: {exc}") from exc


@router.get("/provider-definitions", response_model=ListProviderDefinitionsResponse)
async def list_provider_definitions(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
) -> ListProviderDefinitionsResponse:
    """List built-in and custom upstream providers."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not configured")

    definition_rows = {row.provider: row for row in await op_store.list_provider_definitions()}

    config_specs = _configured_provider_specs()
    db_row_providers = set(definition_rows)
    runtime_known_providers = dynamic_keys.get_known_providers() - db_row_providers
    built_in_providers = (
        set(config_specs) | set(PROVIDER_TARGETS) | set(SELECTABLE_PROVIDER_TARGETS)
    ) | runtime_known_providers

    # The definitions table only surfaces genuine custom providers. A row whose
    # slug matches a built-in name is ignored: built-ins are read-only and
    # sourced from code/config, so a stale override or disabled marker left by an
    # earlier build must never resurrect a provider or leak into routing options.
    custom_rows = {
        provider: row
        for provider, row in definition_rows.items()
        if provider not in built_in_providers
    }
    for row in custom_rows.values():
        provider_registry.register_provider_definition(row)

    providers = built_in_providers | set(custom_rows)
    models_by_provider = _merge_config_models_by_provider(
        _models_by_provider(services),
        config_specs,
    )
    rows = [
        await _build_provider_item(
            provider=provider,
            custom_row=custom_rows.get(provider),
            config_spec=config_specs.get(provider),
            source="custom" if provider in custom_rows else "built_in",
            op_store=op_store,
            models_by_provider=models_by_provider,
        )
        for provider in sorted(providers)
    ]
    rows.sort(key=lambda row: (row.source != "custom", row.display_name.lower(), row.provider))
    return ListProviderDefinitionsResponse(providers=rows)


@router.post("/provider-definitions/verify", response_model=ProbeProviderDefinitionResponse)
async def probe_provider_definition(
    payload: ProbeProviderDefinitionRequest,
    _admin_id: str = Depends(verify_admin_access),
) -> ProbeProviderDefinitionResponse:
    """Probe a candidate OpenAI-compatible upstream provider without saving it."""
    return await _probe_openai_compat(
        default_base_url=payload.default_base_url,
        api_key=payload.api_key,
        probe_model_id=payload.probe_model_id,
    )


@router.post(
    "/provider-definitions",
    response_model=ProviderDefinitionItem,
    status_code=201,
)
async def create_provider_definition(
    payload: CreateProviderDefinitionRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
) -> ProviderDefinitionItem:
    """Create a custom OpenAI-compatible provider and its initial key."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not configured")
    if payload.adapter_kind not in SELF_SERVE_ADAPTER_KINDS:
        raise HTTPException(status_code=422, detail="adapter_kind is not supported")

    provider = _validate_provider_slug(payload.provider)
    if (
        provider in PROVIDER_TARGETS
        or provider in dynamic_keys.get_known_providers()
        or provider in _configured_provider_specs()
    ):
        raise HTTPException(status_code=409, detail="provider is reserved or already registered")
    if await op_store.get_provider_definition(provider) is not None:
        raise HTTPException(status_code=409, detail="provider already exists")

    cleaned_base_url = await _validate_base_url(payload.default_base_url)
    _strip_chat_completions(cleaned_base_url)
    await _probe_openai_compat(
        default_base_url=cleaned_base_url,
        api_key=payload.api_key,
        probe_model_id=payload.probe_model_id,
    )

    row = await op_store.upsert_provider_definition(
        provider=provider,
        display_name=payload.display_name.strip(),
        adapter_kind=payload.adapter_kind,
        default_base_url=cleaned_base_url,
        created_by=admin_id,
    )
    try:
        key_id = await op_store.add_provider_key(
            provider=provider,
            api_key=payload.api_key.strip(),
            label=payload.api_key_label or "Initial key",
            created_by=admin_id,
        )
    except Exception:
        await op_store.delete_provider_definition(provider)
        raise
    provider_registry.register_provider_definition(row)
    dynamic_keys.add_key_to_provider(provider, payload.api_key.strip())

    await log_admin_action(
        op_store,
        admin_id,
        "provider_definition.create",
        None,
        {
            "provider": provider,
            "display_name": row.display_name,
            "adapter_kind": row.adapter_kind,
            "default_base_url": row.default_base_url,
            "provider_key_id": key_id,
        },
    )

    models_by_provider = _models_by_provider(services)
    return await _build_provider_item(
        provider=provider,
        custom_row=row,
        config_spec=None,
        source="custom",
        op_store=op_store,
        models_by_provider=models_by_provider,
    )


@router.patch(
    "/provider-definitions/{provider}",
    response_model=ProviderDefinitionItem,
)
async def update_provider_definition(
    provider: str,
    payload: UpdateProviderDefinitionRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
) -> ProviderDefinitionItem:
    """Update a custom provider definition.

    Built-in providers are read-only here: they are defined in
    config/models.yaml and managed through the Routing and Keys tabs.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not configured")

    provider_slug = _validate_provider_slug(provider)
    config_specs = _configured_provider_specs()
    if _is_config_managed_provider(provider_slug, config_specs):
        raise HTTPException(
            status_code=409,
            detail="Built-in providers are defined in config/models.yaml and cannot be edited here.",
        )
    row = await op_store.get_provider_definition(provider_slug)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    display_name = row.display_name
    if payload.display_name is not None:
        display_name = payload.display_name.strip()
        if not display_name:
            raise HTTPException(status_code=422, detail="display_name must not be blank")

    cleaned_base_url = row.default_base_url
    base_url_changed = False
    if payload.default_base_url is not None:
        cleaned_base_url = await _validate_base_url(payload.default_base_url)
        _strip_chat_completions(cleaned_base_url)
        base_url_changed = cleaned_base_url != row.default_base_url

    if base_url_changed:
        models = _merge_config_models_by_provider(
            _models_by_provider(services),
            config_specs,
        ).get(provider_slug, set())
        if models:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{row.display_name} is used by {len(models)} model(s). "
                    "Update those routes in Routing before changing the provider Base URL."
                ),
            )
        if not payload.api_key or not payload.probe_model_id:
            raise HTTPException(
                status_code=422,
                detail="api_key and probe_model_id are required when changing Base URL",
            )
        await _probe_openai_compat(
            default_base_url=cleaned_base_url,
            api_key=payload.api_key,
            probe_model_id=payload.probe_model_id,
        )

    updated = await op_store.upsert_provider_definition(
        provider=provider_slug,
        display_name=display_name,
        adapter_kind=row.adapter_kind,
        default_base_url=cleaned_base_url,
        created_by=admin_id,
        status="active",
    )
    provider_registry.register_provider_definition(updated)

    await log_admin_action(
        op_store,
        admin_id,
        "provider_definition.update",
        None,
        {
            "provider": provider_slug,
            "display_name": updated.display_name,
            "default_base_url": updated.default_base_url,
            "base_url_changed": base_url_changed,
        },
    )

    models_by_provider = _merge_config_models_by_provider(
        _models_by_provider(services),
        config_specs,
    )
    return await _build_provider_item(
        provider=provider_slug,
        custom_row=updated,
        config_spec=None,
        source="custom",
        op_store=op_store,
        models_by_provider=models_by_provider,
    )


@router.delete(
    "/provider-definitions/{provider}",
    response_model=DeleteProviderDefinitionResponse,
)
async def delete_provider_definition(
    provider: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
) -> DeleteProviderDefinitionResponse:
    """Delete an unused custom provider definition and its stored keys.

    Built-in providers are read-only here: they are defined in
    config/models.yaml and cannot be deleted from the admin registry.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not configured")

    provider_slug = _validate_provider_slug(provider)
    config_specs = _configured_provider_specs()
    if _is_config_managed_provider(provider_slug, config_specs):
        raise HTTPException(
            status_code=409,
            detail="Built-in providers are defined in config/models.yaml and cannot be deleted here.",
        )
    row = await op_store.get_provider_definition(provider_slug)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    models = _merge_config_models_by_provider(
        _models_by_provider(services),
        config_specs,
    ).get(provider_slug, set())
    if models:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{row.display_name} is used by {len(models)} model(s). "
                "Remove those routes in Routing before deleting this provider."
            ),
        )

    deleted_keys = await op_store.delete_provider_keys_for_provider(provider_slug)
    deleted = await op_store.delete_provider_definition(provider_slug)
    if not deleted:
        raise HTTPException(status_code=404, detail="provider not found")
    provider_registry.unregister_provider_definition(provider_slug)

    await log_admin_action(
        op_store,
        admin_id,
        "provider_definition.delete",
        None,
        {
            "provider": provider_slug,
            "display_name": row.display_name,
            "deleted_keys": deleted_keys,
        },
    )
    return DeleteProviderDefinitionResponse(provider=provider_slug, deleted_keys=deleted_keys)
