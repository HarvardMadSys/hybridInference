"""Runtime registry of provider adapters with key pools.

Each adapter that supports multi-key rotation registers itself here at boot
so the admin endpoints can look up every ``KeyPool`` for a given upstream
provider and add or remove keys at runtime without restarting the process.

This module is also the single authority on which *tier* each pooled key is
reserved for. Pools are built from adapter config, which carries no tier, and are
rebuilt whenever a route is installed or a single-key adapter is promoted — so a
reservation attached at one call site is a reservation that silently disappears at
the next. Instead the declarations live here (``_env_key_min_roles`` /
``_db_key_min_roles``), ``_resolve_min_role_locked`` derives the tier a pool entry
must enforce, and ``_apply_min_roles_locked`` reconciles the live pools. Every
path that can create a pool or change a declaration ends in that sweep.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
from typing import TYPE_CHECKING

from serving.adapters.key_pool import DEFAULT_MIN_ROLE
from serving.config.settings import ROLE_RANK
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
# Tier declarations, mirrored in memory so a pool can be tiered without a DB read
# (pools are created inside locks, and runtime route installs create them from a
# provider's whole key set). Env keys have no row of their own, so — like the
# disable tombstones above — theirs are addressed by hash; DB rows are addressed
# by raw value, because that is what a pool entry is keyed on. Absent means "no
# reservation declared". ``_resolve_min_role_locked`` combines them into the one
# tier a pool entry enforces; ``_apply_min_roles_locked`` writes it.
_env_key_min_roles: dict[str, dict[str, str]] = {}
_db_key_min_roles: dict[str, dict[str, str]] = {}
_MAX_NUMBERED_ENV_KEYS = 20
_PROVIDER_ENV_KEY_VARS: dict[str, tuple[str, str]] = {
    "chutes": ("CHUTES_API_KEY", "CHUTES_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    "featherless": ("FEATHERLESS_API_KEY", "FEATHERLESS_API_KEY"),
    "kimi": ("KIMI_CODING_API_KEY", "KIMI_CODING_API_KEY"),
    "minimax": ("MINIMAX_API_KEY", "MINIMAX_API_KEY"),
    "ollama": ("OLLAMA_API_KEY", "OLLAMA_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    "staging": ("STAGING_API_KEY", "STAGING_API_KEY"),
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
        _env_key_min_roles.clear()
        _db_key_min_roles.clear()


def register_adapter_for_provider(
    provider: str,
    adapter: object,
    *,
    allow_db_key_injection: bool = True,
) -> None:
    """Register an adapter under *provider* so its KeyPool can be located later.

    Adapters without a key pool (single-key configurations) may still be
    registered: the admin endpoint will skip them when no pool is present.

    Registration is where stored tier reservations are applied, because it is the
    one point every adapter passes through — the boot registry load and each
    runtime route install (``_install_route_update``, ``_install_route_candidate``,
    ``_install_provider_route_model``). A runtime route with no pinned key builds
    its pool from the provider's whole key set, all of it untiered, so without this
    a newly created or replaced route would serve every reserved key to every tier
    until the next restart.
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
        # Route-bound adapters are excluded from global DB *key injection*, not
        # from tiering: the credential they hold is the same secret, and skipping
        # it would leave a reserved key spendable by anyone on that route.
        _apply_min_roles_locked(provider)
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


def env_key_min_role(provider: str, key_hash: str) -> str:
    """Return the tier an env key is reserved for (``"free"`` when unreserved)."""
    with _lock:
        return _env_key_min_roles.get(provider, {}).get(key_hash, DEFAULT_MIN_ROLE)


def get_env_key_min_roles(provider: str) -> dict[str, str]:
    """Return ``{key_hash: min_role}`` for the provider's reserved env keys."""
    with _lock:
        return dict(_env_key_min_roles.get(provider, {}))


def _resolve_min_role_locked(provider: str, raw_key: str) -> str:
    """Return the tier the pool must enforce for *raw_key*.

    A pool holds one ``_KeyState`` per raw value, but the same credential can be
    declared twice — an env var plus a DB row, or two DB rows — so the tier has to
    be *derived* from every declaration rather than written by whichever code path
    touched the pool last. This is that single derivation.

    ``"free"`` is the absence of a declaration, not an assertion that everyone may
    spend the key, so unreserved sources contribute nothing. Among real
    declarations the most restrictive wins: reservation exists to hold capacity
    back, so a conflict resolves in favor of protecting it, and the answer does not
    depend on the order the sources were configured. Releasing a key back to every
    tier means clearing its declaration, which is exactly what the min-role
    endpoints do.
    """
    declared = [
        role
        for role in (
            _db_key_min_roles.get(provider, {}).get(raw_key),
            _env_key_min_roles.get(provider, {}).get(env_key_hash(raw_key)),
        )
        if role is not None and role != DEFAULT_MIN_ROLE
    ]
    if not declared:
        return DEFAULT_MIN_ROLE
    return max(declared, key=lambda role: ROLE_RANK.get(role, 0))


def resolve_key_min_role(provider: str, raw_key: str) -> str:
    """Return the tier the live pools enforce for *raw_key* (``"free"`` if none).

    The admin list view reports this for env keys so the displayed tier is the one
    actually enforced, even when a duplicate DB row declares something else.
    """
    with _lock:
        return _resolve_min_role_locked(provider, raw_key)


def _apply_min_roles_locked(provider: str) -> int:
    """Reconcile every live pool of *provider* with the resolved tiers.

    Called from every path that can create a pool or change a declaration —
    adapter registration, key add/remove, a re-tier, boot seeding — because a pool
    is seeded from adapter config (which carries no tier) and re-seeded on every
    promotion. Anything that attaches a key and forgets to reconcile leaves a
    reserved credential serving every tier until the next restart.

    A pool-less single-``api_key`` adapter is promoted first when a reservation
    applies to its static key: it otherwise serves that credential through the
    legacy request path, which never consults a pool, so the reservation would be
    silently ignored. Promotion is skipped when nothing is reserved, leaving the
    cheaper legacy path in place for the overwhelmingly common case. (Same move
    the disable-env path makes for tombstoned keys.)

    Returns the number of pool entries whose tier changed.
    """
    updated = 0
    for adapter in _adapters_by_provider.get(provider, []):
        pool = getattr(adapter, "_key_pool", None)
        if pool is None:
            statics = _gather_static_keys(adapter)
            if not any(_resolve_min_role_locked(provider, k) != DEFAULT_MIN_ROLE for k in statics):
                continue
            ensure = getattr(adapter, "ensure_key_pool", None)
            if not callable(ensure):
                logger.warning(
                    "dynamic_keys: provider=%s has a reserved static key on an adapter that "
                    "cannot be promoted to a key pool; the reservation is NOT enforced there",
                    provider,
                )
                continue
            pool = ensure()
            if pool is None:
                continue
        current = pool.snapshot_min_roles()
        for raw, role in current.items():
            resolved = _resolve_min_role_locked(provider, raw)
            if resolved != role and pool.set_key_min_role(raw, resolved):
                updated += 1
    return updated


def apply_min_roles(provider: str) -> int:
    """Public wrapper for the tier reconciliation sweep. Returns entries changed."""
    with _lock:
        return _apply_min_roles_locked(provider)


def _pools_holding_hash_locked(provider: str, key_hash: str) -> int:
    """Count live pools holding a key whose hash is *key_hash*."""
    return sum(
        1
        for pool in _pools_for_provider_locked(provider)
        if any(env_key_hash(raw) == key_hash for raw in pool.snapshot_keys())
    )


def set_env_key_min_role(provider: str, key_hash: str, min_role: str) -> int:
    """Record an env key's reservation and reconcile the live pools.

    Tracked by hash so the reservation survives the raw key leaving the pool
    (disabled, or its env var removed) and is re-applied when it comes back.
    Returns the number of live pools holding that key — 0 is normal for a key that
    is currently disabled or served by an adapter that has no pool yet.
    """
    with _lock:
        _record_declaration_locked(_env_key_min_roles, provider, key_hash, min_role)
        _apply_min_roles_locked(provider)
        return _pools_holding_hash_locked(provider, key_hash)


def pools_holding_key(provider: str, raw_key: str) -> int:
    """Count live pools of *provider* holding *raw_key*."""
    with _lock:
        return sum(
            1 for pool in _pools_for_provider_locked(provider) if raw_key in pool.snapshot_keys()
        )


def _record_declaration_locked(
    store: dict[str, dict[str, str]],
    provider: str,
    key: str,
    min_role: str,
) -> None:
    """Set or clear one tier declaration. ``"free"`` clears it (see the resolver)."""
    bucket = store.setdefault(provider, {})
    if min_role == DEFAULT_MIN_ROLE:
        bucket.pop(key, None)
    else:
        bucket[key] = min_role
    if not bucket:
        store.pop(provider, None)


def load_env_key_min_roles(provider: str, roles: dict[str, str]) -> None:
    """Seed the in-memory env reservations for *provider* from persisted rows."""
    with _lock:
        if roles:
            _env_key_min_roles[provider] = dict(roles)
        else:
            _env_key_min_roles.pop(provider, None)
        _apply_min_roles_locked(provider)


def load_db_key_min_roles(provider: str, roles: dict[str, str]) -> None:
    """Replace the cached DB-row declarations (``{raw_key: min_role}``) and reconcile.

    The ``provider_api_keys`` table is the authority; this is its in-process cache,
    refreshed from ``list_provider_key_min_roles`` at boot and after every admin
    mutation. Caching matters because a pool can be created without a DB read in
    reach — a runtime route install builds one from the provider's whole key set
    inside the registry lock — and because two rows can declare tiers for the same
    raw value, which only the store can collapse correctly.

    Whole-map replacement, not a merge: a row that stopped declaring a tier (or
    stopped existing) has to disappear from the cache, or its reservation would
    outlive it.
    """
    with _lock:
        declared = {k: v for k, v in roles.items() if v and v != DEFAULT_MIN_ROLE}
        if declared:
            _db_key_min_roles[provider] = declared
        else:
            _db_key_min_roles.pop(provider, None)
        _apply_min_roles_locked(provider)


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
    keys = [base_value] if base_value else []
    start_index = 2 if base_value else 1
    for index in range(start_index, _MAX_NUMBERED_ENV_KEYS + 1):
        value = os.getenv(f"{numbered_prefix}{index}", "")
        if not value:
            break
        if value not in keys:
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
        # Promotion above re-seeds the adapter's other statics as unreserved.
        _apply_min_roles_locked(provider)
        return updated


def _attach_key_to_adapter_locked(
    adapter: object,
    key: str,
    disabled_hashes: set[str],
) -> bool:
    """Attach *key* to a single adapter, promoting it to a pool if needed.

    Pool-capable adapters (``add_runtime_key``) lazily create a ``KeyPool``
    seeded with their original static key, so a runtime key is used even when
    the route was configured with a single ``api_key``. Adapters that already
    expose a pool but predate ``add_runtime_key`` fall back to ``add_key``.
    Returns True when the key was attached.

    Deliberately tier-blind: the key lands unreserved and the caller's
    ``_apply_min_roles_locked`` sweep sets the resolved tier. Writing a tier here
    as well made the outcome depend on write order — attaching a DB key whose
    value duplicates a reserved env key applied the row's tier and then had the
    env reservation overwrite it, so the API reported one tier while the pool
    enforced another.

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


def find_env_key_by_hash(provider: str, key_hash: str) -> str | None:
    """Return the raw env key of *provider* whose full hash matches, if known.

    Env keys live only in adapter config, so this is the one way back from a
    stored hash to the value a pool holds. None when the value is no longer
    recoverable (e.g. the env var was removed since the hash was recorded).
    """
    with _lock:
        return _find_env_key_by_hash_locked(provider, key_hash)


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
        # Any pool created above starts unreserved; restore stored reservations.
        _apply_min_roles_locked(provider)


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
        # The sweep restores the tier this key was reserved for: the reservation is
        # tracked by hash precisely so a disable/enable cycle cannot demote it.
        _apply_min_roles_locked(provider)
        return updated


def add_key_to_provider(provider: str, key: str, min_role: str | None = None) -> int:
    """Attach *key* to every pool-capable adapter registered for *provider*.

    Returns the number of adapters the key was attached to. A return value of
    0 means *provider* has no multi-key-capable adapters — the caller should
    treat this as a configuration error and surface it to the admin, since the
    key has been persisted but will never be used for inference.

    Adapters configured with a single ``api_key`` are promoted to a pool on
    first runtime key (seeded with the original key), so dashboard-added keys
    are used without requiring the route to pre-declare ``api_keys``.

    ``min_role`` records the DB row's tier declaration for *key*; None leaves any
    existing declaration alone (a re-add). The tier the pool ends up enforcing is
    always the resolved one — another source declaring a stricter tier for the same
    raw value wins, so a duplicate cannot be quietly widened.

    The key is tracked as DB-injected so a future ``remove_key_from_provider``
    call can distinguish it from env-configured keys that happen to share
    the same raw value.
    """
    with _lock:
        if min_role is not None:
            _record_declaration_locked(_db_key_min_roles, provider, key, min_role)
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
        # Attaching leaves keys untiered and may have promoted a single-``api_key``
        # adapter to a pool, re-seeding its static as untiered too. One sweep sets
        # every entry to its resolved tier.
        _apply_min_roles_locked(provider)
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

    # Tier declarations first, and outside the route-bound guard below: pools were
    # already seeded from adapter config at registry load, so a DB fault that skips
    # DB-key seeding must not also leave a reserved key serving every tier. Loading
    # them here also means a route installed later — which builds its pool from the
    # provider's whole key set — can be tiered from memory, with no DB read inside
    # the registry lock.
    for provider in providers:
        try:
            env_min_roles = await operational_store.list_provider_env_key_min_roles(provider)
        except Exception as exc:
            logger.warning(
                "dynamic_keys: failed to load env key tier reservations for provider=%s; "
                "env keys stay unreserved: %s",
                provider,
                exc,
            )
        else:
            if env_min_roles:
                load_env_key_min_roles(provider, env_min_roles)
                logger.info(
                    "dynamic_keys: applied %d env key tier reservation(s) for provider=%s",
                    len(env_min_roles),
                    provider,
                )
        try:
            db_min_roles = await operational_store.list_provider_key_min_roles(provider)
        except Exception as exc:
            logger.warning(
                "dynamic_keys: failed to load DB key tier reservations for provider=%s; "
                "those keys stay unreserved: %s",
                provider,
                exc,
            )
            continue
        reserved = {k: v for k, v in db_min_roles.items() if v and v != DEFAULT_MIN_ROLE}
        if reserved:
            load_db_key_min_roles(provider, reserved)
            logger.info(
                "dynamic_keys: applied %d DB key tier reservation(s) for provider=%s",
                len(reserved),
                provider,
            )

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
        # No tier argument: the declarations were loaded above, and every
        # ``add_key_to_provider`` ends in the reconciliation sweep that applies them.
        added = 0
        for key in keys:
            added += add_key_to_provider(provider, key)
        if added:
            logger.info(
                "dynamic_keys: seeded %d DB key(s) into provider=%s pools",
                len(keys),
                provider,
            )
