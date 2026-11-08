"""Admin configuration backup and restore endpoints."""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import math
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg
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


from serving.audit import AuditActor, AuditTarget
from serving.config.settings import VALID_ROLES
from serving.observability import alert_mutes
from serving.observability.alert_snooze import SNOOZE_SETTING_KEY, init_alert_snooze
from serving.schemas_config import (
    ConfigBackupAppConfigRow,
    ConfigBackupDisabledEnvKey,
    ConfigBackupEnvKeyMinRole,
    ConfigBackupExportRequest,
    ConfigBackupModelVisibility,
    ConfigBackupPayload,
    ConfigBackupProviderDefinition,
    ConfigBackupProviderKey,
    ConfigBackupRouteBatchPolicy,
    ConfigBackupRouteCandidate,
    ConfigBackupRouteCapability,
    ConfigBackupRouteConfig,
    ConfigBackupRouteState,
    ConfigBackupSignupDomain,
    ConfigBackupSiteSetting,
    ConfigBackupWeightOverride,
    ConfigResponse,
    ConfigRestorePreview,
    ConfigRestoreRequest,
    ConfigRestoreResponse,
)
from serving.servers import restart
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_admin_audit_actor,
    get_admin_user_id,
    get_operational_store,
    get_services,
    verify_admin_access,
)
from serving.servers.request_body import json_body_after, json_body_schema
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
from serving.servers.routers.admin.reauthentication import confirm_administrator
from serving.servers.routers.admin.usage_insights import (
    _SETTING_API_KEY as USAGE_INSIGHTS_API_KEY,
    _SETTING_MODEL as USAGE_INSIGHTS_MODEL,
)
from serving.setup_state import SETUP_CODE_KEY, SETUP_MARKER_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_EXCLUDED_SITE_SETTING_KEYS: frozenset[str] = frozenset({SETUP_MARKER_KEY, SETUP_CODE_KEY})

#: The one secret ``site_settings`` row; its own admin API shows only a hint.
_SECRET_SITE_SETTING_KEYS: frozenset[str] = frozenset({USAGE_INSIGHTS_API_KEY})

# What a restore changes only once the administrator confirms the change and
# gives the account's password: what signs and encrypts, who administers and
# who may sign in or use what, which networks are trusted, where the gateway
# sends requests, credentials, mail and users, and whether it reports trouble.
# For ``app_config`` that is :func:`app_config.security_relevant`; for
# ``site_settings``, these keys and the keys under these prefixes: a runtime
# model's required role, and an alert type's mute. The plan also lists a
# change to who may use a model or a provider key, and to where and how a
# route sends requests and keys (:func:`_restore_plan`).
_SECURITY_SITE_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "user_auth_enabled",
        "signup_enabled",
        "signup_require_email_verification",
        "log_full_payload",
        SNOOZE_SETTING_KEY,
        *_SECRET_SITE_SETTING_KEYS,
    }
)
_SECURITY_SITE_SETTING_PREFIXES: tuple[str, ...] = (
    MODEL_REQUIRED_ROLE_SETTING_PREFIX,
    alert_mutes.MUTE_SETTING_PREFIX,
)

#: The longest a restored alert snooze may run: what ``POST /admin/alerts/snooze``
#: allows, and a minute for clocks that disagree. Past it a snooze silences the
#: gateway's alerts for good (``inf`` never ends).
_MAX_RESTORED_SNOOZE_SECONDS = 7 * 24 * 60 * 60 + 60


def _security_relevant_site_setting(key: str) -> bool:
    """Whether changing ``site_settings`` *key* from a backup needs confirmation and the password."""
    return key in _SECURITY_SITE_SETTING_KEYS or key.startswith(_SECURITY_SITE_SETTING_PREFIXES)


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


def _validate_site_setting(item: ConfigBackupSiteSetting) -> tuple[str, str | None, str]:
    """Return the row a restore writes for *item*; its value is ``None`` when withheld.

    Only a secret row may come without a value, from a backup taken without
    secret values: the restore then keeps this deployment's value.
    """
    key = item.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="site_settings key must not be blank")
    if key in _EXCLUDED_SITE_SETTING_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"site_settings key {key!r} is a first-run setup marker and cannot be restored",
        )
    value_type = item.value_type or "str"
    if item.value is None:
        if key not in _SECRET_SITE_SETTING_KEYS:
            raise HTTPException(status_code=400, detail=f"site_settings key {key!r} needs a value")
        return key, None, value_type
    value = item.value

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

    if key == SNOOZE_SETTING_KEY:
        # Read as a float deadline on every alert (alert_snooze): "inf", or a
        # deadline centuries away, would silence every alert for good.
        try:
            until = float(value)
        except ValueError:
            until = math.nan
        if not math.isfinite(until) or until < 0:
            raise HTTPException(
                status_code=400, detail=f"{key}: must be a deadline in epoch seconds"
            )
        if until > time.time() + _MAX_RESTORED_SNOOZE_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"{key}: an alert snooze ends within 7 days of when it was set",
            )
        return key, value, "float"

    if key in {USAGE_INSIGHTS_API_KEY, USAGE_INSIGHTS_MODEL}:
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
        # ``None`` is a value withheld from the backup; an empty key is no key.
        if not k.id.strip() or not k.provider.strip() or k.api_key == "":
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


async def _build_backup(
    services: Any, op_store: Any, *, include_secret_values: bool
) -> ConfigBackupPayload:
    """Read every administrator-configurable domain into a bundle.

    Without *include_secret_values*, each secret -- a secret ``app_config``
    row, the usage-insights API key, every provider key -- is named with no
    value: ``null``, and ``is_set`` on the ``app_config`` rows.
    """
    raw_app_config = await app_config.export_rows(include_secret_values=include_secret_values)
    app_config_items = [ConfigBackupAppConfigRow(**row) for row in raw_app_config]

    site_settings: list[ConfigBackupSiteSetting] = []
    for row in await _call_store(op_store, "list_settings", default=[]):
        key = str(row["key"])
        if key in _EXCLUDED_SITE_SETTING_KEYS or is_retired_routewise_setting_row(key):
            continue
        withheld = key in _SECRET_SITE_SETTING_KEYS and not include_secret_values
        site_settings.append(
            ConfigBackupSiteSetting(
                key=key,
                value=None if withheld else str(row["value"]),
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
    if include_secret_values:
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
        raw_key: str | None = None
        if include_secret_values:
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

    batch_policies_raw = await _call_store(
        op_store,
        "list_all_provider_route_batch_policies",
        default=[],
    )
    provider_route_batch_policies = sorted(
        (
            ConfigBackupRouteBatchPolicy(
                model_id=str(row["model_id"]),
                route_id=str(row["route_id"]),
                batch_allowed=bool(row["batch_allowed"]),
            )
            for row in batch_policies_raw
        ),
        key=lambda item: (item.model_id, item.route_id),
    )

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

    return ConfigBackupPayload(
        **{
            "schema_version": 1,
            "exported_at": datetime.now(UTC),
            "secrets_included": include_secret_values,
            "app_config": app_config_items,
            "site_settings": site_settings,
            "provider_definitions": provider_definitions,
            "provider_keys": provider_keys,
            "disabled_provider_env_keys": disabled_provider_env_keys,
            "provider_env_key_min_roles": provider_env_key_min_roles,
            "disabled_providers": disabled_providers,
            "provider_route_states": provider_route_states,
            "provider_route_batch_policies": provider_route_batch_policies,
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


#: Secret values handed out, or the deployment's security settings replaced,
#: outlive any session: both ask for the administrator's password again.
_confirm_administrator = confirm_administrator


async def _export(
    response: Response,
    actor: AuditActor,
    services: Any,
    op_store: Any,
    *,
    include_secret_values: bool,
) -> ConfigBackupPayload:
    response.headers["Cache-Control"] = "no-store"
    payload = await _build_backup(services, op_store, include_secret_values=include_secret_values)
    await log_admin_action(
        op_store,
        action="config.backup",
        actor=actor,
        target=AuditTarget("config", "backup"),
        details={
            "secrets_included": include_secret_values,
            "app_config": len(payload.app_config),
            "site_settings": len(payload.site_settings),
            "provider_definitions": len(payload.provider_definitions),
            "provider_keys": len(payload.provider_keys),
            "provider_route_candidates": len(payload.provider_route_candidates),
            "provider_route_configs": len(payload.provider_route_configs),
        },
    )
    return payload


@router.get("/admin/config/backup", response_model=ConfigBackupPayload)
async def export_config_backup(
    response: Response,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ConfigBackupPayload:
    """Export all administrator-configurable state, with no secret values.

    Every secret is named, and its value is ``null``: an ``app_config`` row
    says ``is_set``, a provider key keeps its id, provider and label. Restoring
    the bundle keeps this deployment's secrets. ``POST`` exports the values
    too, after the administrator's password.
    """
    return await _export(response, actor, services, op_store, include_secret_values=False)


@router.post("/admin/config/backup", response_model=ConfigBackupPayload)
async def export_config_backup_with_secrets(
    body: ConfigBackupExportRequest,
    request: Request,
    response: Response,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ConfigBackupPayload:
    """Export all administrator-configurable state, secret values included on request.

    With ``include_secrets`` the bundle carries every secret in plain text --
    the signing and encryption secrets, the admin token, the identity key and
    every provider key -- so the signed-in administrator must give the
    account's password (:func:`_confirm_administrator`); the admin token
    cannot. Without it, the same bundle as ``GET``.
    """
    if body.include_secrets:
        await _confirm_administrator(
            request, op_store, body.password, action="Exporting secret values"
        )
    return await _export(
        response, actor, services, op_store, include_secret_values=body.include_secrets
    )


async def _kept_provider_key_values(op_store: Any, keys: list[Any]) -> dict[str, str]:
    """This deployment's value for each key the backup withholds, by id, where it has one.

    Only for the same id under the same provider: a value is never moved to
    another provider's pool.
    """
    kept: dict[str, str] = {}
    for key in keys:
        if key.api_key is not None:
            continue
        key_id = key.id.strip()
        full = await _call_store(op_store, "get_provider_key_full", key_id, default=None)
        if full is not None and full[0] == key.provider.strip():
            kept[key_id] = full[1]
    return kept


def _security_relevant_setting(key: str) -> bool:
    """Whether changing ``app_config`` *key* from a backup needs confirmation and the password."""
    return app_config.security_relevant(key)


def _route_target_changed(
    current: Any, target: Any, *, default_deployment_type: str | None
) -> bool:
    """Whether a route would send requests, or a key, somewhere or some way it does not now.

    The deployment type decides whether a route is the gateway's own engine,
    which is given only remote media the gateway fetched itself: a
    ``self_hosted`` route made ``provider_api`` is sent clients' media URLs to
    fetch from inside the deployment's network.
    """
    if current is None:
        return True
    return (
        str(current.get("base_url") or "") != target.base_url
        or str(current.get("provider") or "") != target.provider
        or current.get("api_key_id") != target.api_key_id
        or (current.get("deployment_type") or default_deployment_type)
        != (target.deployment_type or default_deployment_type)
    )


async def _restore_plan(
    payload: ConfigBackupPayload, op_store: Any
) -> tuple[list[str], list[str], app_config.RestoreSummary]:
    """What restoring *payload* now would change that is security-relevant, and leave unset.

    Returns:
        The security-relevant changes and the withheld secrets this deployment
        has no value for, each as sorted ``domain:name`` labels, and the
        ``app_config`` summary they were drawn from. Labels never carry a value.

    Raises:
        app_config.ConfigUpdateError: the ``app_config`` rows cannot be restored.
    """
    summary = await app_config.preview_restore_rows(
        [row.model_dump() for row in payload.app_config]
    )
    changes = {f"app_config:{key}" for key in summary.changed if _security_relevant_setting(key)}
    not_restored = {f"app_config:{key}" for key in summary.not_restored}

    current_settings = {
        str(row["key"]): str(row["value"])
        for row in await _call_store(op_store, "list_settings", default=[])
    }
    target_settings = {item.key.strip(): item.value for item in _restorable_site_settings(payload)}
    for key in sorted(
        name
        for name in current_settings.keys() | target_settings.keys()
        if _security_relevant_site_setting(name)
    ):
        if key not in target_settings:
            if key in current_settings:
                changes.add(f"site_settings:{key}")
            continue
        value = target_settings[key]
        if value is None:
            if key not in current_settings:
                not_restored.add(f"site_settings:{key}")
        elif current_settings.get(key) != value:
            changes.add(f"site_settings:{key}")

    # Who may use a model: a lower tier opens it to more accounts.
    current_visibility = {
        str(row["model_id"]): str(row["required_role"])
        for row in await _call_store(op_store, "list_model_visibility_overrides", default=[])
    }
    target_visibility = {v.model_id: v.required_role for v in payload.model_visibility_overrides}
    for model_id in current_visibility.keys() | target_visibility.keys():
        if current_visibility.get(model_id) != target_visibility.get(model_id):
            changes.add(f"model_visibility_overrides:{model_id}")

    # Which tiers may spend an environment key; a key with no row is everyone's.
    current_env_roles = {
        (str(provider), str(key_hash)): (str(prefix), str(min_role))
        for provider, key_hash, prefix, min_role in await _call_store(
            op_store, "list_all_provider_env_key_reservations", default=[]
        )
    }
    target_env_roles = {
        (item.provider.strip(), item.key_hash.strip()): (item.key_prefix, item.min_role)
        for item in payload.provider_env_key_min_roles
    }
    for env_key in current_env_roles.keys() | target_env_roles.keys():
        before = current_env_roles.get(env_key, ("", "free"))
        after = target_env_roles.get(env_key, ("", "free"))
        if before[1] != after[1]:
            changes.add(f"provider_env_key_min_roles:{env_key[0]}/{after[0] or before[0]}")

    current_defs = {
        defn.provider: defn
        for defn in await _call_store(op_store, "list_provider_definitions", default=[])
    }
    for d in payload.provider_definitions:
        current = current_defs.get(d.provider.strip())
        if (
            current is None
            or current.default_base_url != d.default_base_url.strip()
            or current.adapter_kind != (d.adapter_kind.strip() or "openai_compat")
        ):
            changes.add(f"provider_definitions:{d.provider.strip()}")

    current_keys = {
        row.id: row for row in await _call_store(op_store, "list_provider_keys", default=[])
    }
    kept = await _kept_provider_key_values(op_store, payload.provider_keys)
    for k in payload.provider_keys:
        key_id, provider = k.id.strip(), k.provider.strip()
        label = f"provider_keys:{provider}/{key_id}"
        current_key = current_keys.get(key_id)
        same_key = current_key is not None and current_key.provider == provider
        if same_key and (current_key.min_role or "free") != k.min_role:
            # Which tiers may spend it, whether or not the backup holds its value.
            changes.add(label)
        if k.api_key is None:
            if key_id not in kept:
                not_restored.add(label)
            continue
        if not same_key:
            changes.add(label)
            continue
        full = await _call_store(op_store, "get_provider_key_full", key_id, default=None)
        if full is None or full[1] != k.api_key:
            changes.add(label)

    for domain, method, routes, default_deployment_type in (
        (
            "provider_route_candidates",
            "list_all_provider_route_candidates",
            payload.provider_route_candidates,
            "auto",
        ),
        (
            # A route override without a deployment type keeps the route's own.
            "provider_route_configs",
            "list_all_provider_route_configs",
            payload.provider_route_configs,
            None,
        ),
    ):
        current_routes = {
            (str(row["model_id"]), str(row["route_id"])): row
            for row in await _call_store(op_store, method, default=[])
        }
        for route in routes:
            if _route_target_changed(
                current_routes.get((route.model_id, route.route_id)),
                route,
                default_deployment_type=default_deployment_type,
            ):
                changes.add(f"{domain}:{route.model_id}/{route.route_id}")

    current_domains = {
        (str(row["domain"]), bool(row.get("is_wildcard", False)))
        for row in await _call_store(op_store, "list_signup_allowed_domains", default=[])
    }
    if current_domains != {
        (d.domain.strip(), d.is_wildcard) for d in payload.signup_allowed_domains
    }:
        # Who may sign up: an empty allowlist admits every address.
        changes.add("signup_allowed_domains")

    return sorted(changes), sorted(not_restored), summary


@contextlib.asynccontextmanager
async def _stores_in(
    transaction: Any, op_store: Any, batch_store: Any
) -> AsyncIterator[tuple[Any, Any]]:
    """The operational and batch stores, writing on the ``app_config`` transaction's connection.

    All three share the database and its pool, so on that one connection their
    writes commit or roll back together. A store that cannot be bound refuses
    the restore rather than write outside the transaction. A transaction with
    no connection to share is an in-memory double, whose stores are used as
    they are.
    """
    connection = getattr(transaction, "connection", None)
    if not callable(connection):
        yield op_store, batch_store
        return

    def bound(store: Any, conn: Any) -> Any:
        if store is None:
            return None
        bind = getattr(store, "on_connection", None)
        if not callable(bind):
            raise RuntimeError(
                f"{type(store).__name__} cannot write inside the configuration transaction; "
                "nothing was restored."
            )
        return bind(conn)

    async with connection() as conn:
        yield bound(op_store, conn), bound(batch_store, conn)


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
            if val is None:
                # Withheld from the backup: this deployment's value stays, and
                # without one the setting stays unset.
                continue
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

        # 3. provider_api_keys. A key whose value the backup withholds keeps
        # this deployment's value for the same id; without one it is left out.
        kept_values = await _kept_provider_key_values(op_store, payload.provider_keys)
        current_keys = await _call_store(op_store, "list_provider_keys", default=[])
        for existing in current_keys:
            await _call_store(op_store, "delete_provider_key", existing.id)
        for k in payload.provider_keys:
            api_key = k.api_key if k.api_key is not None else kept_values.get(k.id.strip())
            if api_key is None:
                continue
            await _call_store(
                op_store,
                "add_provider_key",
                provider=k.provider.strip(),
                api_key=api_key,
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

        # 7b. provider_route_batch_policies: the bundle's choices replace the
        # current ones; a route the bundle leaves out follows its locality.
        target_batch_policies = {
            (p.model_id, p.route_id): p.batch_allowed for p in payload.provider_route_batch_policies
        }
        for row in await _call_store(
            op_store, "list_all_provider_route_batch_policies", default=[]
        ):
            key = (str(row["model_id"]), str(row["route_id"]))
            if key not in target_batch_policies:
                await _call_store(
                    op_store,
                    "set_provider_route_batch_policy",
                    key[0],
                    key[1],
                    batch_allowed=None,
                    updated_by=admin_id,
                )
        for (model_id, route_id), batch_allowed in target_batch_policies.items():
            await _call_store(
                op_store,
                "set_provider_route_batch_policy",
                model_id,
                route_id,
                batch_allowed=batch_allowed,
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

    batch_route_resolver = getattr(services, "batch_route_resolver", None)
    if batch_route_resolver is not None:
        await _call_store(batch_route_resolver, "load_all")

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


async def _plan_or_refuse(
    payload: ConfigBackupPayload, op_store: Any
) -> tuple[list[str], list[str], app_config.RestoreSummary]:
    _validate_backup_payload(payload)
    try:
        return await _restore_plan(payload, op_store)
    except app_config.ConfigUpdateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


# A bundle can be large, so the two restore routes keep the general body limit
# rather than the small one other /admin paths get (RequestBodyLimitMiddleware),
# and read it only once the caller is known to be an administrator: a declared
# body is read and decoded before any dependency, authentication included.
_bundle_body = json_body_after(verify_admin_access, ConfigBackupPayload)
_restore_body = json_body_after(verify_admin_access, ConfigRestoreRequest)


@router.post(
    "/admin/config/restore/preview",
    response_model=ConfigRestorePreview,
    openapi_extra=json_body_schema(ConfigBackupPayload),
)
async def preview_config_restore(
    response: Response,
    payload: ConfigBackupPayload = Depends(_bundle_body),
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ConfigRestorePreview:
    """Say what restoring *payload* would change, writing nothing.

    ``security_changes`` lists what the restore may change only once the
    administrator confirms it and gives the account's password;
    ``secrets_not_restored`` the secrets the bundle withholds that this
    deployment has no value for.
    """
    response.headers["Cache-Control"] = "no-store"
    security_changes, not_restored, _summary = await _plan_or_refuse(payload, op_store)
    return ConfigRestorePreview(
        security_changes=security_changes, secrets_not_restored=not_restored
    )


@router.post(
    "/admin/config/restore",
    response_model=ConfigRestoreResponse,
    openapi_extra=json_body_schema(ConfigRestoreRequest),
)
async def restore_config_backup(
    request: Request,
    response: Response,
    payload: ConfigRestoreRequest = Depends(_restore_body),
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ConfigRestoreResponse:
    """Restore administrator-configurable state from a backup bundle, all of it or none.

    A bundle is not signed, so a restore that changes anything
    security-relevant (``POST /admin/config/restore/preview`` lists it) needs
    ``confirm_security_changes`` set to exactly that list, and the signed-in
    administrator's ``password``. The list is checked again here, so a
    configuration that changed since the preview is refused with 409 rather
    than changed past what was confirmed.

    Every table is written in the ``app_config`` transaction, on its
    connection, so a failure anywhere rolls all of it back: nothing is
    restored. A secret the bundle withholds keeps this deployment's value; one
    this deployment lacks is left unset and named in ``secrets_not_restored``.
    """
    response.headers["Cache-Control"] = "no-store"

    security_changes, not_restored, _summary = await _plan_or_refuse(payload, op_store)
    if security_changes:
        confirmed = payload.confirm_security_changes
        if confirmed is None or sorted(set(confirmed)) != security_changes:
            raise HTTPException(
                status_code=409,
                detail="This backup changes security settings that were not confirmed: "
                f"{', '.join(security_changes)}. Preview the restore, confirm these changes and "
                "give your password.",
            )
        await _confirm_administrator(
            request, op_store, payload.password, action="Restoring security settings"
        )

    batch_store = getattr(services, "batch_store", None)
    app_config_dicts = [row.model_dump() for row in payload.app_config]
    admin_user_id = get_admin_user_id(request)

    async def apply_in_transaction(transaction: Any) -> None:
        async with _stores_in(transaction, op_store, batch_store) as (tx_op_store, tx_batch):
            await _apply_operational_and_batch_stores(
                tx_op_store, tx_batch, payload, admin_id, admin_user_id
            )

    try:
        summary = await app_config.restore_rows(
            app_config_dicts, updated_by=admin_id, before_commit=apply_in_transaction
        )
    except app_config.ConfigUpdateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except asyncpg.PostgresError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The backup could not be restored ({type(exc).__name__}); nothing was changed.",
        ) from exc

    await _refresh_live_caches(services, op_store)

    # The app_config rows' own answer, from inside the transaction, wins over
    # the plan's for that domain.
    not_restored = sorted(
        {label for label in not_restored if not label.startswith("app_config:")}
        | {f"app_config:{key}" for key in summary.not_restored}
    )
    await log_admin_action(
        op_store,
        action="config.restore",
        actor=actor,
        target=AuditTarget("config", "backup"),
        details={
            "secrets_included": payload.secrets_included,
            "app_config": len(payload.app_config),
            "site_settings": len(payload.site_settings),
            "provider_definitions": len(payload.provider_definitions),
            "provider_keys": len(payload.provider_keys),
            "provider_route_candidates": len(payload.provider_route_candidates),
            "provider_route_configs": len(payload.provider_route_configs),
            "immutable_keys_preserved": list(summary.preserved_immutable),
            "security_changes": security_changes,
            "secrets_not_restored": not_restored,
        },
    )

    return ConfigRestoreResponse(
        config=ConfigResponse(
            **app_config.describe(offered_only=True),
            restart_supported=restart.restart_supported(),
        ),
        immutable_keys_preserved=list(summary.preserved_immutable),
        security_changes=security_changes,
        secrets_not_restored=not_restored,
    )
