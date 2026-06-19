"""Runtime registry of provider adapters with key pools.

Each adapter that supports multi-key rotation registers itself here at boot
so the admin endpoints can look up every ``KeyPool`` for a given upstream
provider and add or remove keys at runtime without restarting the process.
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.adapters.key_pool import KeyPool
    from serving.storage.base import OperationalStore

logger = get_logger(__name__)

_lock = threading.Lock()
_adapters_by_provider: dict[str, list] = {}
_db_injection_disabled_adapter_ids: dict[str, set[int]] = {}
_known_providers: set[str] = set()
# Tracks raw key values that were injected from the DB (per provider).
# Used by ``remove_key_from_provider`` to ensure we never tombstone an
# env-configured key that happens to share its raw value with a deleted DB row.
_db_injected_keys: dict[str, set[str]] = {}
_disabled_env_key_hashes: dict[str, set[str]] = {}


def reset() -> None:
    """Clear the registry. Test helper — not used in production paths."""
    with _lock:
        _adapters_by_provider.clear()
        _db_injection_disabled_adapter_ids.clear()
        _known_providers.clear()
        _db_injected_keys.clear()
        _disabled_env_key_hashes.clear()


def register_adapter_for_provider(
    provider: str,
    adapter: object,
    *,
    allow_db_key_injection: bool = True,
) -> None:
    """Register an adapter under *provider* so its KeyPool can be located later.

    Adapters without a key pool (single-key configurations) may still be
    registered: the admin endpoint will skip them when no pool is present.
    """
    with _lock:
        bucket = _adapters_by_provider.setdefault(provider, [])
        if adapter not in bucket:
            bucket.append(adapter)
        disabled = _db_injection_disabled_adapter_ids.setdefault(provider, set())
        if allow_db_key_injection:
            disabled.discard(id(adapter))
        else:
            disabled.add(id(adapter))
        _known_providers.add(provider)


def register_known_provider(provider: str) -> None:
    """Mark *provider* as a valid whitelist entry without an adapter."""
    with _lock:
        _known_providers.add(provider)


def get_known_providers() -> set[str]:
    """Return the set of providers seen during model registration."""
    with _lock:
        return set(_known_providers)


def _pools_for_provider_locked(
    provider: str,
    *,
    include_db_injection_disabled: bool = True,
) -> list[KeyPool]:
    pools: list[KeyPool] = []
    disabled_ids = _db_injection_disabled_adapter_ids.get(provider, set())
    for adapter in _adapters_by_provider.get(provider, []):
        if not include_db_injection_disabled and id(adapter) in disabled_ids:
            continue
        pool = getattr(adapter, "_key_pool", None)
        if pool is not None:
            pools.append(pool)
    return pools


def get_pools_for_provider(
    provider: str,
    *,
    include_db_injection_disabled: bool = True,
) -> list[KeyPool]:
    """Return the live KeyPool instances configured for *provider*."""
    with _lock:
        return _pools_for_provider_locked(
            provider,
            include_db_injection_disabled=include_db_injection_disabled,
        )


def is_env_key_disabled(provider: str, key_hash: str) -> bool:
    """Return True when an env-sourced key hash has been disabled."""
    with _lock:
        return key_hash in _disabled_env_key_hashes.get(provider, set())


def env_key_hash(key: str) -> str:
    """Return the stable hash used to identify env-sourced provider keys."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def disable_env_key_for_provider(provider: str, key: str, key_hash: str) -> int:
    """Disable an env-sourced key in all live pools for *provider*.

    The hash is tracked so the admin list view can continue filtering the key
    after the raw key has been removed from the pools.
    """
    with _lock:
        _disabled_env_key_hashes.setdefault(provider, set()).add(key_hash)
        pools = _pools_for_provider_locked(provider)
        return sum(1 for pool in pools if pool.remove_key(key))


def add_key_to_provider(provider: str, key: str) -> int:
    """Append *key* to every KeyPool registered for *provider*.

    Returns the number of pools the key was added to. A return value of 0
    means *provider* has no multi-key adapters — the caller should treat
    this as a configuration error and surface it to the admin.

    The key is tracked as DB-injected so a future ``remove_key_from_provider``
    call can distinguish it from env-configured keys that happen to share
    the same raw value.
    """
    with _lock:
        pools = _pools_for_provider_locked(provider, include_db_injection_disabled=False)
        for pool in pools:
            pool.add_key(key)
        _db_injected_keys.setdefault(provider, set()).add(key)
        return len(pools)


def mark_db_key_for_provider(provider: str, key: str) -> None:
    """Track *key* as DB-sourced without injecting it into existing pools."""
    with _lock:
        _db_injected_keys.setdefault(provider, set()).add(key)


def remove_key_from_provider(provider: str, key: str) -> int:
    """Remove *key* from every KeyPool registered for *provider*.

    Only removes the key when it was previously injected via
    ``add_key_to_provider``. Env-configured keys with the same raw value are
    left in place so deleting a DB row that duplicates an env key does not
    tombstone the env key.

    Returns the number of pools that actually held the key.
    """
    with _lock:
        injected = _db_injected_keys.get(provider, set())
        if key not in injected:
            return 0
        injected.discard(key)
        if not injected:
            _db_injected_keys.pop(provider, None)
        pools = _pools_for_provider_locked(provider)
        return sum(1 for pool in pools if pool.remove_key(key))


def _is_db_provider_key_id(key_id: object) -> bool:
    return isinstance(key_id, str) and bool(key_id) and not key_id.startswith("env:")


async def _list_route_bound_db_key_ids(operational_store: OperationalStore) -> set[str]:
    """Return DB provider-key ids reserved by provider route configs."""
    key_ids: set[str] = set()
    for rows in (
        await operational_store.list_all_provider_route_configs(),
        await operational_store.list_all_provider_route_candidates(),
    ):
        for row in rows:
            key_id = row.get("api_key_id")
            if _is_db_provider_key_id(key_id):
                key_ids.add(key_id)
    return key_ids


async def apply_db_keys_at_boot(operational_store: OperationalStore) -> None:
    """Pull persisted provider keys and seed each registered adapter's pool.

    Called once during application bootstrap after the model registry has
    been loaded. Failures for one provider do not affect the others.
    """
    with _lock:
        providers = list(_known_providers)

    try:
        route_bound_key_ids = await _list_route_bound_db_key_ids(operational_store)
    except Exception as exc:
        logger.warning(
            "dynamic_keys: failed to load route-bound provider key ids; "
            "skipping DB key boot seeding to avoid global key leakage: %s",
            exc,
        )
        return

    for provider in providers:
        try:
            disabled_hashes = await operational_store.list_disabled_provider_env_key_hashes(
                provider
            )
        except Exception as exc:
            logger.warning(
                "dynamic_keys: failed to load disabled env keys for provider=%s: %s",
                provider,
                exc,
            )
            disabled_hashes = set()
        if disabled_hashes:
            with _lock:
                _disabled_env_key_hashes[provider] = set(disabled_hashes)
            for pool in get_pools_for_provider(provider):
                for key in pool.snapshot_keys():
                    if env_key_hash(key) in disabled_hashes:
                        pool.remove_key(key)

        try:
            keys = await operational_store.list_provider_keys_full(
                provider,
                exclude_ids=route_bound_key_ids,
            )
        except Exception as exc:
            logger.warning(
                "dynamic_keys: failed to load DB keys for provider=%s: %s",
                provider,
                exc,
            )
            continue
        if not keys:
            continue
        added = 0
        for key in keys:
            added += add_key_to_provider(provider, key)
        if added:
            logger.info(
                "dynamic_keys: seeded %d DB key(s) into provider=%s pools",
                len(keys),
                provider,
            )
