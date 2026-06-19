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
        _known_providers.clear()
        _db_injected_keys.clear()
        _disabled_env_key_hashes.clear()


def register_adapter_for_provider(provider: str, adapter: object) -> None:
    """Register an adapter under *provider* so its KeyPool can be located later.

    Adapters without a key pool (single-key configurations) may still be
    registered: the admin endpoint will skip them when no pool is present.
    """
    with _lock:
        bucket = _adapters_by_provider.setdefault(provider, [])
        if adapter not in bucket:
            bucket.append(adapter)
        _known_providers.add(provider)


def register_known_provider(provider: str) -> None:
    """Mark *provider* as a valid whitelist entry without an adapter."""
    with _lock:
        _known_providers.add(provider)


def get_known_providers() -> set[str]:
    """Return the set of providers seen during model registration."""
    with _lock:
        return set(_known_providers)


def _pools_for_provider_locked(provider: str) -> list[KeyPool]:
    pools: list[KeyPool] = []
    for adapter in _adapters_by_provider.get(provider, []):
        pool = getattr(adapter, "_key_pool", None)
        if pool is not None:
            pools.append(pool)
    return pools


def get_pools_for_provider(provider: str) -> list[KeyPool]:
    """Return the live KeyPool instances configured for *provider*."""
    with _lock:
        return _pools_for_provider_locked(provider)


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


def _attach_key_to_adapter_locked(
    adapter: object, key: str, disabled_hashes: set[str]
) -> bool:
    """Attach *key* to a single adapter, promoting it to a pool if needed.

    Pool-capable adapters (``add_runtime_key``) lazily create a ``KeyPool``
    seeded with their original static key, so a runtime key is used even when
    the route was configured with a single ``api_key``. Adapters that already
    expose a pool but predate ``add_runtime_key`` fall back to ``add_key``.
    Returns True when the key was attached.

    Promotion re-seeds the adapter's static ``api_key`` into the new pool. If
    an admin has disabled that env key (tracked by hash), it must not silently
    come back into rotation — so any disabled env key is dropped from the pool
    afterwards. The key being added is never dropped, even if its hash matches.
    """
    pool = getattr(adapter, "_key_pool", None)
    if pool is not None:
        pool.add_key(key)
        attached = True
    else:
        attach = getattr(adapter, "add_runtime_key", None)
        if not callable(attach):
            return False
        attached = bool(attach(key))
        pool = getattr(adapter, "_key_pool", None)
    if attached and pool is not None and disabled_hashes:
        for existing in pool.snapshot_keys():
            if existing != key and env_key_hash(existing) in disabled_hashes:
                pool.remove_key(existing)
    return attached


def add_key_to_provider(provider: str, key: str) -> int:
    """Attach *key* to every pool-capable adapter registered for *provider*.

    Returns the number of adapters the key was attached to. A return value of
    0 means *provider* has no multi-key-capable adapters — the caller should
    treat this as a configuration error and surface it to the admin, since the
    key has been persisted but will never be used for inference.

    Adapters configured with a single ``api_key`` are promoted to a pool on
    first runtime key (seeded with the original key), so dashboard-added keys
    are used without requiring the route to pre-declare ``api_keys``.

    The key is tracked as DB-injected so a future ``remove_key_from_provider``
    call can distinguish it from env-configured keys that happen to share
    the same raw value.
    """
    with _lock:
        adapters = _adapters_by_provider.get(provider, [])
        disabled = _disabled_env_key_hashes.get(provider, set())
        attached = sum(
            1 for adapter in adapters if _attach_key_to_adapter_locked(adapter, key, disabled)
        )
        _db_injected_keys.setdefault(provider, set()).add(key)
        return attached


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


async def apply_db_keys_at_boot(operational_store: OperationalStore) -> None:
    """Pull persisted provider keys and seed each registered adapter's pool.

    Called once during application bootstrap after the model registry has
    been loaded. Failures for one provider do not affect the others.
    """
    with _lock:
        providers = list(_known_providers)

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
            keys = await operational_store.list_provider_keys_full(provider)
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
