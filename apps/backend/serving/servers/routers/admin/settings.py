"""Admin runtime settings endpoints.

A runtime setting marked security-relevant in the registry
(:func:`~serving.config.runtime_settings.runtime_setting_security_relevant`:
who may sign in or register, whether inference needs a key, what is logged,
what each account may spend and run) is changed only after the signed-in
administrator confirms it (:func:`confirm_administrator`), as a security
setting of ``PATCH /admin/config`` is. ``ADMIN_TOKEN`` cannot confirm, so it
changes only the others. ``PATCH /admin/settings`` writes several at once
behind one confirmation.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.accounting import get_accounting_policy
from serving.audit import AuditActor, AuditTarget
from serving.auth.signup_policy import (
    distribution_allows_public_signup,
    invalidate_signup_policy_cache,
)
from serving.auto_mode import AUTO_MODE_SETTING_KEY, AutoMode
from serving.config.app_config import refresh_health
from serving.config.distribution import DistributionConfigError
from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
    runtime_setting_security_relevant,
)
from serving.config.settings import get_settings
from serving.schemas_admin import (
    ListSettingsResponse,
    RuntimeSettingItem,
    UpdateSettingRequest,
    UpdateSettingsRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    database_enabled,
    get_admin_audit_actor,
    get_auto_mode,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers.admin.reauthentication import confirm_administrator

router = APIRouter(prefix="/admin")

ROUTEWISE_SETTINGS_KEYS = {
    "routewise_budget_alpha",
    "routewise_latency_slo_sec",
    "routewise_probe_enabled",
    "routewise_probe_interval_sec",
}


def _require_runtime_settings(rt: RuntimeSettings | None) -> RuntimeSettings:
    """Return the singleton or raise 503 if the app hasn't initialized it yet."""
    if rt is None:
        raise HTTPException(status_code=503, detail="Runtime settings not initialized")
    return rt


@router.get("/settings", response_model=ListSettingsResponse)
async def list_runtime_settings_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
    auto_mode: AutoMode | None = Depends(get_auto_mode),
) -> ListSettingsResponse:
    """List the runtime settings with current values and defaults.

    The RouteWise ones have a panel of their own, and the auto-mode switch is
    left out while the model registry configures no auto models for it to serve.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)
    items = await rt.list_all()
    hidden = set(ROUTEWISE_SETTINGS_KEYS)
    if auto_mode is None:
        hidden.add(AUTO_MODE_SETTING_KEY)
    return ListSettingsResponse(
        settings=[
            RuntimeSettingItem(
                key=i["key"],
                value=i["value"],
                value_type=i["value_type"],
                default_value=i["default_value"],
                description=i["description"],
                min=i.get("min"),
                max=i.get("max"),
                security_relevant=bool(i.get("security_relevant")),
            )
            for i in items
            if i["key"] not in hidden
        ]
    )


def _security_action(keys: list[str]) -> str:
    """Name what a confirmation confirms: "Changing signup_enabled", "Changing A, B and 2 more"."""
    shown = keys[:3]
    named = ", ".join(shown)
    if len(keys) > len(shown):
        named = f"{named} and {len(keys) - len(shown)} more"
    return f"Changing {named}"


def _checked_value(key: str, value: Any, currency: str | None = None) -> tuple[dict[str, Any], Any]:
    """Return *key*'s registry entry and the value to store, or raise why it cannot be.

    A role quota sent in *currency* (the deployment's accounting unit) is
    stored in USD.

    Everything is checked here, before any confirmation is asked for and
    before anything is written: a value that would be refused never spends a
    password attempt.
    """
    entry = RUNTIME_SETTINGS_REGISTRY.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    if key in ROUTEWISE_SETTINGS_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

    expected_type = entry["type"]
    if key.startswith("user_daily_quota_") and currency is not None:
        policy = get_accounting_policy()
        if currency != policy.currency:
            raise HTTPException(409, "Accounting currency changed; reload before saving")
        try:
            value = float(policy.to_usd(value))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise HTTPException(400, "Quota must be a finite non-negative amount") from exc
    if expected_type == "bool" and not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a boolean value")
    # ``bool`` is a subclass of ``int`` in Python; without the explicit check, a
    # JSON ``true`` would be accepted as an int/float and persisted as ``"True"``,
    # which then fails coercion on read (``int("True")`` raises).
    if expected_type == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects an integer value")
    if expected_type == "float" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a numeric value")
    if expected_type == "str" and not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a string value")

    if expected_type in ("int", "float"):
        # Before the bounds: NaN compares false to both of them.
        if not math.isfinite(value):
            raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a finite number")
        lo = entry.get("min")
        hi = entry.get("max")
        if lo is not None and value < lo:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' value {value} is below min ({lo})",
            )
        if hi is not None and value > hi:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' value {value} is above max ({hi})",
            )

    if key == "user_auth_enabled" and value is True:
        try:
            get_settings().validate_auth_secrets(
                database_enabled=database_enabled(), user_auth_enabled=True
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if key == "signup_enabled" and value is True:
        try:
            allowed = distribution_allows_public_signup()
        except DistributionConfigError:
            raise HTTPException(
                status_code=503,
                detail="Cannot enable signup until the distribution configuration is valid.",
            ) from None
        if not allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The active manifest disables public signup. Change features.public_signup "
                    "to true or null and restart the backend before enabling this setting."
                ),
            )

    return entry, value


async def _confirm_if_guarded(
    request: Request, op_store: Any, keys: list[str], password: str | None
) -> None:
    """Ask the administrator to confirm before *keys* change, when one of them is guarded."""
    guarded = sorted(key for key in keys if runtime_setting_security_relevant(key))
    if guarded:
        await confirm_administrator(request, op_store, password, action=_security_action(guarded))


async def _stored_value(key: str, entry: dict[str, Any], op_store: Any) -> Any:
    """What *key* holds now, for the audit log: its stored row, else the environment's."""
    expected_type = entry["type"]
    old_row = await op_store.get_setting(key)
    if old_row is None:
        return getattr(get_settings(), key, entry["default"])
    raw = old_row.get("value")
    if expected_type == "bool":
        return raw.lower() in ("true", "1", "yes") if isinstance(raw, str) else raw
    if expected_type == "int":
        return int(raw) if raw is not None else None
    if expected_type == "float":
        return float(raw) if raw is not None else None
    return raw


async def _publish(
    key: str,
    entry: dict[str, Any],
    value: Any,
    old_value: Any,
    *,
    actor: AuditActor,
    op_store: Any,
    rt: RuntimeSettings,
) -> RuntimeSettingItem:
    """After *value* is stored: refresh what reads *key*, and audit the change."""
    # Cache the stored value rather than dropping the old one: a dropped entry
    # reads as "not loaded", and synchronous readers such as
    # is_user_auth_enabled() would fall back to the environment until the next
    # refresh.
    rt.set_cached(key, value)
    if key == "signup_enabled":
        # This write proves the store answers, so any fail-closed window the
        # policy resolver is holding is stale and must not mask the new value.
        invalidate_signup_policy_cache()
    # Signup and verification decide whether mail settings are required.
    refresh_health()

    await log_admin_action(
        op_store,
        action="settings.update",
        details={"key": key, "old_value": old_value, "new_value": value},
        actor=actor,
        target=AuditTarget("setting", key),
    )

    return RuntimeSettingItem(
        key=key,
        value=value,
        value_type=entry["type"],
        default_value=entry["default"],
        description=entry["description"],
        min=entry.get("min"),
        max=entry.get("max"),
        security_relevant=runtime_setting_security_relevant(key),
    )


@router.patch("/settings/{key}", response_model=RuntimeSettingItem)
async def update_runtime_setting_endpoint(
    key: str,
    payload: UpdateSettingRequest,
    request: Request,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RuntimeSettingItem:
    """Update a single runtime setting by key.

    A security-relevant one (``security_relevant`` on its item) needs the
    signed-in administrator's confirmation: 403 ``TWO_FACTOR_REQUIRED`` when
    the account has an authenticator and owes a code, 400 for a missing or
    wrong ``password``, 403 for ``ADMIN_TOKEN``, 429 when rate-limited.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)
    entry, value = _checked_value(key, payload.value, payload.currency)
    await _confirm_if_guarded(request, op_store, [key], payload.password)
    old_value = await _stored_value(key, entry, op_store)
    await op_store.set_setting(key, str(value), entry["type"], admin_id)
    return await _publish(key, entry, value, old_value, actor=actor, op_store=op_store, rt=rt)


@router.patch("/settings", response_model=ListSettingsResponse)
async def update_runtime_settings_endpoint(
    payload: UpdateSettingsRequest,
    request: Request,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> ListSettingsResponse:
    """Update several runtime settings, confirmed once when any of them is guarded.

    Every value is checked first, and nothing is written when one is refused.
    A guarded one asks for the confirmation ``PATCH /admin/settings/{key}``
    does, once for the whole request. The values are stored in one
    transaction, so a store that fails part way leaves every setting as it
    was; only then do caches and the audit log follow. Answers the settings
    as written.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)
    checked = {
        key: _checked_value(key, value, payload.currency) for key, value in payload.values.items()
    }
    await _confirm_if_guarded(request, op_store, list(checked), payload.password)
    old_values = {
        key: await _stored_value(key, entry, op_store) for key, (entry, _) in checked.items()
    }
    await op_store.set_settings(
        [(key, str(value), entry["type"]) for key, (entry, value) in checked.items()], admin_id
    )
    return ListSettingsResponse(
        settings=[
            await _publish(
                key, entry, value, old_values[key], actor=actor, op_store=op_store, rt=rt
            )
            for key, (entry, value) in checked.items()
        ]
    )
