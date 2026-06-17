"""Admin endpoints for runtime provider route target overrides."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import socket
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from routing.routers import _get_endpoint_id
from serving.adapters import ModelConfig, dynamic_keys
from serving.schemas_admin import (
    CreateProviderRouteRequest,
    ListAllProviderRoutesResponse,
    ListProviderRoutesResponse,
    ProviderRouteApiKeyRef,
    ProviderRouteItem,
    ProviderRouteOption,
    UpdateProviderRouteRequest,
    UpdateProviderRouteStrategyRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, get_services, verify_admin_access
from serving.servers.registry import _make_adapter, _make_provider_id, parse_openrouter_kind
from serving.utils.logging import get_logger

router = APIRouter(prefix="/admin")
logger = get_logger(__name__)
VERIFY_TIMEOUT_SEC = 20.0
BASELINE_ENTRIES_ATTR = "_provider_route_baseline_entries"
MODEL_ROUTER_STRATEGY_SETTING_PREFIX = "model_router_strategy:"
MODEL_ROUTER_STRATEGIES = {"fixed", "routewise"}


@dataclass(frozen=True)
class ProviderTarget:
    provider: str
    label: str
    kind: str
    key_provider: str
    default_base_url: str


def _env_default(name: str, fallback: str) -> str:
    return (os.getenv(name) or fallback).strip()


BLOCKED_BASE_URL_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "host.docker.internal",
    "metadata.google.internal",
}

PROVIDER_TARGETS: dict[str, ProviderTarget] = {
    "chutes": ProviderTarget(
        provider="chutes",
        label="Chutes",
        kind="chutes",
        key_provider="chutes",
        default_base_url=_env_default("CHUTES_BASE_URL", "https://llm.chutes.ai/v1"),
    ),
    "featherless": ProviderTarget(
        provider="featherless",
        label="Featherless",
        kind="featherless",
        key_provider="featherless",
        default_base_url=_env_default("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"),
    ),
    "openrouter": ProviderTarget(
        provider="openrouter",
        label="OpenRouter",
        kind="openrouter",
        key_provider="openrouter",
        default_base_url="https://openrouter.ai/api/v1",
    ),
    "deepinfra": ProviderTarget(
        provider="deepinfra",
        label="DeepInfra via OpenRouter",
        kind="openrouter[deepinfra]",
        key_provider="openrouter",
        default_base_url="https://openrouter.ai/api/v1",
    ),
    "parasail": ProviderTarget(
        provider="parasail",
        label="Parasail via OpenRouter",
        kind="openrouter[parasail]",
        key_provider="openrouter",
        default_base_url="https://openrouter.ai/api/v1",
    ),
}

PROVIDER_MODEL_IDS: dict[str, dict[str, str]] = {
    "minimax-fast": {
        "chutes": "MiniMaxAI/MiniMax-M2.5-TEE",
        "featherless": "MiniMaxAI/MiniMax-M2.5",
        "deepinfra": "minimax/minimax-m2.5",
        "openrouter": "minimax/minimax-m2.5",
        "parasail": "minimax/minimax-m2.5",
    },
    "minimax-m2.5": {
        "chutes": "MiniMaxAI/MiniMax-M2.5-TEE",
        "featherless": "MiniMaxAI/MiniMax-M2.5",
        "openrouter": "minimax/minimax-m2.5",
        "deepinfra": "minimax/minimax-m2.5",
        "parasail": "minimax/minimax-m2.5",
    },
}


@dataclass
class PreparedRouteUpdate:
    route: Any
    route_id: str
    route_index: int
    adapter: Any
    endpoint_id: str
    upstream_provider: str
    key_provider: str
    base_url: str
    api_key_id: str | None
    provider_model_id: str
    quota_limit: int | None


@dataclass
class PreparedRouteCandidate:
    route: Any
    route_id: str
    adapter: Any
    endpoint_id: str
    upstream_provider: str
    key_provider: str
    base_url: str
    api_key_id: str | None
    provider_model_id: str
    route_type: str
    quota_limit: int | None
    concurrency_limit: int | None
    weight: float


def _mask(api_key: str) -> str:
    if len(api_key) >= 16:
        return f"{api_key[:8]}...{api_key[-4:]}"
    return "***configured***"


def _env_key_id(api_key: str) -> str:
    return f"env:{dynamic_keys.env_key_hash(api_key)[:32]}"


def _strategy_for_model(services, model_id: str) -> str:
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return "fixed"
    return registry.get_router_name(model_id)


def _model_strategy_setting_key(model_id: str) -> str:
    return f"{MODEL_ROUTER_STRATEGY_SETTING_PREFIX}{model_id}"


def _model_id_from_strategy_setting_key(key: str) -> str | None:
    if not key.startswith(MODEL_ROUTER_STRATEGY_SETTING_PREFIX):
        return None
    model_id = key[len(MODEL_ROUTER_STRATEGY_SETTING_PREFIX) :]
    return model_id or None


def _validate_model_router_strategy(services, model_id: str, strategy: str) -> tuple[str, Any]:
    if strategy not in MODEL_ROUTER_STRATEGIES:
        raise HTTPException(status_code=422, detail="strategy must be fixed or routewise")
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="Model router registry not configured")
    route = _validate_canonical_route(services, model_id)
    canonical_model_id = route.adapters[0][0].config.id
    validate = getattr(registry, "validate_router_strategy", None)
    if validate is not None:
        validate(canonical_model_id, strategy)
    return canonical_model_id, route


def _apply_model_router_strategy(services, model_id: str, strategy: str) -> None:
    canonical_model_id, _route = _validate_model_router_strategy(services, model_id, strategy)
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="Model router registry not configured")
    setter = getattr(registry, "set_router_override", None)
    if setter is None:
        raise HTTPException(status_code=500, detail="Model router registry cannot be updated")
    setter(canonical_model_id, strategy)


def _is_canonical_model(model_id: str, route) -> bool:
    return bool(route.adapters) and route.adapters[0][0].config.id == model_id


def _raw_route_entries(route) -> list[tuple[object, float, str]]:
    raw_adapters = getattr(route, "raw_adapters", None)
    if raw_adapters:
        return list(raw_adapters)
    return [
        (adapter, float(weight), _get_endpoint_id(adapter)) for adapter, weight in route.adapters
    ]


def _route_id_for_entry(adapter, endpoint_id: str) -> str:
    route_metadata = getattr(adapter.config, "route_metadata", None) or {}
    route_id = route_metadata.get("route_id")
    return str(route_id or endpoint_id)


def _baseline_route_entries(route) -> list[tuple[object, float, str]]:
    baseline = getattr(route, BASELINE_ENTRIES_ATTR, None)
    if baseline is None:
        baseline = list(_raw_route_entries(route))
        setattr(route, BASELINE_ENTRIES_ATTR, baseline)
    return list(baseline)


def _route_index_for_id(entries: list[tuple[object, float, str]], route_id: str) -> int:
    if not route_id:
        raise HTTPException(status_code=400, detail="unknown route id")
    matches: list[int] = []
    for index, (adapter, _weight, endpoint_id) in enumerate(entries):
        if _route_id_for_entry(adapter, endpoint_id) == route_id:
            matches.append(index)
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"duplicate provider route id {route_id!r}; set unique endpoint_id values "
                "before editing this route"
            ),
        )
    if matches:
        return matches[0]
    raise HTTPException(status_code=400, detail="unknown route id")


def _validate_canonical_route(services, model_id: str):
    route = services.router.routes.get(model_id)
    if route is None or not _is_canonical_model(model_id, route):
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")
    return route


def _split_model_route_path(services, model_route_path: str) -> tuple[str, str, Any]:
    for model_id in sorted(services.router.routes, key=len, reverse=True):
        route = services.router.routes[model_id]
        prefix = f"{model_id}/"
        if _is_canonical_model(model_id, route) and model_route_path.startswith(prefix):
            route_id = model_route_path[len(prefix) :]
            if not route_id:
                raise HTTPException(status_code=400, detail="unknown route id")
            return model_id, route_id, route
    raise HTTPException(status_code=404, detail="Unknown model")


def _target_for_provider(provider: str) -> ProviderTarget:
    if provider in PROVIDER_TARGETS:
        return PROVIDER_TARGETS[provider]
    known = dynamic_keys.get_known_providers()
    if provider in known:
        return ProviderTarget(
            provider=provider,
            label=provider,
            kind=provider,
            key_provider=provider,
            default_base_url="",
        )
    raise HTTPException(
        status_code=400,
        detail=f"Unknown provider {provider!r}. Valid providers: {sorted(PROVIDER_TARGETS)}",
    )


def _provider_options() -> list[ProviderRouteOption]:
    options = [
        ProviderRouteOption(
            provider=target.provider,
            label=target.label,
            kind=target.kind,
            key_provider=target.key_provider,
            default_base_url=target.default_base_url,
        )
        for target in PROVIDER_TARGETS.values()
    ]
    for provider in sorted(set(dynamic_keys.get_known_providers()) - set(PROVIDER_TARGETS)):
        options.append(
            ProviderRouteOption(
                provider=provider,
                label=provider,
                kind=provider,
                key_provider=provider,
                default_base_url="",
            )
        )
    return options


def _upstream_provider(adapter) -> str:
    pinned = getattr(adapter.config, "openrouter_pinned_provider", None)
    if pinned:
        return str(pinned)
    return str(adapter.config.provider)


def _route_provider(adapter) -> str:
    route_metadata = getattr(adapter.config, "route_metadata", None) or {}
    provider = route_metadata.get("route_provider")
    if provider:
        return str(provider)
    return _upstream_provider(adapter)


def _route_type(adapter) -> str:
    route_metadata = getattr(adapter.config, "route_metadata", None) or {}
    return str(
        getattr(adapter.config, "provider_type", None)
        or route_metadata.get("provider_type")
        or "on_demand"
    )


def _validate_base_url(base_url: str) -> str:
    cleaned = base_url.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="base_url must not be blank")

    if "://" not in cleaned:
        cleaned = f"https://{cleaned.lstrip('/')}"

    parsed = urlparse(cleaned)
    if parsed.scheme != "https":
        raise HTTPException(status_code=422, detail="base_url must use https")
    if not parsed.netloc or not parsed.hostname:
        raise HTTPException(status_code=422, detail="base_url must include a host")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="base_url must not include credentials")
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=422, detail="base_url must not include query or fragment")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in BLOCKED_BASE_URL_HOSTS or hostname.endswith(".localhost"):
        raise HTTPException(status_code=422, detail="base_url host is not allowed")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                hostname,
                parsed.port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise HTTPException(
                status_code=422,
                detail="base_url host could not be resolved",
            ) from exc

        for result in resolved:
            address = result[4][0]
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="base_url host resolved to an invalid address",
                ) from None
            if _base_url_ip_blocked(resolved_ip):
                raise HTTPException(
                    status_code=422,
                    detail="base_url host is not allowed",
                ) from None
        return cleaned

    if _base_url_ip_blocked(ip):
        raise HTTPException(status_code=422, detail="base_url host is not allowed")
    return cleaned


def _base_url_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _config_to_dict(config: Any) -> dict[str, Any]:
    if is_dataclass(config):
        return asdict(config)
    values: dict[str, Any] = {}
    for field in fields(ModelConfig):
        if hasattr(config, field.name):
            values[field.name] = getattr(config, field.name)
    return values


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


async def _resolve_key_material(
    op_store,
    *,
    key_provider: str,
    api_key_id: str | None,
) -> tuple[str | None, list[str] | None]:
    if api_key_id is None:
        keys: list[str] = []
        for pool in dynamic_keys.get_pools_for_provider(key_provider):
            keys.extend(pool.snapshot_keys())
        try:
            keys.extend(await op_store.list_provider_keys_full(key_provider))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Failed to load provider keys for {key_provider}: {exc}",
            ) from exc
        keys = _unique([key for key in keys if key.strip()])
        if not keys:
            raise HTTPException(
                status_code=400,
                detail=f"No API keys configured for provider {key_provider!r}",
            )
        return None, keys

    if api_key_id.startswith("env:"):
        for pool in dynamic_keys.get_pools_for_provider(key_provider):
            for raw in pool.snapshot_keys():
                if _env_key_id(raw) == api_key_id:
                    return raw, None
        raise HTTPException(status_code=404, detail="Env provider key not found")

    row = await op_store.get_provider_key_full(api_key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider key not found")
    row_provider, raw_key = row
    if row_provider != key_provider:
        raise HTTPException(
            status_code=400,
            detail=f"Provider key belongs to {row_provider!r}, expected {key_provider!r}",
        )
    return raw_key, None


async def _api_key_ref(
    op_store,
    *,
    key_provider: str,
    api_key_id: str | None,
) -> ProviderRouteApiKeyRef:
    if api_key_id is None:
        return ProviderRouteApiKeyRef(
            id=None,
            provider=key_provider,
            label="Provider default",
            key_prefix=None,
            source="default",
        )
    if api_key_id.startswith("env:"):
        for pool in dynamic_keys.get_pools_for_provider(key_provider):
            for raw in pool.snapshot_keys():
                if _env_key_id(raw) == api_key_id:
                    return ProviderRouteApiKeyRef(
                        id=api_key_id,
                        provider=key_provider,
                        label=None,
                        key_prefix=_mask(raw),
                        source="env",
                    )
        return ProviderRouteApiKeyRef(
            id=api_key_id,
            provider=key_provider,
            label="Missing key",
            key_prefix=None,
            source="missing",
        )

    rows = await op_store.list_provider_keys(key_provider)
    for row in rows:
        if row.id == api_key_id:
            return ProviderRouteApiKeyRef(
                id=row.id,
                provider=row.provider,
                label=row.label,
                key_prefix=row.key_prefix,
                source="db",
            )
    return ProviderRouteApiKeyRef(
        id=api_key_id,
        provider=key_provider,
        label="Missing key",
        key_prefix=None,
        source="missing",
    )


def _resolve_provider_model_id(
    model_id: str,
    upstream_provider: str,
    route_entries: list[tuple[object, float, str]],
    current_adapter,
) -> str:
    mapped = PROVIDER_MODEL_IDS.get(model_id, {}).get(upstream_provider)
    if mapped:
        return mapped

    current_provider = _upstream_provider(current_adapter)
    current_provider_model_id = getattr(current_adapter.config, "provider_model_id", None)
    if upstream_provider == current_provider and current_provider_model_id:
        return str(current_provider_model_id)
    if upstream_provider == current_provider:
        return model_id

    for adapter, _weight, _endpoint_id in route_entries:
        if _upstream_provider(adapter) == upstream_provider:
            provider_model_id = getattr(adapter.config, "provider_model_id", None)
            if provider_model_id:
                return str(provider_model_id)

    target = _target_for_provider(upstream_provider)
    base_kind, _pinned = parse_openrouter_kind(target.kind)
    if base_kind == "openrouter":
        for adapter, _weight, _endpoint_id in route_entries:
            if getattr(adapter.config, "provider", None) == "openrouter":
                provider_model_id = getattr(adapter.config, "provider_model_id", None)
                if provider_model_id:
                    return str(provider_model_id)

    raise HTTPException(
        status_code=400,
        detail=f"{upstream_provider} is not configured for {model_id} yet",
    )


def _preserve_route_semantics(
    cfg: dict[str, Any],
    *,
    current_adapter,
    upstream_provider: str,
    route_id: str,
) -> None:
    route_metadata = dict(cfg.get("route_metadata") or {})
    route_metadata["route_id"] = route_id
    route_metadata["route_provider"] = _route_provider(current_adapter)
    route_metadata["upstream_provider"] = upstream_provider

    provider_type = _route_type(current_adapter)
    cfg["provider_type"] = provider_type
    route_metadata["provider_type"] = provider_type
    cfg["route_metadata"] = route_metadata


def _quota_limit_for_adapter(adapter) -> int | None:
    if _route_type(adapter) != "quota":
        return None
    quota = getattr(adapter.config, "quota", None)
    if not isinstance(quota, dict):
        return None
    raw_limit = quota.get("limit")
    return int(raw_limit) if raw_limit is not None else None


def _is_runtime_candidate(adapter) -> bool:
    route_metadata = getattr(adapter.config, "route_metadata", None)
    return isinstance(route_metadata, dict) and route_metadata.get("runtime_candidate") is True


def _concurrency_limit_for_adapter(adapter) -> int | None:
    if _route_type(adapter) != "concurrency":
        return None
    concurrency = getattr(adapter.config, "concurrency", None)
    if not isinstance(concurrency, dict):
        return None
    raw_limit = concurrency.get("limit")
    return int(raw_limit) if raw_limit is not None else None


def _validate_positive_weight(weight: float) -> float:
    value = float(weight)
    if not math.isfinite(value) or value <= 0:
        raise HTTPException(status_code=422, detail="weight must be a positive finite number")
    return value


def _ensure_route_id_available(entries: list[tuple[object, float, str]], route_id: str) -> None:
    for adapter, _weight, endpoint_id in entries:
        if _route_id_for_entry(adapter, endpoint_id) == route_id:
            raise HTTPException(status_code=409, detail="provider route already exists")


def _runtime_quota_pool(_model_id: str, route_id: str) -> str:
    return f"{route_id}:runtime-quota"


def _runtime_concurrency_pool(_model_id: str, route_id: str) -> str:
    return f"{route_id}:runtime-concurrency"


async def _prepare_route_candidate(
    services,
    op_store,
    *,
    model_id: str,
    route_type: str,
    upstream_provider: str,
    base_url: str,
    api_key_id: str | None,
    provider_model_id: str,
    quota_limit: int | None,
    concurrency_limit: int | None,
    weight: float,
    route_id: str | None = None,
) -> PreparedRouteCandidate:
    route = _validate_canonical_route(services, model_id)
    entries = _raw_route_entries(route)
    if route_type not in {"quota", "concurrency", "on_demand"}:
        raise HTTPException(status_code=422, detail="unknown route_type")
    if route_type == "quota" and quota_limit is None:
        raise HTTPException(status_code=422, detail="quota_limit is required for quota routes")
    if route_type != "quota" and quota_limit is not None:
        raise HTTPException(status_code=422, detail="quota_limit is only valid for quota routes")
    if route_type == "concurrency" and concurrency_limit is None:
        raise HTTPException(
            status_code=422,
            detail="concurrency_limit is required for concurrency routes",
        )
    if route_type != "concurrency" and concurrency_limit is not None:
        raise HTTPException(
            status_code=422,
            detail="concurrency_limit is only valid for concurrency routes",
        )

    target = _target_for_provider(upstream_provider)
    cleaned_base_url = _validate_base_url(base_url)
    provider_model_id = provider_model_id.strip()
    if not provider_model_id:
        raise HTTPException(status_code=422, detail="provider_model_id must not be blank")
    raw_weight = _validate_positive_weight(weight)

    provider_for_cfg, _pinned = parse_openrouter_kind(target.kind)
    candidate_route_id = route_id or _make_provider_id(model_id, target.kind, cleaned_base_url)
    _ensure_route_id_available(entries, candidate_route_id)

    api_key, api_keys = await _resolve_key_material(
        op_store,
        key_provider=target.key_provider,
        api_key_id=api_key_id,
    )

    template_adapter = entries[0][0]
    cfg = _config_to_dict(template_adapter.config)
    cfg.update(
        {
            "id": model_id,
            "name": model_id,
            "provider": provider_for_cfg,
            "base_url": cleaned_base_url,
            "api_key": api_key,
            "api_keys": api_keys,
            "provider_model_id": provider_model_id,
            "endpoint_id": candidate_route_id,
            "provider_type": route_type,
            "quota_pool": None,
            "quota_source": None,
            "quota": None,
            "concurrency_pool": None,
            "concurrency": None,
            "route_metadata": {
                "route_id": candidate_route_id,
                "route_provider": upstream_provider,
                "upstream_provider": upstream_provider,
                "provider_type": route_type,
                "runtime_candidate": True,
            },
        }
    )
    if route_type == "quota":
        quota_pool = _runtime_quota_pool(model_id, candidate_route_id)
        cfg["quota_pool"] = quota_pool
        cfg["quota_source"] = {
            "provider": "local",
            "usage_label": f"routewise:{quota_pool}",
            "unit": "requests",
        }
        cfg["quota"] = {"limit": quota_limit}
        cfg["route_metadata"]["local_quota_fallback"] = True
    elif route_type == "concurrency":
        cfg["concurrency_pool"] = _runtime_concurrency_pool(model_id, candidate_route_id)
        cfg["concurrency"] = {"limit": concurrency_limit}

    adapter = _make_adapter(target.kind, cfg)
    return PreparedRouteCandidate(
        route=route,
        route_id=candidate_route_id,
        adapter=adapter,
        endpoint_id=_get_endpoint_id(adapter),
        upstream_provider=upstream_provider,
        key_provider=target.key_provider,
        base_url=cleaned_base_url,
        api_key_id=api_key_id,
        provider_model_id=provider_model_id,
        route_type=route_type,
        quota_limit=_quota_limit_for_adapter(adapter),
        concurrency_limit=_concurrency_limit_for_adapter(adapter),
        weight=raw_weight,
    )


async def _prepare_route_update(
    services,
    op_store,
    *,
    model_id: str,
    route_id: str,
    upstream_provider: str,
    base_url: str,
    api_key_id: str | None,
    provider_model_id_override: str | None = None,
    quota_limit_override: int | None = None,
) -> PreparedRouteUpdate:
    route = _validate_canonical_route(services, model_id)
    entries = _raw_route_entries(route)
    index = _route_index_for_id(entries, route_id)

    current_adapter, _raw_weight, _old_endpoint_id = entries[index]
    route_type = _route_type(current_adapter)
    route_provider = _route_provider(current_adapter)
    if quota_limit_override is not None:
        if route_type != "quota":
            raise HTTPException(
                status_code=400,
                detail="quota_limit can only be set for quota routes",
            )
        if route_provider == upstream_provider:
            raise HTTPException(
                status_code=400,
                detail="quota_limit is only supported when using an override provider",
            )
    target = _target_for_provider(upstream_provider)
    cleaned_base_url = _validate_base_url(base_url)

    api_key, api_keys = await _resolve_key_material(
        op_store,
        key_provider=target.key_provider,
        api_key_id=api_key_id,
    )
    provider_model_id = provider_model_id_override or _resolve_provider_model_id(
        model_id, upstream_provider, entries, current_adapter
    )

    cfg = _config_to_dict(current_adapter.config)
    cfg["base_url"] = cleaned_base_url
    cfg["api_key"] = api_key
    cfg["api_keys"] = api_keys
    cfg["provider_model_id"] = provider_model_id
    cfg["endpoint_id"] = _make_provider_id(model_id, target.kind, cleaned_base_url)
    if quota_limit_override is not None:
        quota = dict(cfg.get("quota") or {})
        quota["limit"] = quota_limit_override
        cfg["quota"] = quota

    provider_for_cfg, _pinned = parse_openrouter_kind(target.kind)
    cfg["provider"] = provider_for_cfg
    _preserve_route_semantics(
        cfg,
        current_adapter=current_adapter,
        upstream_provider=upstream_provider,
        route_id=route_id,
    )

    adapter = _make_adapter(target.kind, cfg)

    return PreparedRouteUpdate(
        route=route,
        route_id=route_id,
        route_index=index,
        adapter=adapter,
        endpoint_id=_get_endpoint_id(adapter),
        upstream_provider=upstream_provider,
        key_provider=target.key_provider,
        base_url=cleaned_base_url,
        api_key_id=api_key_id,
        provider_model_id=provider_model_id,
        quota_limit=_quota_limit_for_adapter(adapter),
    )


def _install_route_update(services, update: PreparedRouteUpdate) -> None:
    _baseline_route_entries(update.route)
    entries = _raw_route_entries(update.route)
    if update.route_index >= len(entries):
        raise HTTPException(status_code=400, detail="unknown route id")
    _old_adapter, raw_weight, _old_endpoint_id = entries[update.route_index]
    entries[update.route_index] = (update.adapter, float(raw_weight), update.endpoint_id)

    total_weight = sum(float(weight) for _adapter, weight, _endpoint_id in entries)
    if total_weight <= 0:
        raise HTTPException(status_code=400, detail="cannot zero all routes for model")

    def _mutate() -> None:
        update.route.raw_adapters = entries
        update.route.adapters = [
            (adapter, float(weight) / total_weight) for adapter, weight, _endpoint_id in entries
        ]

    lock = getattr(services.router, "_lock", None)
    if lock is not None:
        with lock:
            _mutate()
    else:
        _mutate()

    dynamic_keys.register_known_provider(update.key_provider)
    if getattr(update.adapter, "_key_pool", None) is not None:
        dynamic_keys.register_adapter_for_provider(update.key_provider, update.adapter)

    _rebuild_routewise_routers(services)


def _install_route_restore(services, route, route_id: str) -> tuple[object, float, str]:
    baseline_entries = _baseline_route_entries(route)
    current_entries = _raw_route_entries(route)
    current_index = _route_index_for_id(current_entries, route_id)
    baseline_index = _route_index_for_id(baseline_entries, route_id)
    baseline_adapter, baseline_weight, baseline_endpoint_id = baseline_entries[baseline_index]
    current_entries[current_index] = (
        baseline_adapter,
        float(baseline_weight),
        baseline_endpoint_id,
    )

    total_weight = sum(float(weight) for _adapter, weight, _endpoint_id in current_entries)
    if total_weight <= 0:
        raise HTTPException(status_code=400, detail="cannot zero all routes for model")

    def _mutate() -> None:
        route.raw_adapters = current_entries
        route.adapters = [
            (adapter, float(weight) / total_weight)
            for adapter, weight, _endpoint_id in current_entries
        ]

    lock = getattr(services.router, "_lock", None)
    if lock is not None:
        with lock:
            _mutate()
    else:
        _mutate()

    _rebuild_routewise_routers(services)
    return baseline_adapter, float(baseline_weight), baseline_endpoint_id


def _install_route_candidate(services, candidate: PreparedRouteCandidate) -> None:
    _baseline_route_entries(candidate.route)
    entries = _raw_route_entries(candidate.route)
    _ensure_route_id_available(entries, candidate.route_id)
    entries.append((candidate.adapter, candidate.weight, candidate.endpoint_id))

    total_weight = sum(float(weight) for _adapter, weight, _endpoint_id in entries)
    if total_weight <= 0:
        raise HTTPException(status_code=400, detail="cannot zero all routes for model")

    def _mutate() -> None:
        candidate.route.raw_adapters = entries
        candidate.route.adapters = [
            (adapter, float(weight) / total_weight) for adapter, weight, _endpoint_id in entries
        ]

    lock = getattr(services.router, "_lock", None)
    if lock is not None:
        with lock:
            _mutate()
    else:
        _mutate()

    dynamic_keys.register_known_provider(candidate.key_provider)
    if getattr(candidate.adapter, "_key_pool", None) is not None:
        dynamic_keys.register_adapter_for_provider(candidate.key_provider, candidate.adapter)

    _rebuild_routewise_routers(services)


def _install_route_candidate_delete(services, route, route_id: str) -> tuple[object, float, str]:
    current_entries = _raw_route_entries(route)
    current_index = _route_index_for_id(current_entries, route_id)
    adapter, raw_weight, endpoint_id = current_entries[current_index]
    if not _is_runtime_candidate(adapter):
        raise HTTPException(
            status_code=400,
            detail="Only runtime-added provider routes can be deleted",
        )
    next_entries = current_entries[:current_index] + current_entries[current_index + 1 :]
    if not next_entries:
        raise HTTPException(status_code=400, detail="cannot delete the last route for model")

    total_weight = sum(float(weight) for _adapter, weight, _endpoint_id in next_entries)
    if total_weight <= 0:
        raise HTTPException(status_code=400, detail="cannot zero all routes for model")

    def _mutate() -> None:
        route.raw_adapters = next_entries
        route.adapters = [
            (entry_adapter, float(weight) / total_weight)
            for entry_adapter, weight, _endpoint_id in next_entries
        ]

    lock = getattr(services.router, "_lock", None)
    if lock is not None:
        with lock:
            _mutate()
    else:
        _mutate()

    _rebuild_routewise_routers(services)
    return adapter, float(raw_weight), endpoint_id


def _truncate(value: str, limit: int = 500) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def _verification_error_detail(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"Provider verification timed out after {VERIFY_TIMEOUT_SEC:.0f}s"
    if isinstance(exc, aiohttp.ClientResponseError):
        body = getattr(exc, "error_body", "")
        body_text = f": {_truncate(str(body))}" if body else ""
        return f"Provider verification failed with HTTP {exc.status}{body_text}"
    return f"Provider verification failed: {_truncate(str(exc) or type(exc).__name__)}"


async def _verify_provider_route(update: PreparedRouteUpdate | PreparedRouteCandidate) -> None:
    """Send a minimal upstream request before accepting a route target change."""
    try:
        await asyncio.wait_for(
            update.adapter.chat_completion(
                [{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            ),
            timeout=VERIFY_TIMEOUT_SEC,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_verification_error_detail(exc)) from exc


def _rebuild_routewise_routers(services) -> None:
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return
    seen: set[int] = set()
    for router_obj in registry.cached_routers():
        if id(router_obj) in seen:
            continue
        seen.add(id(router_obj))
        rebuild = getattr(router_obj, "_rebuild_from_fixed_router", None)
        if rebuild is not None:
            rebuild()


def _effective_weight(services, model_id: str, raw_weight: float, endpoint_id: str) -> float:
    resolver = getattr(services, "weight_override_resolver", None)
    if resolver is None:
        return float(raw_weight)
    get_snapshot = getattr(resolver, "get_snapshot_for_model", None)
    if get_snapshot is None:
        return float(raw_weight)
    return float(get_snapshot(model_id).get(endpoint_id, raw_weight))


def _quota_limit_for_row(adapter, override_row: dict[str, Any] | None) -> int | None:
    if _route_type(adapter) != "quota":
        return None
    if override_row and override_row.get("quota_limit") is not None:
        return int(override_row["quota_limit"])
    return _quota_limit_for_adapter(adapter)


async def _route_row(
    services,
    op_store,
    *,
    model_id: str,
    strategy: str,
    route_id: str,
    adapter,
    yaml_weight: float,
    endpoint_id: str,
    override_row: dict[str, Any] | None,
) -> ProviderRouteItem:
    route_provider = _route_provider(adapter)
    upstream_provider = (
        str(override_row["provider"]) if override_row else _upstream_provider(adapter)
    )
    target = _target_for_provider(upstream_provider)
    api_key_id = (
        str(override_row["api_key_id"])
        if override_row and override_row.get("api_key_id") is not None
        else None
    )
    api_key = await _api_key_ref(
        op_store,
        key_provider=target.key_provider,
        api_key_id=api_key_id,
    )
    base_url = str(override_row["base_url"]) if override_row else str(adapter.config.base_url)
    provider_model_id = (
        str(override_row["provider_model_id"])
        if override_row and override_row.get("provider_model_id") is not None
        else getattr(adapter.config, "provider_model_id", None)
    )
    source = "override" if override_row else ("runtime" if _is_runtime_candidate(adapter) else "yaml")
    return ProviderRouteItem(
        model_id=model_id,
        strategy=strategy,
        route_id=route_id,
        route_type=_route_type(adapter),
        provider=route_provider,
        upstream_provider=upstream_provider,
        key_provider=target.key_provider,
        base_url=base_url,
        api_key_id=api_key_id,
        api_key=api_key,
        provider_model_id=provider_model_id,
        quota_limit=_quota_limit_for_row(adapter, override_row),
        endpoint_id=endpoint_id,
        yaml_weight=float(yaml_weight),
        effective_weight=_effective_weight(services, model_id, float(yaml_weight), endpoint_id),
        source=source,
        updated_at=override_row.get("updated_at") if override_row else None,
        updated_by=override_row.get("updated_by") if override_row else None,
    )


async def _build_routes_for_model(
    services,
    op_store,
    *,
    model_id: str,
    route,
    overrides: dict[str, dict[str, Any]],
) -> list[ProviderRouteItem]:
    strategy = _strategy_for_model(services, model_id)
    rows: list[ProviderRouteItem] = []
    for adapter, yaml_weight, endpoint_id in _raw_route_entries(route):
        route_id = _route_id_for_entry(adapter, endpoint_id)
        rows.append(
            await _route_row(
                services,
                op_store,
                model_id=model_id,
                strategy=strategy,
                route_id=route_id,
                adapter=adapter,
                yaml_weight=float(yaml_weight),
                endpoint_id=endpoint_id,
                override_row=overrides.get(route_id),
            )
        )
    return rows


def _rows_by_route_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["route_id"]): row for row in rows}


@router.get("/routing/provider-routes/{model_id:path}", response_model=ListProviderRoutesResponse)
async def list_provider_routes(
    model_id: str,
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListProviderRoutesResponse:
    """List runtime provider route targets for one canonical model."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    route = _validate_canonical_route(services, model_id)
    override_rows = await op_store.list_provider_route_configs_for_model(model_id)
    strategy = _strategy_for_model(services, model_id)
    return ListProviderRoutesResponse(
        model_id=model_id,
        strategy=strategy,
        provider_options=_provider_options(),
        routes=await _build_routes_for_model(
            services,
            op_store,
            model_id=model_id,
            route=route,
            overrides=_rows_by_route_id(override_rows),
        ),
    )


@router.get("/routing/provider-routes", response_model=ListAllProviderRoutesResponse)
async def list_all_provider_routes(
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListAllProviderRoutesResponse:
    """List runtime provider route targets for all canonical models."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    override_rows = await op_store.list_all_provider_route_configs()
    overrides_by_model: dict[str, dict[str, dict[str, Any]]] = {}
    for row in override_rows:
        model_id = str(row["model_id"])
        overrides_by_model.setdefault(model_id, {})[str(row["route_id"])] = row

    all_rows: list[ProviderRouteItem] = []
    for model_id in sorted(services.router.routes):
        route = services.router.routes[model_id]
        if not _is_canonical_model(model_id, route):
            continue
        all_rows.extend(
            await _build_routes_for_model(
                services,
                op_store,
                model_id=model_id,
                route=route,
                overrides=overrides_by_model.get(model_id, {}),
            )
        )
    return ListAllProviderRoutesResponse(provider_options=_provider_options(), routes=all_rows)


@router.patch(
    "/routing/provider-route-strategies/{model_id:path}",
    response_model=ListProviderRoutesResponse,
)
async def update_provider_route_strategy(
    model_id: str,
    payload: UpdateProviderRouteStrategyRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListProviderRoutesResponse:
    """Persist and hot-apply the router strategy for one canonical model."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    canonical_model_id, route = _validate_model_router_strategy(
        services,
        model_id,
        payload.strategy,
    )
    await op_store.set_setting(
        _model_strategy_setting_key(canonical_model_id),
        payload.strategy,
        "string",
        admin_id,
    )
    _apply_model_router_strategy(services, canonical_model_id, payload.strategy)
    await log_admin_action(
        op_store,
        admin_id,
        "routing.provider_routes.strategy.update",
        None,
        {
            "model_id": canonical_model_id,
            "strategy": payload.strategy,
        },
    )

    override_rows = await op_store.list_provider_route_configs_for_model(canonical_model_id)
    return ListProviderRoutesResponse(
        model_id=canonical_model_id,
        strategy=_strategy_for_model(services, canonical_model_id),
        provider_options=_provider_options(),
        routes=await _build_routes_for_model(
            services,
            op_store,
            model_id=canonical_model_id,
            route=route,
            overrides=_rows_by_route_id(override_rows),
        ),
    )


@router.post("/routing/provider-route-candidates/{model_id:path}", response_model=ProviderRouteItem)
async def create_provider_route_candidate(
    model_id: str,
    payload: CreateProviderRouteRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ProviderRouteItem:
    """Add a DB-backed runtime provider route candidate for one model."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    candidate = await _prepare_route_candidate(
        services,
        op_store,
        model_id=model_id,
        route_type=payload.route_type,
        upstream_provider=payload.upstream_provider,
        base_url=payload.base_url,
        api_key_id=payload.api_key_id,
        provider_model_id=payload.provider_model_id,
        quota_limit=payload.quota_limit,
        concurrency_limit=payload.concurrency_limit,
        weight=payload.weight,
    )
    await _verify_provider_route(candidate)
    await op_store.upsert_provider_route_candidate(
        model_id,
        candidate.route_id,
        candidate.route_type,
        candidate.upstream_provider,
        candidate.base_url,
        candidate.api_key_id,
        candidate.provider_model_id,
        candidate.quota_limit,
        candidate.concurrency_limit,
        candidate.weight,
        admin_id,
    )
    _install_route_candidate(services, candidate)

    await log_admin_action(
        op_store,
        admin_id,
        "routing.provider_routes.candidate.create",
        None,
        {
            "model_id": model_id,
            "route_id": candidate.route_id,
            "route_type": candidate.route_type,
            "upstream_provider": candidate.upstream_provider,
            "endpoint_id": candidate.endpoint_id,
            "api_key_id": candidate.api_key_id,
            "quota_limit": candidate.quota_limit,
            "concurrency_limit": candidate.concurrency_limit,
            "weight": candidate.weight,
        },
    )

    return await _route_row(
        services,
        op_store,
        model_id=model_id,
        strategy=_strategy_for_model(services, model_id),
        route_id=candidate.route_id,
        adapter=candidate.adapter,
        yaml_weight=candidate.weight,
        endpoint_id=candidate.endpoint_id,
        override_row=None,
    )


@router.put("/routing/provider-routes/{model_route_path:path}", response_model=ProviderRouteItem)
async def update_provider_route(
    model_route_path: str,
    payload: UpdateProviderRouteRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ProviderRouteItem:
    """Update the provider/base URL/key target for one route candidate."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    model_id, route_id, route = _split_model_route_path(services, model_route_path)
    old_entries = _raw_route_entries(route)
    old_adapter, old_weight, old_endpoint_id = old_entries[_route_index_for_id(old_entries, route_id)]
    old_upstream_provider = _upstream_provider(old_adapter)
    route_provider = _route_provider(old_adapter)
    upstream_provider = payload.upstream_provider or payload.provider
    if not upstream_provider:
        raise HTTPException(status_code=422, detail="upstream_provider must not be blank")
    provider_model_id = payload.provider_model_id.strip() if payload.provider_model_id else None
    if payload.provider_model_id is not None and not provider_model_id:
        raise HTTPException(status_code=422, detail="provider_model_id must not be blank")

    update = await _prepare_route_update(
        services,
        op_store,
        model_id=model_id,
        route_id=route_id,
        upstream_provider=upstream_provider,
        base_url=payload.base_url,
        api_key_id=payload.api_key_id,
        provider_model_id_override=provider_model_id,
        quota_limit_override=payload.quota_limit,
    )
    await _verify_provider_route(update)
    await op_store.upsert_provider_route_config(
        model_id,
        route_id,
        update.upstream_provider,
        update.base_url,
        update.api_key_id,
        update.provider_model_id,
        payload.quota_limit,
        admin_id,
    )
    _install_route_update(services, update)

    await log_admin_action(
        op_store,
        admin_id,
        "routing.provider_routes.update",
        None,
        {
            "model_id": model_id,
            "route_id": route_id,
            "route_provider": route_provider,
            "old_upstream_provider": old_upstream_provider,
            "old_endpoint_id": old_endpoint_id,
            "new_upstream_provider": update.upstream_provider,
            "new_endpoint_id": update.endpoint_id,
            "api_key_id": update.api_key_id,
            "quota_limit": payload.quota_limit,
        },
    )

    override_rows = await op_store.list_provider_route_configs_for_model(model_id)
    return await _route_row(
        services,
        op_store,
        model_id=model_id,
        strategy=_strategy_for_model(services, model_id),
        route_id=route_id,
        adapter=update.adapter,
        yaml_weight=float(old_weight),
        endpoint_id=update.endpoint_id,
        override_row=_rows_by_route_id(override_rows).get(route_id),
    )


@router.delete("/routing/provider-routes/{model_route_path:path}", response_model=ProviderRouteItem)
async def delete_provider_route_override(
    model_route_path: str,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ProviderRouteItem:
    """Remove a runtime provider route override and restore the YAML route."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    model_id, route_id, route = _split_model_route_path(services, model_route_path)
    old_entries = _raw_route_entries(route)
    old_adapter, _old_weight, old_endpoint_id = old_entries[
        _route_index_for_id(old_entries, route_id)
    ]
    deleted = await op_store.delete_provider_route_config(model_id, route_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider route override not found")

    restored_adapter, restored_weight, restored_endpoint_id = _install_route_restore(
        services,
        route,
        route_id,
    )

    await log_admin_action(
        op_store,
        admin_id,
        "routing.provider_routes.delete",
        None,
        {
            "model_id": model_id,
            "route_id": route_id,
            "old_upstream_provider": _upstream_provider(old_adapter),
            "old_endpoint_id": old_endpoint_id,
            "restored_upstream_provider": _upstream_provider(restored_adapter),
            "restored_endpoint_id": restored_endpoint_id,
        },
    )

    return await _route_row(
        services,
        op_store,
        model_id=model_id,
        strategy=_strategy_for_model(services, model_id),
        route_id=route_id,
        adapter=restored_adapter,
        yaml_weight=restored_weight,
        endpoint_id=restored_endpoint_id,
        override_row=None,
    )


@router.delete(
    "/routing/provider-route-candidates/{model_route_path:path}",
    response_model=ListProviderRoutesResponse,
)
async def delete_provider_route_candidate(
    model_route_path: str,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListProviderRoutesResponse:
    """Delete a DB-backed runtime provider route candidate."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    model_id, route_id, route = _split_model_route_path(services, model_route_path)
    current_entries = _raw_route_entries(route)
    current_adapter = current_entries[_route_index_for_id(current_entries, route_id)][0]
    if not _is_runtime_candidate(current_adapter):
        raise HTTPException(
            status_code=400,
            detail="Only runtime-added provider routes can be deleted",
        )
    deleted = await op_store.delete_provider_route_candidate(model_id, route_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider route candidate not found")
    await op_store.delete_provider_route_config(model_id, route_id)
    deleted_adapter, _raw_weight, deleted_endpoint_id = _install_route_candidate_delete(
        services,
        route,
        route_id,
    )

    await log_admin_action(
        op_store,
        admin_id,
        "routing.provider_routes.candidate.delete",
        None,
        {
            "model_id": model_id,
            "route_id": route_id,
            "upstream_provider": _upstream_provider(deleted_adapter),
            "endpoint_id": deleted_endpoint_id,
        },
    )

    override_rows = await op_store.list_provider_route_configs_for_model(model_id)
    return ListProviderRoutesResponse(
        model_id=model_id,
        strategy=_strategy_for_model(services, model_id),
        provider_options=_provider_options(),
        routes=await _build_routes_for_model(
            services,
            op_store,
            model_id=model_id,
            route=route,
            overrides=_rows_by_route_id(override_rows),
        ),
    )


async def apply_persisted_provider_route_candidates(services, op_store) -> None:
    """Apply persisted runtime provider route candidates to the in-process router."""
    rows = await op_store.list_all_provider_route_candidates()
    for row in rows:
        model_id = str(row["model_id"])
        route_id = str(row["route_id"])
        try:
            candidate = await _prepare_route_candidate(
                services,
                op_store,
                model_id=model_id,
                route_id=route_id,
                route_type=str(row["route_type"]),
                upstream_provider=str(row["provider"]),
                base_url=str(row["base_url"]),
                api_key_id=row.get("api_key_id"),
                provider_model_id=str(row["provider_model_id"]),
                quota_limit=row.get("quota_limit"),
                concurrency_limit=row.get("concurrency_limit"),
                weight=float(row["weight"]),
            )
            _install_route_candidate(services, candidate)
        except Exception as exc:
            logger.warning(
                "Failed to apply provider route candidate for model=%s route_id=%s: %s",
                model_id,
                route_id,
                exc,
            )


async def apply_persisted_provider_route_configs(services, op_store) -> None:
    """Apply persisted provider route configs to the in-process router at boot."""
    rows = await op_store.list_all_provider_route_configs()
    for row in rows:
        model_id = str(row["model_id"])
        route_id = str(row["route_id"])
        try:
            update = await _prepare_route_update(
                services,
                op_store,
                model_id=model_id,
                route_id=route_id,
                upstream_provider=str(row["provider"]),
                base_url=str(row["base_url"]),
                api_key_id=row.get("api_key_id"),
                provider_model_id_override=str(row["provider_model_id"]),
                quota_limit_override=row.get("quota_limit"),
            )
            _install_route_update(services, update)
        except Exception as exc:
            logger.warning(
                "Failed to apply provider route config for model=%s route_id=%s: %s",
                model_id,
                route_id,
                exc,
            )
            if route_id.startswith("route-"):
                try:
                    await op_store.delete_provider_route_config(model_id, route_id)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Failed to clean up stale provider route config for model=%s "
                        "route_id=%s: %s",
                        model_id,
                        route_id,
                        cleanup_exc,
                    )


async def apply_persisted_model_router_strategy_overrides(services, op_store) -> None:
    """Apply persisted per-model router strategy overrides at boot."""
    rows = await op_store.list_settings()
    for row in rows:
        key = str(row["key"])
        model_id = _model_id_from_strategy_setting_key(key)
        if model_id is None:
            continue
        strategy = str(row["value"])
        try:
            _apply_model_router_strategy(services, model_id, strategy)
        except Exception as exc:
            logger.warning(
                "Failed to apply model router strategy override for model=%s strategy=%s: %s",
                model_id,
                strategy,
                exc,
            )
