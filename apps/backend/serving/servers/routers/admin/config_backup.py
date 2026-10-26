"""Admin configuration backup and restore endpoints."""

from __future__ import annotations

import inspect
import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from serving.adapters import dynamic_keys, provider_registry
from serving.auth.signup_policy import (
    invalidate_allowlist_cache,
    invalidate_signup_policy_cache,
)
from serving.config import app_config
from serving.config.offload_routes import (
    MODEL_OFFLOAD_ROUTE_SETTING_PREFIX,
    OFFLOAD_ROUTE_VALUE_TYPE,
    decode_offload_policy,
)
from serving.config.route_states import RouteState
from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

try:
    from serving.config.routewise_model_settings import (
        MODEL_ROUTEWISE_SETTING_PREFIX,
        _coerce_setting_value,
        _parse_model_setting_key,
        is_retired_routewise_setting_row,
    )
except ImportError:
    MODEL_ROUTEWISE_SETTING_PREFIX = "model_routewise_setting:"
    _coerce_setting_value = None  # type: ignore[assignment]
    _parse_model_setting_key = None  # type: ignore[assignment]

    def is_retired_routewise_setting_row(setting_key: str) -> bool:  # type: ignore[misc]
        """Without RouteWise settings, no stored row is a retired one."""
        return False


from serving.config.settings import VALID_ROLES
from serving.observability import alert_mutes
from serving.observability.alert_snooze import SNOOZE_SETTING_KEY, init_alert_snooze
from serving.schemas_config import (
    ConfigBackupAppConfigRow,
    ConfigBackupDisabledEnvKey,
    ConfigBackupEnvKeyMinRole,
    ConfigBackupModelVisibility,
    ConfigBackupPayload,
    ConfigBackupProviderDefinition,
    ConfigBackupProviderKey,
    ConfigBackupRouteCandidate,
    ConfigBackupRouteCapability,
    ConfigBackupRouteConfig,
    ConfigBackupRouteState,
    ConfigBackupSignupDomain,
    ConfigBackupSiteSetting,
    ConfigBackupWeightOverride,
    ConfigResponse,
    ConfigRestoreResponse,
)
from serving.servers import restart
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_admin_user_id,
    get_operational_store,
    get_services,
    verify_admin_access,
)
from serving.servers.routers.admin.provider_definitions import (
    _config_managed_provider_names,
    _configured_provider_specs,
    config_route_provider_labels,
)
from serving.servers.routers.admin.provider_routes import (
    MODEL_REQUIRED_ROLE_SETTING_PREFIX,
    MODEL_ROUTER_STRATEGIES,
    MODEL_ROUTER_STRATEGY_SETTING_PREFIX,
    _normalize_runtime_model_capabilities,
    _normalize_runtime_model_pricing,
    reconcile_live_routes_from_store,
)
from serving.servers.routers.admin.usage_insights import (
    _SETTING_API_KEY as USAGE_INSIGHTS_API_KEY,
    _SETTING_MODEL as USAGE_INSIGHTS_MODEL,
)
from serving.setup_state import SETUP_CODE_KEY, SETUP_MARKER_KEY

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_EXCLUDED_SITE_SETTING_KEYS: frozenset[str] = frozenset({SETUP_MARKER_KEY, SETUP_CODE_KEY})


def _jsonb_value(raw: Any) -> Any:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


async def _call_store(
    store: Any,
    method_name: str,
    *args: Any,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    """Invoke an async store method when present and awaitable, or return ``default``."""
    if store is None:
        return default
    fn = getattr(store, method_name, None)
    if not callable(fn):
        return default
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return default


def _validate_runtime_setting_string(key: str, raw: str) -> str:
    entry = RUNTIME_SETTINGS_REGISTRY[key]
    expected_type = str(entry["type"])
    if expected_type == "bool":
        normalized = raw.strip().lower()
        if normalized not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError(f"Setting '{key}' expects a boolean value")
        return expected_type
    if expected_type == "int":
        try:
            num: int | float = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"Setting '{key}' expects an integer value") from exc
    elif expected_type == "float":
        try:
            num = float(raw.strip())
        except ValueError as exc:
            raise ValueError(f"Setting '{key}' expects a numeric value") from exc
        if not math.isfinite(num):
            raise ValueError(f"Setting '{key}' expects a finite numeric value")
    else:
        return expected_type

    lo = entry.get("min")
    hi = entry.get("max")
    if lo is not None and num < lo:
        raise ValueError(f"Setting '{key}' value {num} is below min ({lo})")
    if hi is not None and num > hi:
        raise ValueError(f"Setting '{key}' value {num} is above max ({hi})")
    return expected_type


def _validate_site_setting(item: ConfigBackupSiteSetting) -> tuple[str, str, str]:
    key = item.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="site_settings key must not be blank")
    if key in _EXCLUDED_SITE_SETTING_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"site_settings key {key!r} is a first-run setup marker and cannot be restored",
        )
    value = item.value
    value_type = item.value_type or "str"

    if key in RUNTIME_SETTINGS_REGISTRY:
        try:
            expected_type = _validate_runtime_setting_string(key, value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return key, value, expected_type

    if key.startswith(MODEL_ROUTEWISE_SETTING_PREFIX):
        if _parse_model_setting_key is not None and _coerce_setting_value is not None:
            parsed = _parse_model_setting_key(key)
            if parsed is None:
                raise HTTPException(
                    status_code=400, detail=f"invalid RouteWise setting key: {key!r}"
                )
            routewise_key, _model_id = parsed
            try:
                _coerce_setting_value(routewise_key, value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{key}: {exc}") from exc
            return key, value, str(RUNTIME_SETTINGS_REGISTRY[routewise_key]["type"])
        return key, value, value_type

    if key.startswith(MODEL_OFFLOAD_ROUTE_SETTING_PREFIX):
        model_id = key[len(MODEL_OFFLOAD_ROUTE_SETTING_PREFIX) :]
        if not model_id:
            raise HTTPException(status_code=400, detail=f"invalid offload setting key: {key!r}")
        try:
            decode_offload_policy(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{key}: {exc}") from exc
        return key, value, OFFLOAD_ROUTE_VALUE_TYPE

    if key.startswith(MODEL_ROUTER_STRATEGY_SETTING_PREFIX):
        model_id = key[len(MODEL_ROUTER_STRATEGY_SETTING_PREFIX) :]
        if not model_id or value not in MODEL_ROUTER_STRATEGIES:
            raise HTTPException(
                status_code=400,
                detail=f"invalid router strategy setting {key!r}={value!r}",
            )
        return key, value, "str"

    if key.startswith(MODEL_REQUIRED_ROLE_SETTING_PREFIX):
        model_id = key[len(MODEL_REQUIRED_ROLE_SETTING_PREFIX) :]
        if not model_id or value not in VALID_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"invalid runtime model required_role setting {key!r}={value!r}",
            )
        return key, value, "str"

    if key.startswith(alert_mutes.MUTE_SETTING_PREFIX):
        alert_type = alert_mutes.alert_type_from_setting_key(key)
        if not alert_type:
            raise HTTPException(status_code=400, detail=f"invalid alert mute key: {key!r}")
        try:
            alert_mutes.decode_mute_until(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{key}: {exc}") from exc
        return key, value, alert_mutes.MUTE_VALUE_TYPE

    if key in {USAGE_INSIGHTS_API_KEY, USAGE_INSIGHTS_MODEL, SNOOZE_SETTING_KEY}:
        return key, value, value_type

    return key, value, value_type


def _restorable_site_settings(payload: ConfigBackupPayload) -> list[ConfigBackupSiteSetting]:
    """Return the backup's site settings less those of retired RouteWise settings.

    Nothing reads such a row any more, so a restore drops it rather than
    refusing a backup taken while the setting existed.
    """
    return [
        item
        for item in payload.site_settings
        if not is_retired_routewise_setting_row(item.key.strip())
    ]


def _validate_backup_payload(payload: ConfigBackupPayload) -> None:
    if payload.schema_version != 1:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported backup schema_version: {payload.schema_version} (expected 1)",
        )

    seen_settings: set[str] = set()
    for s in _restorable_site_settings(payload):
        key, _val, _vtype = _validate_site_setting(s)
        if key in seen_settings:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate site_settings key in backup: {key!r}",
            )
        seen_settings.add(key)

    seen_defs: set[str] = set()
    for d in payload.provider_definitions:
        provider = d.provider.strip()
        if not provider or not d.display_name.strip() or not d.default_base_url.strip():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid provider_definition for {d.provider!r}",
            )
        if provider in seen_defs:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate provider_definition in backup: {provider!r}",
            )
        seen_defs.add(provider)

    seen_key_ids: set[str] = set()
    for k in payload.provider_keys:
        if not k.id.strip() or not k.provider.strip() or not k.api_key:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid provider_key entry for provider {k.provider!r}",
            )
        if k.min_role not in VALID_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid min_role {k.min_role!r} on provider_key {k.id!r}",
            )
        if k.id in seen_key_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate provider_key id in backup: {k.id!r}",
            )
        seen_key_ids.add(k.id)

    for env_res in payload.provider_env_key_min_roles:
        if env_res.min_role not in VALID_ROLES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid min_role {env_res.min_role!r} on provider_env_key_min_roles "
                    f"for {env_res.provider!r}"
                ),
            )

    for cand in payload.provider_route_candidates:
        if cand.route_type not in {"quota", "concurrency", "on_demand"}:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid route_type {cand.route_type!r} on route candidate {cand.route_id!r}"
                ),
            )
        if not math.isfinite(cand.weight) or cand.weight <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid weight {cand.weight!r} on route candidate {cand.route_id!r}",
            )
        if cand.pricing is not None:
            try:
                _normalize_runtime_model_pricing(cand.pricing)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid pricing on route candidate {cand.route_id!r}: {exc}",
                ) from exc
        if cand.capabilities is not None:
            try:
                _normalize_runtime_model_capabilities(cand.capabilities)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid capabilities on route candidate {cand.route_id!r}: {exc}",
                ) from exc

    for cap in payload.provider_route_capabilities:
        try:
            normalized = _normalize_runtime_model_capabilities(cap.capabilities)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid capabilities on {cap.model_id}/{cap.route_id}: {exc}",
            ) from exc
        if normalized is None:
            raise HTTPException(
                status_code=400,
                detail=f"Empty capabilities on {cap.model_id}/{cap.route_id}",
            )

    for w in payload.provider_weight_overrides:
        if not math.isfinite(w.weight) or w.weight < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid weight override for {w.model_id}/{w.endpoint_id}: {w.weight}",
            )

    for vis in payload.model_visibility_overrides:
        if vis.required_role not in VALID_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid required_role {vis.required_role!r} for model {vis.model_id!r}",
            )

    for model_id, max_concurrent in payload.batch_model_limits.items():
        # 0 is a cap like any other: it pauses the model's batch work.
        if not model_id.strip() or int(max_concurrent) < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid batch_model_limit for {model_id!r}: {max_concurrent}",
            )


@router.get("/admin/config/backup", response_model=ConfigBackupPayload)
async def export_config_backup(
    response: Response,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ConfigBackupPayload:
    """Export all administrator-configurable state as a portable JSON bundle."""
    response.headers["Cache-Control"] = "no-store"

    raw_app_config = await app_config.export_rows()
    app_config_items = [ConfigBackupAppConfigRow(**row) for row in raw_app_config]

    site_settings: list[ConfigBackupSiteSetting] = []
    for row in await _call_store(op_store, "list_settings", default=[]):
        key = str(row["key"])
        if key in _EXCLUDED_SITE_SETTING_KEYS or is_retired_routewise_setting_row(key):
            continue
        site_settings.append(
            ConfigBackupSiteSetting(
                key=key,
                value=str(row["value"]),
                value_type=str(row.get("value_type") or "str"),
            )
        )
    site_settings.sort(key=lambda item: item.key)

    provider_definitions: list[ConfigBackupProviderDefinition] = []
    for defn in await _call_store(op_store, "list_provider_definitions", default=[]):
        provider_definitions.append(
            ConfigBackupProviderDefinition(
                provider=defn.provider,
                display_name=defn.display_name,
                adapter_kind=defn.adapter_kind,
                default_base_url=defn.default_base_url,
                status="disabled" if defn.status == "disabled" else "active",
            )
        )
    provider_definitions.sort(key=lambda item: item.provider)

    key_rows = await _call_store(op_store, "list_provider_keys", default=[])
    raw_values_by_provider: dict[str, dict[str, str]] = {}
    for row in key_rows:
        if row.provider not in raw_values_by_provider:
            raw_values_by_provider[row.provider] = await _call_store(
                op_store,
                "list_provider_key_values",
                row.provider,
                default={},
            )

    provider_keys: list[ConfigBackupProviderKey] = []
    for row in key_rows:
        raw_key = raw_values_by_provider.get(row.provider, {}).get(row.id)
        if raw_key is None:
            full = await _call_store(op_store, "get_provider_key_full", row.id, default=None)
            if full is not None:
                raw_key = full[1]
        if raw_key is None:
            continue
        provider_keys.append(
            ConfigBackupProviderKey(
                id=row.id,
                provider=row.provider,
                api_key=raw_key,
                label=row.label,
                status="disabled" if row.status == "disabled" else "active",
                min_role=row.min_role or "free",
            )
        )
    provider_keys.sort(key=lambda item: (item.provider, item.id))

    disabled_env_rows = await _call_store(
        op_store,
        "list_all_disabled_provider_env_keys",
        default=[],
    )
    disabled_provider_env_keys = [
        ConfigBackupDisabledEnvKey(
            provider=str(provider),
            key_hash=str(key_hash),
            key_prefix=str(key_prefix),
        )
        for provider, key_hash, key_prefix in disabled_env_rows
    ]
    disabled_provider_env_keys.sort(key=lambda item: (item.provider, item.key_hash))

    env_role_rows = await _call_store(
        op_store,
        "list_all_provider_env_key_reservations",
        default=[],
    )
    provider_env_key_min_roles = [
        ConfigBackupEnvKeyMinRole(
            provider=str(provider),
            key_hash=str(key_hash),
            key_prefix=str(key_prefix),
            min_role=str(min_role),
        )
        for provider, key_hash, key_prefix, min_role in env_role_rows
    ]
    provider_env_key_min_roles.sort(key=lambda item: (item.provider, item.key_hash))

    disabled_providers_raw = await _call_store(op_store, "list_disabled_providers", default=[])
    disabled_providers = sorted(
        str(row["provider"] if isinstance(row, dict) else row) for row in disabled_providers_raw
    )

    route_states_raw = await _call_store(
        op_store,
        "list_all_provider_route_states",
        default=[],
    )
    provider_route_states = [
        ConfigBackupRouteState(
            model_id=str(row["model_id"]),
            route_id=str(row["route_id"]),
            disabled=bool(row.get("disabled", False)),
            removed=bool(row.get("removed", False)),
        )
        for row in route_states_raw
        if row.get("disabled") or row.get("removed")
    ]
    provider_route_states.sort(key=lambda item: (item.model_id, item.route_id))

    route_candidates_raw = await _call_store(
        op_store,
        "list_all_provider_route_candidates",
        default=[],
    )
    provider_route_candidates = [
        ConfigBackupRouteCandidate(
            model_id=str(row["model_id"]),
            route_id=str(row["route_id"]),
            route_type=str(row["route_type"]),
            provider=str(row["provider"]),
            openrouter_sort=row.get("openrouter_sort"),
            deployment_type=str(row.get("deployment_type") or "auto"),
            base_url=str(row["base_url"]),
            api_key_id=row.get("api_key_id"),
            provider_model_id=str(row["provider_model_id"]),
            quota_limit=row.get("quota_limit"),
            concurrency_limit=row.get("concurrency_limit"),
            weight=float(row.get("weight", 1.0)),
            pricing=_jsonb_value(row.get("pricing")),
            capabilities=_jsonb_value(row.get("capabilities")),
            endpoint_id=row.get("endpoint_id"),
        )
        for row in route_candidates_raw
    ]
    provider_route_candidates.sort(key=lambda item: (item.model_id, item.route_id))

    route_configs_raw = await _call_store(
        op_store,
        "list_all_provider_route_configs",
        default=[],
    )
    provider_route_configs = [
        ConfigBackupRouteConfig(
            model_id=str(row["model_id"]),
            route_id=str(row["route_id"]),
            provider=str(row["provider"]),
            openrouter_sort=row.get("openrouter_sort"),
            deployment_type=row.get("deployment_type"),
            base_url=str(row["base_url"]),
            api_key_id=row.get("api_key_id"),
            provider_model_id=str(row["provider_model_id"]),
            quota_limit=row.get("quota_limit"),
            concurrency_limit=row.get("concurrency_limit"),
        )
        for row in route_configs_raw
    ]
    provider_route_configs.sort(key=lambda item: (item.model_id, item.route_id))

    route_caps_raw = await _call_store(
        op_store,
        "list_all_route_capability_overrides",
        default=[],
    )
    provider_route_capabilities = [
        ConfigBackupRouteCapability(
            model_id=str(row["model_id"]),
            route_id=str(row["route_id"]),
            capabilities=_jsonb_value(row["capabilities"]),
        )
        for row in route_caps_raw
    ]
    provider_route_capabilities.sort(key=lambda item: (item.model_id, item.route_id))

    weight_overrides_raw = await _call_store(
        op_store,
        "list_all_weight_overrides",
        default=[],
    )
    provider_weight_overrides = [
        ConfigBackupWeightOverride(
            model_id=str(row["model_id"]),
            endpoint_id=str(row["endpoint_id"]),
            weight=float(row["weight"]),
        )
        for row in weight_overrides_raw
    ]
    provider_weight_overrides.sort(key=lambda item: (item.model_id, item.endpoint_id))

    visibility_raw = await _call_store(
        op_store,
        "list_model_visibility_overrides",
        default=[],
    )
    model_visibility_overrides = [
        ConfigBackupModelVisibility(
            model_id=str(row["model_id"]),
            required_role=str(row["required_role"]),
        )
        for row in visibility_raw
    ]
    model_visibility_overrides.sort(key=lambda item: item.model_id)

    exemptions_raw = await _call_store(
        op_store,
        "list_model_concurrency_exemptions",
        default=[],
    )
    model_concurrency_exemptions = sorted(str(row["model_id"]) for row in exemptions_raw)

    batch_store = getattr(services, "batch_store", None)
    batch_model_limits_raw = await _call_store(batch_store, "list_model_limits", default={})
    batch_model_limits = {str(k): int(v) for k, v in sorted((batch_model_limits_raw or {}).items())}

    domains_raw = await _call_store(op_store, "list_signup_allowed_domains", default=[])
    signup_allowed_domains = [
        ConfigBackupSignupDomain(
            domain=str(row["domain"]),
            is_wildcard=bool(row.get("is_wildcard", False)),
        )
        for row in domains_raw
    ]
    signup_allowed_domains.sort(key=lambda item: item.domain)

    await log_admin_action(
        op_store,
        admin_id,
        "config.backup",
        None,
        {
            "app_config": len(app_config_items),
            "site_settings": len(site_settings),
            "provider_definitions": len(provider_definitions),
            "provider_keys": len(provider_keys),
            "provider_route_candidates": len(provider_route_candidates),
            "provider_route_configs": len(provider_route_configs),
        },
    )

    return ConfigBackupPayload(
        **{
            "schema_version": 1,
            "exported_at": datetime.now(UTC),
            "app_config": app_config_items,
            "site_settings": site_settings,
            "provider_definitions": provider_definitions,
            "provider_keys": provider_keys,
            "disabled_provider_env_keys": disabled_provider_env_keys,
            "provider_env_key_min_roles": provider_env_key_min_roles,
            "disabled_providers": disabled_providers,
            "provider_route_states": provider_route_states,
            "provider_route_candidates": provider_route_candidates,
            "provider_route_configs": provider_route_configs,
            "provider_route_capabilities": provider_route_capabilities,
            "provider_weight_overrides": provider_weight_overrides,
            "model_visibility_overrides": model_visibility_overrides,
            "model_concurrency_exemptions": model_concurrency_exemptions,
            "batch_model_limits": batch_model_limits,
            "signup_allowed_domains": signup_allowed_domains,
        }
    )


async def _apply_operational_and_batch_stores(
    op_store: Any,
    batch_store: Any,
    payload: ConfigBackupPayload,
    admin_id: str,
    admin_user_id: str | None = None,
) -> None:
    """Replace operational and batch store configuration domains to match ``payload``."""
    if op_store is not None:
        # 1. site_settings (preserve setup_completed_at and setup_code)
        current_settings = await _call_store(op_store, "list_settings", default=[])
        # A retired RouteWise setting's stored row is not in the target either,
        # so a restore also clears one left behind.
        target_settings = {
            key: (val, vtype)
            for key, val, vtype in (
                _validate_site_setting(s) for s in _restorable_site_settings(payload)
            )
        }
        for row in current_settings:
            existing_key = str(row["key"])
            if existing_key in _EXCLUDED_SITE_SETTING_KEYS:
                continue
            if existing_key not in target_settings:
                await _call_store(op_store, "delete_setting", existing_key)
        for key, (val, vtype) in target_settings.items():
            await _call_store(op_store, "set_setting", key, val, vtype, admin_id)

        # 2. provider_definitions
        current_defs = await _call_store(op_store, "list_provider_definitions", default=[])
        target_defs = {d.provider.strip(): d for d in payload.provider_definitions}
        for defn in current_defs:
            if defn.provider not in target_defs:
                await _call_store(op_store, "delete_provider_definition", defn.provider)
        for provider, d in target_defs.items():
            await _call_store(
                op_store,
                "upsert_provider_definition",
                provider=provider,
                display_name=d.display_name.strip(),
                adapter_kind=d.adapter_kind.strip() or "openai_compat",
                default_base_url=d.default_base_url.strip(),
                created_by=admin_id,
                status=d.status,
            )

        # 3. provider_api_keys
        current_keys = await _call_store(op_store, "list_provider_keys", default=[])
        for existing in current_keys:
            await _call_store(op_store, "delete_provider_key", existing.id)
        for k in payload.provider_keys:
            await _call_store(
                op_store,
                "add_provider_key",
                provider=k.provider.strip(),
                api_key=k.api_key,
                label=k.label,
                created_by=admin_id,
                key_id=k.id.strip(),
                min_role=k.min_role,
            )
            if k.status == "disabled":
                await _call_store(op_store, "set_provider_key_status", k.id.strip(), "disabled")

        # 4. disabled_provider_env_keys
        current_disabled_env = await _call_store(
            op_store,
            "list_all_disabled_provider_env_keys",
            default=[],
        )
        target_disabled_env = {
            (item.provider.strip(), item.key_hash.strip()): item
            for item in payload.disabled_provider_env_keys
        }
        for provider, key_hash, _prefix in current_disabled_env:
            if (str(provider), str(key_hash)) not in target_disabled_env:
                await _call_store(
                    op_store,
                    "enable_provider_env_key",
                    str(provider),
                    str(key_hash),
                )
        for (provider, key_hash), item in target_disabled_env.items():
            await _call_store(
                op_store,
                "disable_provider_env_key",
                provider=provider,
                key_hash=key_hash,
                key_prefix=item.key_prefix,
                disabled_by=admin_id,
            )

        # 5. provider_env_key_min_roles
        current_env_roles = await _call_store(
            op_store,
            "list_all_provider_env_key_reservations",
            default=[],
        )
        target_env_roles = {
            (item.provider.strip(), item.key_hash.strip()): item
            for item in payload.provider_env_key_min_roles
        }
        for provider, key_hash, prefix, _min_role in current_env_roles:
            if (str(provider), str(key_hash)) not in target_env_roles:
                await _call_store(
                    op_store,
                    "set_provider_env_key_min_role",
                    provider=str(provider),
                    key_hash=str(key_hash),
                    key_prefix=str(prefix),
                    min_role="free",
                    updated_by=admin_id,
                )
        for (provider, key_hash), item in target_env_roles.items():
            await _call_store(
                op_store,
                "set_provider_env_key_min_role",
                provider=provider,
                key_hash=key_hash,
                key_prefix=item.key_prefix,
                min_role=item.min_role,
                updated_by=admin_id,
            )

        # 6. disabled_providers
        current_disabled_providers = {
            str(row["provider"] if isinstance(row, dict) else row)
            for row in await _call_store(op_store, "list_disabled_providers", default=[])
        }
        target_disabled_providers = {p.strip() for p in payload.disabled_providers if p.strip()}
        for p in current_disabled_providers - target_disabled_providers:
            await _call_store(op_store, "clear_provider_disabled", str(p))
        for p in target_disabled_providers - current_disabled_providers:
            await _call_store(op_store, "set_provider_disabled", str(p), admin_id)

        # 7. provider_route_states
        current_route_state_rows = await _call_store(
            op_store,
            "list_all_provider_route_states",
            default=[],
        )
        current_route_states = {
            (str(row["model_id"]), str(row["route_id"])): RouteState(
                disabled=bool(row.get("disabled", False)),
                removed=bool(row.get("removed", False)),
            )
            for row in current_route_state_rows
        }
        target_route_states = {
            (s.model_id, s.route_id): RouteState(disabled=s.disabled, removed=s.removed)
            for s in payload.provider_route_states
            if s.disabled or s.removed
        }
        for key in current_route_states:
            if key not in target_route_states:
                await _call_store(
                    op_store,
                    "set_provider_route_state",
                    key[0],
                    key[1],
                    disabled=False,
                    removed=False,
                    updated_by=admin_id,
                )
        for (model_id, route_id), new_state in target_route_states.items():
            await _call_store(
                op_store,
                "set_provider_route_state",
                model_id,
                route_id,
                disabled=new_state.disabled,
                removed=new_state.removed,
                updated_by=admin_id,
            )

        # 8. provider_route_candidates
        current_candidates = await _call_store(
            op_store,
            "list_all_provider_route_candidates",
            default=[],
        )
        for row in current_candidates:
            await _call_store(
                op_store,
                "delete_provider_route_candidate",
                str(row["model_id"]),
                str(row["route_id"]),
            )
        for cand in payload.provider_route_candidates:
            await _call_store(
                op_store,
                "upsert_provider_route_candidate",
                cand.model_id,
                cand.route_id,
                cand.route_type,
                cand.provider,
                cand.openrouter_sort,
                cand.base_url,
                cand.api_key_id,
                cand.provider_model_id,
                cand.quota_limit,
                cand.concurrency_limit,
                cand.weight,
                cand.pricing,
                cand.capabilities,
                admin_id,
                deployment_type=cand.deployment_type or "auto",
                endpoint_id=cand.endpoint_id,
            )

        # 9. provider_route_configs
        current_configs = await _call_store(
            op_store,
            "list_all_provider_route_configs",
            default=[],
        )
        target_configs = {(c.model_id, c.route_id): c for c in payload.provider_route_configs}
        for row in current_configs:
            key = (str(row["model_id"]), str(row["route_id"]))
            if key not in target_configs:
                await _call_store(op_store, "delete_provider_route_config", key[0], key[1])
        for cfg in payload.provider_route_configs:
            await _call_store(
                op_store,
                "upsert_provider_route_config",
                cfg.model_id,
                cfg.route_id,
                cfg.provider,
                cfg.openrouter_sort,
                cfg.base_url,
                cfg.api_key_id,
                cfg.provider_model_id,
                cfg.quota_limit,
                cfg.concurrency_limit,
                admin_id,
                deployment_type=cfg.deployment_type,
            )

        # 10. provider_route_capabilities
        current_caps = await _call_store(
            op_store,
            "list_all_route_capability_overrides",
            default=[],
        )
        target_caps = {(c.model_id, c.route_id): c for c in payload.provider_route_capabilities}
        for row in current_caps:
            key = (str(row["model_id"]), str(row["route_id"]))
            if key not in target_caps:
                await _call_store(op_store, "delete_route_capability_override", key[0], key[1])
        for cap in payload.provider_route_capabilities:
            normalized_cap = _normalize_runtime_model_capabilities(cap.capabilities)
            if normalized_cap is not None:
                await _call_store(
                    op_store,
                    "upsert_route_capability_override",
                    cap.model_id,
                    cap.route_id,
                    normalized_cap,
                    admin_id,
                )

        # 11. provider_weight_overrides
        current_weights = await _call_store(op_store, "list_all_weight_overrides", default=[])
        target_weights = {(w.model_id, w.endpoint_id): w for w in payload.provider_weight_overrides}
        for row in current_weights:
            key = (str(row["model_id"]), str(row["endpoint_id"]))
            if key not in target_weights:
                await _call_store(op_store, "delete_weight_override", key[0], key[1])
        for w in payload.provider_weight_overrides:
            await _call_store(
                op_store,
                "upsert_weight_override",
                w.model_id,
                w.endpoint_id,
                w.weight,
                admin_id,
            )

        # 12. model_visibility_overrides
        current_vis = await _call_store(
            op_store,
            "list_model_visibility_overrides",
            default=[],
        )
        target_vis = {v.model_id: v for v in payload.model_visibility_overrides}
        for row in current_vis:
            mid = str(row["model_id"])
            if mid not in target_vis:
                await _call_store(op_store, "delete_model_visibility_override", mid)
        for v in payload.model_visibility_overrides:
            await _call_store(
                op_store,
                "set_model_visibility_override",
                v.model_id,
                v.required_role,
                admin_id,
            )

        # 13. model_concurrency_exemptions
        current_exemptions = {
            str(row["model_id"])
            for row in await _call_store(
                op_store,
                "list_model_concurrency_exemptions",
                default=[],
            )
        }
        target_exemptions = {m.strip() for m in payload.model_concurrency_exemptions if m.strip()}
        for mid in current_exemptions - target_exemptions:
            await _call_store(op_store, "delete_model_concurrency_exemption", mid)
        for mid in target_exemptions:
            await _call_store(op_store, "set_model_concurrency_exemption", mid, admin_id)

        # 14. signup_allowed_domains, keyed as the table is: (domain, is_wildcard).
        # ``created_by`` references users.id, so it takes the admin's account id
        # (None for ADMIN_TOKEN), not the ``admin_id`` display identity.
        current_domains = {
            (str(row["domain"]), bool(row.get("is_wildcard", False)))
            for row in await _call_store(op_store, "list_signup_allowed_domains", default=[])
        }
        target_domains = {(d.domain.strip(), d.is_wildcard) for d in payload.signup_allowed_domains}
        for dom, is_wildcard in sorted(current_domains - target_domains):
            await _call_store(
                op_store, "remove_signup_allowed_domain", domain=dom, is_wildcard=is_wildcard
            )
        for dom, is_wildcard in sorted(target_domains - current_domains):
            await _call_store(
                op_store,
                "add_signup_allowed_domain",
                domain=dom,
                is_wildcard=is_wildcard,
                created_by=admin_user_id,
            )

    if batch_store is not None:
        current_limits = await _call_store(batch_store, "list_model_limits", default={})
        for mid in set(current_limits) - set(payload.batch_model_limits):
            await _call_store(batch_store, "delete_model_limit", str(mid))
        for mid, limit in payload.batch_model_limits.items():
            await _call_store(
                batch_store,
                "set_model_limit",
                mid,
                int(limit),
                updated_by=admin_id,
            )


def _has_async_method(store: Any, method_name: str) -> bool:
    if store is None:
        return False
    return inspect.iscoroutinefunction(getattr(store, method_name, None))


async def _refresh_live_caches(services: Any, op_store: Any) -> None:
    """Refresh in-memory resolvers, key pools, and route tables after a restore."""
    rt = getattr(services, "runtime_settings", None)
    if rt is not None:
        try:
            await _call_store(rt, "refresh")
        except Exception:
            logger.debug("RuntimeSettings refresh skipped or failed after restore", exc_info=True)

    invalidate_signup_policy_cache()
    invalidate_allowlist_cache()

    if op_store is not None:
        try:
            alert_mutes.init_alert_mutes(op_store)
        except Exception:
            logger.warning("Failed to refresh alert mutes after restore", exc_info=True)

        try:
            init_alert_snooze(op_store)
        except Exception:
            logger.warning("Failed to refresh alert snooze after restore", exc_info=True)

        if _has_async_method(op_store, "list_provider_definitions"):
            try:
                config_specs = _configured_provider_specs()
                await provider_registry.apply_provider_definitions_at_boot(
                    op_store,
                    reserved_providers=_config_managed_provider_names(config_specs),
                    config_label_providers=config_route_provider_labels(config_specs),
                )
            except Exception:
                logger.warning(
                    "Failed to refresh provider definitions after restore", exc_info=True
                )

        if _has_async_method(op_store, "list_provider_keys"):
            try:
                await dynamic_keys.apply_db_keys_at_boot(op_store)
            except Exception:
                logger.warning(
                    "Failed to refresh dynamic provider keys after restore", exc_info=True
                )

    disabled_resolver = getattr(services, "disabled_provider_resolver", None)
    if disabled_resolver is not None:
        await _call_store(disabled_resolver, "load_all")

    route_state_resolver = getattr(services, "route_state_resolver", None)
    if route_state_resolver is not None:
        await _call_store(route_state_resolver, "load_all")

    weight_resolver = getattr(services, "weight_override_resolver", None)
    if weight_resolver is not None:
        await _call_store(weight_resolver, "load_all")

    routewise_resolver = getattr(services, "routewise_settings_resolver", None)
    if routewise_resolver is not None:
        await _call_store(routewise_resolver, "load_all")

    offload_resolver = getattr(services, "offload_route_resolver", None)
    if offload_resolver is not None:
        await _call_store(offload_resolver, "load_all")

    if op_store is not None and getattr(services, "router", None) is not None:
        try:
            await reconcile_live_routes_from_store(services, op_store)
        except Exception:
            logger.warning("Failed to reconcile live routes after restore", exc_info=True)
        if _has_async_method(op_store, "list_provider_key_min_roles"):
            try:
                await dynamic_keys.load_min_role_declarations(op_store)
            except Exception:
                logger.warning(
                    "Failed to reload provider key tier reservations after restore",
                    exc_info=True,
                )

    vis_resolver = getattr(services, "model_visibility_resolver", None)
    if vis_resolver is not None and callable(getattr(vis_resolver, "invalidate_cache", None)):
        vis_resolver.invalidate_cache()

    conc_resolver = getattr(services, "model_concurrency_resolver", None)
    if conc_resolver is not None and callable(getattr(conc_resolver, "invalidate_cache", None)):
        conc_resolver.invalidate_cache()

    batch_limits = getattr(services, "batch_model_limits", None)
    if batch_limits is not None and callable(getattr(batch_limits, "invalidate", None)):
        batch_limits.invalidate()

    app_config.refresh_health()


@router.post("/admin/config/restore", response_model=ConfigRestoreResponse)
async def restore_config_backup(
    payload: ConfigBackupPayload,
    request: Request,
    response: Response,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ConfigRestoreResponse:
    """Restore administrator-configurable state from a backup bundle."""
    response.headers["Cache-Control"] = "no-store"

    _validate_backup_payload(payload)

    batch_store = getattr(services, "batch_store", None)
    app_config_dicts = [row.model_dump() for row in payload.app_config]

    try:
        preserved = await app_config.restore_rows(
            app_config_dicts,
            updated_by=admin_id,
            before_commit=lambda: _apply_operational_and_batch_stores(
                op_store,
                batch_store,
                payload,
                admin_id,
                get_admin_user_id(request),
            ),
        )
    except app_config.ConfigUpdateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    await _refresh_live_caches(services, op_store)

    await log_admin_action(
        op_store,
        admin_id,
        "config.restore",
        None,
        {
            "app_config": len(payload.app_config),
            "site_settings": len(payload.site_settings),
            "provider_definitions": len(payload.provider_definitions),
            "provider_keys": len(payload.provider_keys),
            "provider_route_candidates": len(payload.provider_route_candidates),
            "provider_route_configs": len(payload.provider_route_configs),
            "immutable_keys_preserved": preserved,
        },
    )

    return ConfigRestoreResponse(
        config=ConfigResponse(
            **app_config.describe(offered_only=True),
            restart_supported=restart.restart_supported(),
        ),
        immutable_keys_preserved=preserved,
    )
