"""Admin endpoints for runtime-managed upstream provider API keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
    EnableProviderEnvKeyRequest,
    EnableProviderEnvKeyResponse,
    ListProviderApiKeyProvidersResponse,
    ListProviderApiKeysResponse,
    ProviderApiKeyItem,
    ProviderKeyByRefRequest,
    ProviderKeyByRefResponse,
    SetProviderApiKeyMinRoleRequest,
    SetProviderApiKeyMinRoleResponse,
    SetProviderApiKeyStatusResponse,
    SetProviderEnvKeyMinRoleRequest,
    VerifyProviderApiKeyRequest,
    VerifyProviderApiKeyResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, get_services, verify_admin_access
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


def _env_keys_for_provider(provider: str) -> list[str]:
    """Return live env-sourced keys from adapter pools plus numbered env vars."""
    keys: list[str] = []
    seen: set[str] = set()

    for pool in dynamic_keys.get_pools_for_provider(provider):
        for raw in pool.snapshot_keys():
            if raw and raw not in seen:
                seen.add(raw)
                keys.append(raw)

    for raw in dynamic_keys.configured_env_keys_for_provider(provider):
        if raw and raw not in seen:
            seen.add(raw)
            keys.append(raw)

    return keys


def _known_key_providers() -> set[str]:
    return {
        dynamic_keys.normalize_key_provider(provider)
        for provider in dynamic_keys.get_known_providers()
    }


def _validate_provider(provider: str) -> None:
    """Reject providers that did not appear in the loaded model registry."""
    known = _known_key_providers()
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
    env_min_roles: dict[str, dict[str, str]] = {}
    for prov in providers_to_inspect:
        try:
            disabled_hashes[prov] = set(await op_store.list_disabled_provider_env_key_hashes(prov))
            # Env reservations are keyed by the key's full hash, so they are read
            # from the DB rather than from the (truncated) list ids.
            env_min_roles[prov] = dict(await op_store.list_provider_env_key_min_roles(prov))
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
                min_role=row.min_role,  # type: ignore[arg-type]
            )
        )

    for prov in providers_to_inspect:
        # Candidate env keys union three sources so every env credential is
        # surfaced for management: live pool keys + base/numbered env vars
        # (``_env_keys_for_provider``) and the static keys of legacy
        # single-api_key adapters not yet promoted to a pool
        # (``list_candidate_env_keys``).
        seen: set[str] = set()
        candidates = list(_env_keys_for_provider(prov))
        for raw in dynamic_keys.list_candidate_env_keys(prov):
            if raw not in candidates:
                candidates.append(raw)
        for raw in candidates:
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
                    # The tier the pool actually enforces, which is the resolved
                    # one — a duplicate DB row declaring something stricter for the
                    # same credential must not be reported as the shared value.
                    min_role=dynamic_keys.resolve_key_min_role(prov, raw),  # type: ignore[arg-type]
                )
            )

    # Disabled env keys are no longer in any pool, so surface them from the
    # tombstone table with their masked prefix and an enable affordance. A
    # tombstone shadowed by an active DB row holding the same value is stale —
    # that key is live, and listing it twice (once active, once disabled) is
    # what the entry would otherwise say.
    for prov in providers_to_inspect:
        try:
            tombstones = await op_store.list_disabled_provider_env_keys(prov)
        except Exception as exc:
            raise HTTPException(503, f"Failed to load provider keys for {prov}: {exc}") from exc
        active_db_hashes = {dynamic_keys.env_key_hash(raw) for raw in db_raw_keys.get(prov, set())}
        for key_hash, key_prefix in tombstones:
            if key_hash in active_db_hashes:
                continue
            keys.append(
                ProviderApiKeyItem(
                    id=f"env:{key_hash[:32]}",
                    provider=prov,
                    key_prefix=key_prefix,
                    label=None,
                    source="env",
                    status="disabled",
                    created_at=None,
                    min_role=env_min_roles.get(prov, {}).get(key_hash, "free"),  # type: ignore[arg-type]
                )
            )

    return ListProviderApiKeysResponse(provider=provider, keys=keys)


@router.get("/provider-keys/providers", response_model=ListProviderApiKeyProvidersResponse)
async def list_provider_key_providers(
    _admin_id: str = Depends(verify_admin_access),
) -> ListProviderApiKeyProvidersResponse:
    """List providers that support runtime-managed API keys."""
    return ListProviderApiKeyProvidersResponse(providers=sorted(_known_key_providers()))


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

    # Adding a credential states that it should serve traffic, and
    # ``add_key_to_provider`` below puts it straight into the pools — so a
    # tombstone left over from an earlier env-key disable has to go, or the key
    # runs live while a disabled record still exists for it.
    await _clear_env_tombstone_for_key(op_store, payload.provider, api_key, admin_id)

    key_id = await op_store.add_provider_key(
        provider=payload.provider,
        api_key=api_key,
        label=payload.label,
        created_by=admin_id,
        min_role=payload.min_role,
    )

    # Refresh the tier cache from the row just written, then attach: the attach
    # ends in the reconciliation sweep, so the key enters the pools already at its
    # resolved tier rather than untiered for a window.
    await _refresh_db_key_tiers(
        op_store,
        payload.provider,
        fallback=(api_key, payload.min_role),
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
            "min_role": payload.min_role,
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
            min_role=new_row.min_role,  # type: ignore[arg-type]
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
    candidates = list(_env_keys_for_provider(payload.provider))
    for raw in dynamic_keys.list_candidate_env_keys(payload.provider):
        if raw not in candidates:
            candidates.append(raw)
    for raw in candidates:
        if raw in db_raw_keys:
            continue
        if _env_key_id(raw) == payload.env_key_id:
            target_key = raw
            break

    if target_key is None:
        raise HTTPException(404, "Env provider key not found")

    pools_updated = await _tombstone_env_key(op_store, payload.provider, target_key, admin_id)

    return DisableProviderEnvKeyResponse(
        id=payload.env_key_id,
        provider=payload.provider,
        pools_updated=pools_updated,
    )


async def _tombstone_env_key(op_store, provider: str, raw_key: str, admin_id: str) -> int:
    """Record the disable tombstone for *raw_key* and drop it from the pools.

    Shared by the ``disable-env`` endpoint — which resolves the raw value from
    an ``env:{hash}`` id — and the by-ref path, which already holds it. Returns
    the number of pools the key was removed from.
    """
    key_hash = dynamic_keys.env_key_hash(raw_key)
    key_prefix = _mask(raw_key)
    await op_store.disable_provider_env_key(
        provider=provider,
        key_hash=key_hash,
        key_prefix=key_prefix,
        disabled_by=admin_id,
    )
    pools_updated = dynamic_keys.disable_env_key_for_provider(provider, raw_key, key_hash)

    await log_admin_action(
        op_store,
        admin_id,
        "disable_provider_env_key",
        None,
        {
            "id": _env_key_id(raw_key),
            "provider": provider,
            "key_prefix": key_prefix,
            "pools_updated": pools_updated,
        },
    )
    return pools_updated


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

    pools_updated = await _clear_env_tombstone(op_store, payload.provider, target_hash, admin_id)

    return EnableProviderEnvKeyResponse(
        id=payload.env_key_id,
        provider=payload.provider,
        pools_updated=pools_updated,
    )


async def _refresh_db_key_tiers(
    op_store,
    provider: str,
    *,
    fallback: tuple[str, str] | None = None,
) -> None:
    """Re-read the provider's DB-declared tiers and reconcile the live pools.

    Called after every mutation that can change them (add, re-tier, disable,
    enable, delete). The table is the authority — reading it back is what makes two
    rows declaring tiers for one raw value resolve correctly, and what stops a
    departing row's reservation from outliving it.

    ``fallback`` is ``(raw_key, min_role)`` for the declaration this request just
    persisted. When the read fails it is applied directly, because "the pools keep
    their previous tiers" is *not* uniformly safe: a ``free``→``pro`` re-tier would
    keep serving the key to free callers, and a newly added reserved key is attached
    straight into rotation with no declaration at all, so it would enter as shared.
    Applying the known declaration cannot widen anything — it never relaxes a
    stricter cached tier — so the reservation the caller was told about is enforced
    even on a degraded read.

    Removals pass no fallback: which declaration should survive depends on the rows
    that remain, which only the read can say, and guessing could relax a reservation
    another row still holds. Those keep their stricter cached tier until the next
    successful read.
    """
    try:
        tiers = await op_store.list_provider_key_min_roles(provider)
    except Exception as exc:
        if fallback is not None:
            raw_key, min_role = fallback
            applied = dynamic_keys.declare_db_key_min_role_no_relax(provider, raw_key, min_role)
            logger.warning(
                "failed to refresh DB key tier reservations for provider=%r; applied the "
                "just-written tier %r directly (enforcing %r) — other keys keep their "
                "cached tiers until the next read: %s",
                provider,
                min_role,
                applied,
                exc,
            )
            return
        logger.warning(
            "failed to refresh DB key tier reservations for provider=%r; "
            "live pools keep their current tiers: %s",
            provider,
            exc,
        )
        return
    dynamic_keys.load_db_key_min_roles(provider, tiers)


async def _resolve_env_key_id(
    op_store,
    provider: str,
    env_key_id: str,
) -> tuple[str, str, str | None]:
    """Resolve an ``env:{hash32}`` id to ``(full_hash, key_prefix, raw_key)``.

    The list view exposes a truncated hash, so the full one is recovered either
    from the live/candidate env keys (hashing the raw value) or — for a key that
    is currently disabled and therefore absent from every pool — from the
    tombstone rows, in which case ``raw_key`` is None. Raises 404 when the id
    matches no env key of *provider*.
    """
    try:
        db_raw_keys = set(await op_store.list_provider_keys_full(provider))
    except Exception as exc:
        raise HTTPException(503, f"Failed to load provider keys for {provider}: {exc}") from exc

    candidates = list(_env_keys_for_provider(provider))
    for raw in dynamic_keys.list_candidate_env_keys(provider):
        if raw not in candidates:
            candidates.append(raw)
    for raw in candidates:
        if raw in db_raw_keys:
            continue
        if _env_key_id(raw) == env_key_id:
            return (dynamic_keys.env_key_hash(raw), _mask(raw), raw)

    try:
        tombstones = await op_store.list_disabled_provider_env_keys(provider)
    except Exception as exc:
        raise HTTPException(503, f"Failed to load provider keys for {provider}: {exc}") from exc
    for key_hash, key_prefix in tombstones:
        if f"env:{key_hash[:32]}" == env_key_id:
            # Disabled: the raw value may still be recoverable from adapter
            # config, but the reservation is stored either way and applies when
            # the key is re-enabled.
            return (key_hash, key_prefix, dynamic_keys.find_env_key_by_hash(provider, key_hash))

    raise HTTPException(404, "Env provider key not found")


@router.post("/provider-keys/min-role-env", response_model=SetProviderApiKeyMinRoleResponse)
async def set_provider_env_key_min_role(
    payload: SetProviderEnvKeyMinRoleRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> SetProviderApiKeyMinRoleResponse:
    """Reserve an env-sourced provider key for a tier (or release it to all).

    Same semantics as the DB-key endpoint, addressed by hash because an env
    credential has no row of its own — the same way ``disable-env`` tombstones
    one. The reservation therefore survives the key leaving rotation (disabled,
    or its env var temporarily removed) and is re-applied when it returns, and at
    boot before any request is served.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    _validate_provider(payload.provider)

    key_hash, key_prefix, _raw = await _resolve_env_key_id(
        op_store,
        payload.provider,
        payload.env_key_id,
    )

    await op_store.set_provider_env_key_min_role(
        provider=payload.provider,
        key_hash=key_hash,
        key_prefix=key_prefix,
        min_role=payload.min_role,
        updated_by=admin_id,
    )
    pools_updated = dynamic_keys.set_env_key_min_role(
        payload.provider,
        key_hash,
        payload.min_role,
    )

    await log_admin_action(
        op_store,
        admin_id,
        "set_provider_env_key_min_role",
        None,
        {
            "id": payload.env_key_id,
            "provider": payload.provider,
            "key_prefix": key_prefix,
            "min_role": payload.min_role,
            "pools_updated": pools_updated,
        },
    )

    return SetProviderApiKeyMinRoleResponse(
        id=payload.env_key_id,
        provider=payload.provider,
        min_role=payload.min_role,
        pools_updated=pools_updated,
    )


async def _clear_env_tombstone_for_key(
    op_store,
    provider: str,
    raw_key: str,
    admin_id: str,
) -> bool:
    """Clear the tombstone for *raw_key* if it carries one. Returns True if so.

    Unlike :func:`_clear_env_tombstone` this is a no-op — no store write, no
    audit entry — when the key was never disabled, so it is safe to call on
    every add. A store failure is logged and treated as "no tombstone": the add
    itself must not fail because the tombstone table was unreadable.
    """
    key_hash = dynamic_keys.env_key_hash(raw_key)
    try:
        tombstones = await op_store.list_disabled_provider_env_keys(provider)
    except Exception as exc:
        logger.warning(
            "failed to check disabled env keys for provider=%r while adding a key: %s",
            provider,
            exc,
        )
        return False

    tombstoned = any(key_hash == existing for existing, _prefix in tombstones)
    if not tombstoned and not dynamic_keys.is_env_key_disabled(provider, key_hash):
        return False

    await _clear_env_tombstone(op_store, provider, key_hash, admin_id)
    return True


async def _clear_env_tombstone(op_store, provider: str, key_hash: str, admin_id: str) -> int:
    """Drop the tombstone for *key_hash* and put the key back in its pools.

    *key_hash* is the full hash, not the truncated ``key_ref`` form. Returns the
    number of pools the key was re-added to.
    """
    await op_store.enable_provider_env_key(provider, key_hash)
    pools_updated = dynamic_keys.enable_env_key_for_provider(provider, key_hash)

    await log_admin_action(
        op_store,
        admin_id,
        "enable_provider_env_key",
        None,
        {
            "id": f"env:{key_hash[:32]}",
            "provider": provider,
            "pools_updated": pools_updated,
        },
    )
    return pools_updated


@dataclass
class _KeyRefTargets:
    """Every source one credential is reachable from, with its current state.

    A raw key can be configured in more than one place at once — an env var and
    a DB row, several DB rows, or a route-bound row whose value also lives in
    the adapter's static config. Toggling only the first source found leaves the
    key live through the others while a disabled record exists for it, which the
    quota dashboard then renders as a second card for the same key.
    """

    db_ids: list[str]
    """DB rows holding this credential that are currently active."""

    disabled_db_ids: list[str]
    """DB rows holding this credential that are currently disabled."""

    raw_key: str | None
    """The raw credential, when it is still recoverable from a live source."""

    env_hash: str | None
    """Full env-key hash when the credential also has a static/env copy."""

    env_disabled: bool
    """True when the static/env copy carries a disable tombstone."""

    @property
    def source(self) -> Literal["db", "env"]:
        """Primary source label for the response body."""
        return "db" if (self.db_ids or self.disabled_db_ids) else "env"

    @property
    def all_active(self) -> bool:
        return not self.disabled_db_ids and not self.env_disabled

    @property
    def all_disabled(self) -> bool:
        return not self.db_ids and (self.env_hash is None or self.env_disabled)


async def _resolve_key_ref(op_store, provider: str, key_ref: str) -> _KeyRefTargets:
    """Resolve an opaque ``key_ref`` to every source that holds the credential.

    A raw value can be recorded as several DB rows *and* as a static/env
    credential at the same time; all of them are reported, because disabling
    only one leaves the key serving traffic through the others. Raises 404 when
    the ref matches no key of *provider*.
    """
    try:
        db_rows = await op_store.list_provider_keys(provider)
    except Exception as exc:
        raise HTTPException(503, f"Failed to load provider keys for {provider}: {exc}") from exc

    try:
        tombstones = await op_store.list_disabled_provider_env_keys(provider)
    except Exception as exc:
        raise HTTPException(503, f"Failed to load provider keys for {provider}: {exc}") from exc
    disabled_hashes = {key_hash for key_hash, _prefix in tombstones}

    raw_key: str | None = None
    db_ids: list[str] = []
    disabled_db_ids: list[str] = []
    for row in db_rows:
        target = await op_store.get_provider_key_full(row.id)
        if target is None:
            continue
        raw = target[1]
        if dynamic_keys.env_key_hash(raw)[:32] != key_ref:
            continue
        raw_key = raw
        if row.status == "disabled":
            disabled_db_ids.append(row.id)
        else:
            db_ids.append(row.id)

    if raw_key is None:
        candidates = list(_env_keys_for_provider(provider))
        for raw in dynamic_keys.list_candidate_env_keys(provider):
            if raw not in candidates:
                candidates.append(raw)
        for raw in candidates:
            if dynamic_keys.env_key_hash(raw)[:32] == key_ref:
                raw_key = raw
                break

    env_hash: str | None = None
    env_disabled = False
    if raw_key is not None:
        candidate_hash = dynamic_keys.env_key_hash(raw_key)
        env_disabled = candidate_hash in disabled_hashes or dynamic_keys.is_env_key_disabled(
            provider, candidate_hash
        )
        # Only a static/env-configured value has a copy the DB status cannot
        # reach. A dashboard-added key lives in the pool solely because it was
        # injected from its row, so it needs no tombstone.
        if (
            env_disabled
            or raw_key in dynamic_keys.configured_env_keys_for_provider(provider)
            or dynamic_keys.is_active_env_static_key(provider, raw_key)
        ):
            env_hash = candidate_hash
    else:
        for key_hash in disabled_hashes:
            if key_hash[:32] == key_ref:
                env_hash = key_hash
                env_disabled = True
                break

    if not db_ids and not disabled_db_ids and env_hash is None:
        raise HTTPException(404, "Provider key not found")

    return _KeyRefTargets(
        db_ids=db_ids,
        disabled_db_ids=disabled_db_ids,
        raw_key=raw_key,
        env_hash=env_hash,
        env_disabled=env_disabled,
    )


@router.post("/provider-keys/by-ref/disable", response_model=ProviderKeyByRefResponse)
async def disable_provider_key_by_ref(
    payload: ProviderKeyByRefRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ProviderKeyByRefResponse:
    """Disable one provider key identified by the quota dashboard's ``key_ref``.

    Disables the credential at *every* source that holds it — each DB row plus
    the env copy — so a key configured twice cannot keep serving traffic from
    the source that was not toggled.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    _validate_provider(payload.provider)
    targets = await _resolve_key_ref(op_store, payload.provider, payload.key_ref)
    if targets.all_disabled:
        return ProviderKeyByRefResponse(
            provider=payload.provider,
            key_ref=payload.key_ref,
            source=targets.source,
            status="disabled",
            pools_updated=0,
        )

    pools_updated = 0
    # Tombstone the static/env copy first: the DB path deliberately keeps a raw
    # value in the pool while an active env key still shares it, so the env copy
    # has to stop counting as active before the DB rows are evicted.
    if targets.env_hash is not None and not targets.env_disabled and targets.raw_key is not None:
        pools_updated += await _tombstone_env_key(
            op_store, payload.provider, targets.raw_key, admin_id
        )
    for key_id in targets.db_ids:
        db_result = await disable_provider_key(key_id, admin_id, op_store)
        pools_updated += db_result.pools_updated

    return ProviderKeyByRefResponse(
        provider=payload.provider,
        key_ref=payload.key_ref,
        source=targets.source,
        status="disabled",
        pools_updated=pools_updated,
    )


@router.post("/provider-keys/by-ref/enable", response_model=ProviderKeyByRefResponse)
async def enable_provider_key_by_ref(
    payload: ProviderKeyByRefRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ProviderKeyByRefResponse:
    """Re-enable one provider key identified by the quota dashboard's ``key_ref``.

    Clears the disabled state at every source holding the credential, so no
    stale tombstone or disabled row is left behind to strip the key from its
    pool at the next restart — or to render a second, disabled card for it.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    _validate_provider(payload.provider)
    targets = await _resolve_key_ref(op_store, payload.provider, payload.key_ref)
    if targets.all_active:
        return ProviderKeyByRefResponse(
            provider=payload.provider,
            key_ref=payload.key_ref,
            source=targets.source,
            status="active",
            pools_updated=0,
        )

    pools_updated = 0
    if targets.env_hash is not None and targets.env_disabled:
        pools_updated += await _clear_env_tombstone(
            op_store, payload.provider, targets.env_hash, admin_id
        )
    for key_id in targets.disabled_db_ids:
        db_result = await enable_provider_key(key_id, admin_id, op_store)
        pools_updated += db_result.pools_updated

    return ProviderKeyByRefResponse(
        provider=payload.provider,
        key_ref=payload.key_ref,
        source=targets.source,
        status="active",
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
    # The pool keeps one entry per raw value, so this row's reservation outlives
    # it when another source shares the value — re-derive the tier from what is
    # left instead of stranding a restriction nobody owns any more.
    # Drop this row's tier declaration either way: a stale one would re-apply if the
    # same credential is added back later. Only report it when the value survives
    # through another source, where the resolved tier is what the pool now enforces.
    await _refresh_db_key_tiers(op_store, provider)
    reconciled_min_role = dynamic_keys.resolve_key_min_role(provider, raw_key) if shared else None

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
            "reconciled_min_role": reconciled_min_role,
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

    # Re-enter at the tier the row was reserved for — re-enabling a pro-only key
    # must not quietly hand it back to every tier. The row counts as active again,
    # so refreshing before the attach is what makes its tier visible to the sweep.
    row_min_role = await op_store.get_provider_key_min_role(key_id)
    await _refresh_db_key_tiers(
        op_store,
        provider,
        fallback=(raw_key, row_min_role or "free"),
    )
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


@router.post("/provider-keys/{key_id}/min-role", response_model=SetProviderApiKeyMinRoleResponse)
async def set_provider_key_min_role(
    key_id: str,
    payload: SetProviderApiKeyMinRoleRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> SetProviderApiKeyMinRoleResponse:
    """Reserve a DB-sourced provider key for a tier (or release it back to all).

    ``min_role="free"`` un-reserves the key. Anything higher makes it invisible
    to callers below that role: they neither select it nor fall back to it, and a
    key reserved above every live user simply sits idle. Applied to the live
    pools immediately — no restart. Env-sourced keys go through
    ``POST /admin/provider-keys/min-role-env`` instead, since they are addressed
    by hash rather than by row id.
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    target = await op_store.get_provider_key_full(key_id)
    if target is None:
        raise HTTPException(404, f"Provider key {key_id!r} not found")
    provider, raw_key = target

    updated = await op_store.set_provider_key_min_role(key_id, payload.min_role)
    if not updated:
        raise HTTPException(404, f"Provider key {key_id!r} not found")

    await _refresh_db_key_tiers(op_store, provider, fallback=(raw_key, payload.min_role))
    pools_updated = dynamic_keys.pools_holding_key(provider, raw_key)

    await log_admin_action(
        op_store,
        admin_id,
        "set_provider_key_min_role",
        None,
        {
            "id": key_id,
            "provider": provider,
            "key_prefix": _mask(raw_key),
            "min_role": payload.min_role,
            "pools_updated": pools_updated,
        },
    )

    return SetProviderApiKeyMinRoleResponse(
        id=key_id,
        provider=provider,
        min_role=payload.min_role,
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

    # ``remove_key_from_provider`` no-ops on a value that was never DB-injected,
    # but a value shared by *another* still-active source (a second DB row with
    # the same value, or a non-disabled env key) must also be preserved —
    # otherwise deleting one duplicate evicts the credential the other source
    # still relies on. The row is already hard-deleted, so the active full list
    # no longer includes it; if the value survives there or as an active env
    # static key, leave it in the pool. (Mirrors the disable handler's guard.)
    try:
        active_after = set(await op_store.list_provider_keys_full(provider))
    except Exception:
        active_after = set()
    shared = raw_key in active_after or dynamic_keys.is_active_env_static_key(provider, raw_key)
    # When shared, leave both the pool entry and the ``_db_injected_keys``
    # bookkeeping intact: the surviving source still owns the value, and a later
    # delete of *that* row re-runs this same guard. The tier is re-derived from
    # the surviving sources, so this row's reservation leaves with the row.
    pools_updated = 0 if shared else dynamic_keys.remove_key_from_provider(provider, raw_key)
    # Drop this row's tier declaration either way: a stale one would re-apply if the
    # same credential is added back later. Only report it when the value survives
    # through another source, where the resolved tier is what the pool now enforces.
    await _refresh_db_key_tiers(op_store, provider)
    reconciled_min_role = dynamic_keys.resolve_key_min_role(provider, raw_key) if shared else None

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
            "reconciled_min_role": reconciled_min_role,
        },
    )

    return DeleteProviderApiKeyResponse(
        id=key_id,
        provider=provider,
        pools_updated=pools_updated,
    )
