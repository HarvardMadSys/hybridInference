"""Runtime registry of provider adapters with key pools.

Each adapter that supports multi-key rotation registers itself here at boot
so the admin endpoints can look up every ``KeyPool`` for a given upstream
provider and add or remove keys at runtime without restarting the process.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
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
_MAX_NUMBERED_ENV_KEYS = 20
_PROVIDER_ENV_KEY_VARS: dict[str, tuple[str, str]] = {
    "chutes": ("CHUTES_API_KEY", "CHUTES_API_KEY"),
    "featherless": ("FEATHERLESS_API_KEY", "FEATHERLESS_API_KEY"),
    "kimi": ("KIMI_CODING_API_KEY", "KIMI_CODING_API_KEY"),
    "minimax": ("MINIMAX_API_KEY", "MINIMAX_API_KEY"),
    "ollama": ("OLLAMA_API_KEY", "OLLAMA_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    "zai": ("ZAI_API_KEY", "ZAI_API_KEY"),
}
_KEY_PROVIDER_ALIASES = {
    "kimi_coding": "kimi",
}


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


def unregister_adapter_for_provider(provider: str, adapter: object) -> None:
    """Remove one adapter registration for rollback paths."""
    with _lock:
        bucket = _adapters_by_provider.get(provider)
        if bucket is not None:
            with contextlib.suppress(ValueError):
                bucket.remove(adapter)
            if not bucket:
                _adapters_by_provider.pop(provider, None)
        disabled = _db_injection_disabled_adapter_ids.get(provider)
        if disabled is not None:
            disabled.discard(id(adapter))
            if not disabled:
                _db_injection_disabled_adapter_ids.pop(provider, None)


def register_known_provider(provider: str) -> None:
    """Mark *provider* as a valid whitelist entry without an adapter."""
    with _lock:
        _known_providers.add(provider)


def unregister_known_provider(provider: str) -> bool:
    """Remove a whitelist-only provider.

    Providers with live adapters are left registered because model routes still
    depend on them. Returns True when the provider was removed from the known
    provider set.
    """
    with _lock:
        if _adapters_by_provider.get(provider):
            return False
        before = provider in _known_providers
        _known_providers.discard(provider)
        _db_injected_keys.pop(provider, None)
        _disabled_env_key_hashes.pop(provider, None)
        return before


def get_known_providers() -> set[str]:
    """Return the set of providers seen during model registration."""
    with _lock:
        return set(_known_providers)


def get_registered_base_urls(provider: str) -> list[str]:
    """Return base URLs observed on live adapters for *provider*."""
    with _lock:
        urls: list[str] = []
        for adapter in _adapters_by_provider.get(provider, []):
            cfg = getattr(adapter, "config", None)
            base_url = str(getattr(cfg, "base_url", "") or "").strip()
            if base_url and base_url not in urls:
                urls.append(base_url)
        return urls


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


def list_candidate_env_keys(provider: str) -> list[str]:
    """Return raw keys that may be env-sourced and manageable for *provider*.

    Union of (a) all keys currently in live pools and (b) the static
    ``api_key`` / ``api_keys`` of adapters not yet promoted to a pool — so a
    legacy single-``api_key`` route's env credential is surfaced for the admin
    list and ``disable-env`` even though it has no pool yet. The caller filters
    out DB-injected and already-disabled keys. Order is stable (deduped).
    """
    with _lock:
        out: list[str] = []
        for pool in _pools_for_provider_locked(provider):
            for k in pool.snapshot_keys():
                if k not in out:
                    out.append(k)
        for adapter in _adapters_by_provider.get(provider, []):
            if getattr(adapter, "_key_pool", None) is not None:
                continue
            for k in _gather_static_keys(adapter):
                if k not in out:
                    out.append(k)
        return out


def is_active_env_static_key(provider: str, key: str) -> bool:
    """Return True when *key* is an env-configured static key still in use.

    Used to avoid evicting a raw value from the pool when disabling a DB row
    that happens to share its value with a (non-disabled) env-sourced key.
    """
    with _lock:
        if env_key_hash(key) in _disabled_env_key_hashes.get(provider, set()):
            return False
        return any(
            key in _gather_static_keys(adapter)
            for adapter in _adapters_by_provider.get(provider, [])
        )


def env_key_hash(key: str) -> str:
    """Return the stable hash used to identify env-sourced provider keys."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize_key_provider(provider: str) -> str:
    """Return the provider name used for shared API-key management."""
    return _KEY_PROVIDER_ALIASES.get(provider, provider)


def configured_env_keys_for_provider(provider: str) -> list[str]:
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


def disable_env_key_for_provider(provider: str, key: str, key_hash: str) -> int:
    """Disable an env-sourced key for *provider* and remove it from rotation.

    The hash is tracked so the admin list view can continue filtering the key
    after the raw key has been removed from the pools. Pool-less single-key
    adapters whose static key is being disabled are promoted to a pool first,
    otherwise the legacy single-key request path would keep serving the
    disabled key. Returns the number of pools the key was removed from.
    """
    with _lock:
        _disabled_env_key_hashes.setdefault(provider, set()).add(key_hash)
        updated = 0
        for adapter in _adapters_by_provider.get(provider, []):
            pool = getattr(adapter, "_key_pool", None)
            if pool is None:
                if key not in _gather_static_keys(adapter):
                    continue
                ensure = getattr(adapter, "ensure_key_pool", None)
                if not callable(ensure):
                    continue
                pool = ensure()
                if pool is None:
                    continue
            if pool.remove_key(key):
                updated += 1
        return updated


def _attach_key_to_adapter_locked(adapter: object, key: str, disabled_hashes: set[str]) -> bool:
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


def remove_key_from_pools(provider: str, key: str) -> int:
    """Remove *key* from every live pool for *provider*, regardless of source.

    Used by the DB-key disable toggle: the DB row's ``status`` is the source of
    truth for whether the key is enabled, so (unlike the env-key path) no hash
    tombstone is recorded here. Returns the number of pools the key left.
    """
    with _lock:
        pools = _pools_for_provider_locked(provider)
        return sum(1 for pool in pools if pool.remove_key(key))


def _gather_static_keys(adapter: object) -> list[str]:
    """Return the env-sourced static keys an adapter was configured with."""
    cfg = getattr(adapter, "config", None)
    if cfg is None:
        return []
    out: list[str] = []
    ak = getattr(cfg, "api_key", None)
    if isinstance(ak, str) and ak.strip():
        out.append(ak.strip())
    aks = getattr(cfg, "api_keys", None)
    if aks:
        out.extend(k.strip() for k in aks if isinstance(k, str) and k.strip())
    return out


def _find_env_key_by_hash_locked(provider: str, key_hash: str) -> str | None:
    """Recover a raw env-sourced key for *provider* whose hash matches.

    Env keys are not stored in the DB (only a tombstone hash + prefix), so to
    re-enable one we recover the raw value from a registered adapter's static
    config (``api_key`` / ``api_keys``).
    """
    for adapter in _adapters_by_provider.get(provider, []):
        for raw in _gather_static_keys(adapter):
            if env_key_hash(raw) == key_hash:
                return raw
    return None


def enforce_disabled_static_keys(provider: str, disabled_hashes: set[str]) -> None:
    """Ensure disabled env keys are never served via the legacy single-key path.

    A single-``api_key`` adapter with no pool serves ``config.api_key``
    directly, bypassing tombstones. When *provider* has disabled-env-key
    hashes, promote any such adapter whose static key is disabled to a pool and
    drop the disabled key — so e.g. an add-key → disable-env → delete-key
    history cannot resurrect the disabled env key after a restart. Adapters
    that already have a pool simply have their disabled keys removed.
    """
    if not disabled_hashes:
        return
    with _lock:
        for adapter in _adapters_by_provider.get(provider, []):
            pool = getattr(adapter, "_key_pool", None)
            if pool is None:
                statics = _gather_static_keys(adapter)
                if not any(env_key_hash(k) in disabled_hashes for k in statics):
                    continue
                ensure = getattr(adapter, "ensure_key_pool", None)
                if not callable(ensure):
                    continue
                pool = ensure()
                if pool is None:
                    continue
            for existing in pool.snapshot_keys():
                if env_key_hash(existing) in disabled_hashes:
                    pool.remove_key(existing)


def enable_env_key_for_provider(provider: str, key_hash: str) -> int:
    """Re-enable a disabled env key: clear the tombstone and re-add to pools.

    Clears the in-memory disabled-hash set and re-injects the raw key
    (recovered from adapter config) into the pools of the adapters that were
    actually configured with it. Re-adding only to *owning* adapters avoids
    leaking a model-scoped credential into unrelated routes/models under the
    same provider (which could fail auth or mix quotas). Returns the number of
    pools updated; 0 when the raw key can no longer be recovered (e.g. the env
    var was removed since it was disabled).
    """
    with _lock:
        disabled = _disabled_env_key_hashes.get(provider)
        if disabled is not None:
            disabled.discard(key_hash)
        raw = _find_env_key_by_hash_locked(provider, key_hash)
        if raw is None:
            return 0
        updated = 0
        for adapter in _adapters_by_provider.get(provider, []):
            if raw not in _gather_static_keys(adapter):
                continue
            pool = getattr(adapter, "_key_pool", None)
            if pool is None:
                ensure = getattr(adapter, "ensure_key_pool", None)
                if not callable(ensure):
                    continue
                pool = ensure()
                if pool is None:
                    continue
            pool.add_key(raw)
            updated += 1
        return updated


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
        # Route-bound adapters opt out of global DB-key injection — a DB key
        # added for the whole provider must not land on a route reserved for a
        # specific key (mirrors ``_pools_for_provider_locked(..., False)``).
        disabled_ids = _db_injection_disabled_adapter_ids.get(provider, set())
        disabled = _disabled_env_key_hashes.get(provider, set())
        attached = sum(
            1
            for adapter in adapters
            if id(adapter) not in disabled_ids
            and _attach_key_to_adapter_locked(adapter, key, disabled)
        )
        _db_injected_keys.setdefault(provider, set()).add(key)
        return attached


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
            # Promote pool-less single-key adapters whose static key is disabled
            # and strip disabled keys from every pool, so the legacy single-key
            # path can never serve a tombstoned env key after a restart.
            enforce_disabled_static_keys(provider, set(disabled_hashes))

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
