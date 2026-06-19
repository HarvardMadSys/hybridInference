"""Admin endpoints for runtime-managed upstream provider API keys."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serving.adapters import dynamic_keys
from serving.schemas_admin import (
    AddProviderApiKeyRequest,
    AddProviderApiKeyResponse,
    DeleteProviderApiKeyResponse,
    DisableProviderEnvKeyRequest,
    DisableProviderEnvKeyResponse,
    EnableProviderEnvKeyRequest,
    EnableProviderEnvKeyResponse,
    ListProviderApiKeysResponse,
    ProviderApiKeyItem,
    SetProviderApiKeyStatusResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.logging import get_logger

router = APIRouter(prefix="/admin")

logger = get_logger(__name__)


def _mask(api_key: str) -> str:
    """Mask an upstream provider API key for display."""
    if len(api_key) >= 16:
        return f"{api_key[:8]}...{api_key[-4:]}"
    return "***configured***"


def _env_key_id(api_key: str) -> str:
    return f"env:{dynamic_keys.env_key_hash(api_key)[:32]}"


def _validate_provider(provider: str) -> None:
    """Reject providers that did not appear in the loaded model registry."""
    known = dynamic_keys.get_known_providers()
    if provider not in known:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown provider {provider!r}. "
                f"Valid providers: {sorted(known) or '<none registered>'}"
            ),
        )


@router.get("/provider-keys", response_model=ListProviderApiKeysResponse)
async def list_provider_keys(
    provider: str | None = None,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListProviderApiKeysResponse:
    """List configured upstream provider API keys.

    Combines runtime DB rows with env-var-sourced keys discovered from the
    in-process adapter key pools. Raw secrets are never returned — every
    entry is masked.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    db_rows = await op_store.list_provider_keys(provider)
    db_raw_keys: dict[str, set[str]] = {}
    for row in db_rows:
        db_raw_keys.setdefault(row.provider, set())

    # Materialize the raw DB key strings keyed by provider so we can
    # subtract them from the live pool snapshot to identify env-only keys.
    providers_to_inspect: list[str]
    if provider is not None:
        providers_to_inspect = [provider]
    else:
        providers_to_inspect = sorted(dynamic_keys.get_known_providers())

    for prov in providers_to_inspect:
        try:
            db_raw_keys[prov] = set(await op_store.list_provider_keys_full(prov))
        except Exception as exc:
            raise HTTPException(503, f"Failed to load provider keys for {prov}: {exc}") from exc

    disabled_hashes: dict[str, set[str]] = {}
    for prov in providers_to_inspect:
        try:
            disabled_hashes[prov] = set(await op_store.list_disabled_provider_env_key_hashes(prov))
        except Exception as exc:
            raise HTTPException(503, f"Failed to load provider keys for {prov}: {exc}") from exc

    keys: list[ProviderApiKeyItem] = []
    for row in db_rows:
        keys.append(
            ProviderApiKeyItem(
                id=row.id,
                provider=row.provider,
                key_prefix=row.key_prefix,
                label=row.label,
                source="db",
                status=row.status,
                created_at=row.created_at,
            )
        )

    for prov in providers_to_inspect:
        # Candidate env keys include live pool keys AND the static keys of
        # legacy single-api_key adapters that have not been promoted to a pool,
        # so those env credentials are still surfaced for management.
        seen: set[str] = set()
        for raw in dynamic_keys.list_candidate_env_keys(prov):
            if raw in db_raw_keys.get(prov, set()):
                continue
            raw_hash = dynamic_keys.env_key_hash(raw)
            if raw_hash in disabled_hashes.get(prov, set()) or dynamic_keys.is_env_key_disabled(
                prov,
                raw_hash,
            ):
                continue
            if raw in seen:
                continue
            seen.add(raw)
            keys.append(
                ProviderApiKeyItem(
                    id=_env_key_id(raw),
                    provider=prov,
                    key_prefix=_mask(raw),
                    label=None,
                    source="env",
                    status="active",
                    created_at=None,
                )
            )

    # Disabled env keys are no longer in any pool, so surface them from the
    # tombstone table with their masked prefix and an enable affordance.
    for prov in providers_to_inspect:
        try:
            tombstones = await op_store.list_disabled_provider_env_keys(prov)
        except Exception as exc:
            raise HTTPException(503, f"Failed to load provider keys for {prov}: {exc}") from exc
        for key_hash, key_prefix in tombstones:
            keys.append(
                ProviderApiKeyItem(
                    id=f"env:{key_hash[:32]}",
                    provider=prov,
                    key_prefix=key_prefix,
                    label=None,
                    source="env",
                    status="disabled",
                    created_at=None,
                )
            )

    return ListProviderApiKeysResponse(provider=provider, keys=keys)


@router.post("/provider-keys", response_model=AddProviderApiKeyResponse, status_code=201)
async def add_provider_key(
    payload: AddProviderApiKeyRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> AddProviderApiKeyResponse:
    """Persist a new provider API key and inject it into matching pools."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    _validate_provider(payload.provider)

    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(422, "api_key must not be blank")

    key_id = await op_store.add_provider_key(
        provider=payload.provider,
        api_key=api_key,
        label=payload.label,
        created_by=admin_id,
    )

    pools_updated = dynamic_keys.add_key_to_provider(payload.provider, api_key)
    if pools_updated == 0:
        # The key is persisted but no live adapter accepted it, so it will not
        # be used for inference. Surface it loudly instead of reporting success.
        logger.warning(
            "provider key for %r persisted but attached to 0 pools - no "
            "multi-key-capable adapter is registered for this provider; the "
            "key will NOT be used for inference",
            payload.provider,
        )

    await log_admin_action(
        op_store,
        admin_id,
        "add_provider_key",
        None,
        {
            "id": key_id,
            "provider": payload.provider,
            "key_prefix": _mask(api_key),
            "label": payload.label,
            "pools_updated": pools_updated,
        },
    )

    rows = await op_store.list_provider_keys(payload.provider)
    new_row = next((r for r in rows if r.id == key_id), None)
    if new_row is None:
        raise HTTPException(500, "Failed to read back inserted key")

    return AddProviderApiKeyResponse(
        key=ProviderApiKeyItem(
            id=new_row.id,
            provider=new_row.provider,
            key_prefix=new_row.key_prefix,
            label=new_row.label,
            source="db",
            status=new_row.status,
            created_at=new_row.created_at,
        ),
        pools_updated=pools_updated,
    )


@router.post("/provider-keys/disable-env", response_model=DisableProviderEnvKeyResponse)
async def disable_provider_env_key(
    payload: DisableProviderEnvKeyRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> DisableProviderEnvKeyResponse:
    """Persistently disable an env-sourced provider API key."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    _validate_provider(payload.provider)

    try:
        db_raw_keys = set(await op_store.list_provider_keys_full(payload.provider))
    except Exception as exc:
        raise HTTPException(
            503,
            f"Failed to load provider keys for {payload.provider}: {exc}",
        ) from exc

    target_key: str | None = None
    for raw in dynamic_keys.list_candidate_env_keys(payload.provider):
        if raw in db_raw_keys:
            continue
        if _env_key_id(raw) == payload.env_key_id:
            target_key = raw
            break

    if target_key is None:
        raise HTTPException(404, "Env provider key not found")

    key_hash = dynamic_keys.env_key_hash(target_key)
    key_prefix = _mask(target_key)
    await op_store.disable_provider_env_key(
        provider=payload.provider,
        key_hash=key_hash,
        key_prefix=key_prefix,
        disabled_by=admin_id,
    )
    pools_updated = dynamic_keys.disable_env_key_for_provider(
        payload.provider,
        target_key,
        key_hash,
    )

    await log_admin_action(
        op_store,
        admin_id,
        "disable_provider_env_key",
        None,
        {
            "id": payload.env_key_id,
            "provider": payload.provider,
            "key_prefix": key_prefix,
            "pools_updated": pools_updated,
        },
    )

    return DisableProviderEnvKeyResponse(
        id=payload.env_key_id,
        provider=payload.provider,
        pools_updated=pools_updated,
    )


@router.post("/provider-keys/enable-env", response_model=EnableProviderEnvKeyResponse)
async def enable_provider_env_key(
    payload: EnableProviderEnvKeyRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> EnableProviderEnvKeyResponse:
    """Re-enable a previously disabled env-sourced provider API key."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    _validate_provider(payload.provider)

    try:
        tombstones = await op_store.list_disabled_provider_env_keys(payload.provider)
    except Exception as exc:
        raise HTTPException(
            503,
            f"Failed to load provider keys for {payload.provider}: {exc}",
        ) from exc

    # The list view exposes a truncated id (``env:{hash[:32]}``); recover the
    # full hash from the tombstone rows so we can clear the right one.
    target_hash: str | None = None
    for key_hash, _prefix in tombstones:
        if f"env:{key_hash[:32]}" == payload.env_key_id:
            target_hash = key_hash
            break

    if target_hash is None:
        raise HTTPException(404, "Disabled env provider key not found")

    await op_store.enable_provider_env_key(payload.provider, target_hash)
    pools_updated = dynamic_keys.enable_env_key_for_provider(payload.provider, target_hash)

    await log_admin_action(
        op_store,
        admin_id,
        "enable_provider_env_key",
        None,
        {
            "id": payload.env_key_id,
            "provider": payload.provider,
            "pools_updated": pools_updated,
        },
    )

    return EnableProviderEnvKeyResponse(
        id=payload.env_key_id,
        provider=payload.provider,
        pools_updated=pools_updated,
    )


@router.post("/provider-keys/{key_id}/disable", response_model=SetProviderApiKeyStatusResponse)
async def disable_provider_key(
    key_id: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> SetProviderApiKeyStatusResponse:
    """Disable a DB-sourced provider key (reversible) and drop it from pools."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    target = await op_store.get_provider_key_full(key_id)
    if target is None:
        raise HTTPException(404, f"Provider key {key_id!r} not found")
    provider, raw_key = target

    updated = await op_store.set_provider_key_status(key_id, "disabled")
    if not updated:
        raise HTTPException(404, f"Provider key {key_id!r} not found")

    # Don't evict the raw value from the pool if it is still active via another
    # source — another active DB row with the same value, or a non-disabled
    # env-configured key sharing it. (status was already flipped, so the active
    # full list no longer includes this row.)
    try:
        active_after = set(await op_store.list_provider_keys_full(provider))
    except Exception:
        active_after = set()
    shared = raw_key in active_after or dynamic_keys.is_active_env_static_key(provider, raw_key)
    pools_updated = 0 if shared else dynamic_keys.remove_key_from_pools(provider, raw_key)

    await log_admin_action(
        op_store,
        admin_id,
        "disable_provider_key",
        None,
        {
            "id": key_id,
            "provider": provider,
            "key_prefix": _mask(raw_key),
            "pools_updated": pools_updated,
        },
    )

    return SetProviderApiKeyStatusResponse(
        id=key_id,
        provider=provider,
        status="disabled",
        pools_updated=pools_updated,
    )


@router.post("/provider-keys/{key_id}/enable", response_model=SetProviderApiKeyStatusResponse)
async def enable_provider_key(
    key_id: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> SetProviderApiKeyStatusResponse:
    """Re-enable a disabled DB-sourced provider key and re-add it to pools."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    target = await op_store.get_provider_key_full(key_id)
    if target is None:
        raise HTTPException(404, f"Provider key {key_id!r} not found")
    provider, raw_key = target

    updated = await op_store.set_provider_key_status(key_id, "active")
    if not updated:
        raise HTTPException(404, f"Provider key {key_id!r} not found")

    pools_updated = dynamic_keys.add_key_to_provider(provider, raw_key)
    if pools_updated == 0:
        logger.warning(
            "provider key %r re-enabled but attached to 0 pools for provider %r",
            key_id,
            provider,
        )

    await log_admin_action(
        op_store,
        admin_id,
        "enable_provider_key",
        None,
        {
            "id": key_id,
            "provider": provider,
            "key_prefix": _mask(raw_key),
            "pools_updated": pools_updated,
        },
    )

    return SetProviderApiKeyStatusResponse(
        id=key_id,
        provider=provider,
        status="active",
        pools_updated=pools_updated,
    )


@router.delete("/provider-keys/{key_id}", response_model=DeleteProviderApiKeyResponse)
async def delete_provider_key(
    key_id: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> DeleteProviderApiKeyResponse:
    """Hard-delete a DB-sourced provider key and remove it from pools."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    target = await op_store.get_provider_key_full(key_id)
    if target is None:
        raise HTTPException(404, f"Provider key {key_id!r} not found")
    provider, raw_key = target

    deleted = await op_store.delete_provider_key(key_id)
    if not deleted:
        raise HTTPException(404, f"Provider key {key_id!r} not found")

    # ``remove_key_from_provider`` no-ops on env-configured keys with the
    # same raw value, so we always pass the raw key without checking.
    pools_updated = dynamic_keys.remove_key_from_provider(provider, raw_key)

    await log_admin_action(
        op_store,
        admin_id,
        "delete_provider_key",
        None,
        {
            "id": key_id,
            "provider": provider,
            "key_prefix": _mask(raw_key),
            "pools_updated": pools_updated,
        },
    )

    return DeleteProviderApiKeyResponse(
        id=key_id,
        provider=provider,
        pools_updated=pools_updated,
    )
