"""Runtime registry of provider adapters with key pools.

Each adapter that supports multi-key rotation registers itself here at boot
so the admin endpoints can look up every ``KeyPool`` for a given upstream
provider and add or remove keys at runtime without restarting the process.
"""

from __future__ import annotations

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


def reset() -> None:
    """Clear the registry. Test helper — not used in production paths."""
    with _lock:
        _adapters_by_provider.clear()
        _known_providers.clear()


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


def add_key_to_provider(provider: str, key: str) -> int:
    """Append *key* to every KeyPool registered for *provider*.

    Returns the number of pools the key was added to. A return value of 0
    means *provider* has no multi-key adapters — the caller should treat
    this as a configuration error and surface it to the admin.
    """
    with _lock:
        pools = _pools_for_provider_locked(provider)
        for pool in pools:
            pool.add_key(key)
        return len(pools)


def remove_key_from_provider(provider: str, key: str) -> int:
    """Remove *key* from every KeyPool registered for *provider*.

    Returns the number of pools that actually held the key.
    """
    with _lock:
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
