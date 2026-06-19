"""Admin endpoints for runtime-managed upstream provider API keys."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from serving.adapters import dynamic_keys
from serving.admin.provider_key_probe import (
    ProviderKeyProbeError,
    probe_provider_key_with_existing_route,
)
from serving.schemas_admin import (
    AddProviderApiKeyRequest,
    AddProviderApiKeyResponse,
    DeleteProviderApiKeyResponse,
    DisableProviderEnvKeyRequest,
    DisableProviderEnvKeyResponse,
    ListProviderApiKeyProvidersResponse,
    ListProviderApiKeysResponse,
    ProviderApiKeyItem,
    VerifyProviderApiKeyRequest,
    VerifyProviderApiKeyResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, get_services, verify_admin_access

router = APIRouter(prefix="/admin")


def _mask(api_key: str) -> str:
    """Mask an upstream provider API key for display."""
    if len(api_key) >= 16:
        return f"{api_key[:8]}...{api_key[-4:]}"
    return "***configured***"


def _env_key_id(api_key: str) -> str:
    return f"env:{dynamic_keys.env_key_hash(api_key)[:32]}"


_MAX_NUMBERED_ENV_KEYS = 20
_PROVIDER_ENV_KEY_VARS: dict[str, tuple[str, str]] = {
    "chutes": ("CHUTES_API_KEY", "CHUTES_API_KEY"),
    "featherless": ("FEATHERLESS_API_KEY", "FEATHERLESS_API_KEY"),
    "kimi": ("KIMI_CODING_API_KEY", "KIMI_CODING_API_KEY"),
    "minimax": ("MINIMAX_API_KEY", "MINIMAX_API_KEY"),
    "ollama": ("OLLAMA_API_KEY", "OLLAMA_API_KEY"),
    "zai": ("ZAI_API_KEY", "ZAI_API_KEY"),
}


def _numbered_env_keys_for_provider(provider: str) -> list[str]:
    """Return provider keys configured through base + numbered env vars."""
    spec = _PROVIDER_ENV_KEY_VARS.get(provider)
    if spec is None:
        return []

    base_var, numbered_prefix = spec
    base_value = os.getenv(base_var, "")
    if not base_value:
        return []

    keys = [base_value]
    for index in range(2, _MAX_NUMBERED_ENV_KEYS):
        value = os.getenv(f"{numbered_prefix}{index}", "")
        if not value:
            break
        keys.append(value)
    return keys


def _env_keys_for_provider(provider: str) -> list[str]:
    """Return live env-sourced keys from adapter pools plus numbered env vars."""
    keys: list[str] = []
    seen: set[str] = set()

    for pool in dynamic_keys.get_pools_for_provider(provider):
        for raw in pool.snapshot_keys():
            if raw and raw not in seen:
                seen.add(raw)
                keys.append(raw)

    for raw in _numbered_env_keys_for_provider(provider):
        if raw and raw not in seen:
            seen.add(raw)
            keys.append(raw)

    return keys


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
        for raw in _env_keys_for_provider(prov):
            if raw in db_raw_keys.get(prov, set()):
                continue
            raw_hash = dynamic_keys.env_key_hash(raw)
            if raw_hash in disabled_hashes.get(prov, set()) or dynamic_keys.is_env_key_disabled(
                prov,
                raw_hash,
            ):
                continue
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

    return ListProviderApiKeysResponse(provider=provider, keys=keys)


@router.get("/provider-keys/providers", response_model=ListProviderApiKeyProvidersResponse)
async def list_provider_key_providers(
    _admin_id: str = Depends(verify_admin_access),
) -> ListProviderApiKeyProvidersResponse:
    """List providers that support runtime-managed API keys."""
    return ListProviderApiKeyProvidersResponse(providers=sorted(dynamic_keys.get_known_providers()))


@router.post("/provider-keys/verify", response_model=VerifyProviderApiKeyResponse)
async def verify_provider_key(
    payload: VerifyProviderApiKeyRequest,
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
) -> VerifyProviderApiKeyResponse:
    """Verify a provider API key against an existing registered provider route."""
    _validate_provider(payload.provider)

    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(422, "api_key must not be blank")

    try:
        await probe_provider_key_with_existing_route(
            services,
            provider=payload.provider,
            api_key=api_key,
        )
    except ProviderKeyProbeError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return VerifyProviderApiKeyResponse(ok=True)


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
    for raw in _env_keys_for_provider(payload.provider):
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
