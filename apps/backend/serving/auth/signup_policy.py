"""Public signup availability and domain-based approval policy.

An active manifest can disable public signup. Otherwise the runtime
``signup_enabled`` setting (falling back to the environment) decides whether
registration is available. The API and console share this decision.

The allowlist is stored in ``signup_allowed_domains``. Match rules:

- Exact rows match the email's domain case-insensitively after trim.
- Wildcard rows (``is_wildcard=TRUE``, stored without the ``*.`` prefix)
  match any subdomain of the stored suffix but **not** the bare suffix.

Empty allowlist semantics (``signup_allowlist_is_empty``) is the hot path
during signup: when no rows exist, every signup auto-approves and we want
to avoid the round-trip on each request. This module wraps the call in a
small in-process TTL cache. Mutating endpoints (admin add/remove) call
``invalidate_allowlist_cache`` to ensure the next signup observes the
change.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from serving.config.distribution import get_distribution_config
from serving.config.runtime_settings import get_runtime_settings_instance
from serving.config.settings import get_settings

if TYPE_CHECKING:
    from serving.storage.base import OperationalStore

# In-process cache for ``signup_allowlist_is_empty`` so the hot signup path
# avoids a DB round-trip when the feature is unused. ~30s TTL keeps
# stale-window short while still cushioning bursty signup traffic.
_ALLOWLIST_EMPTY_TTL_SECONDS: float = 30.0
_ALLOWLIST_EMPTY_CACHE: dict[str, tuple[float, bool]] = {}
_ALLOWLIST_EMPTY_LOCK = asyncio.Lock()


async def is_public_signup_enabled() -> bool:
    """Resolve signup availability for both registration and public site config.

    An explicit ``public_signup: false`` in an active manifest is a hard
    restriction; runtime or environment settings cannot reopen registration.
    Dark/absent manifests and true/null feature values preserve the existing
    runtime-over-environment signup policy.
    """
    settings = get_settings()
    if settings.distribution_config_mode.strip().lower() == "active":
        distribution = get_distribution_config()
        if distribution is not None and distribution.features.public_signup is False:
            return False

    try:
        runtime_settings = get_runtime_settings_instance()
    except RuntimeError:
        # Database-free deployments do not initialize the runtime store.
        return settings.signup_enabled
    return await runtime_settings.get_bool("signup_enabled")


def invalidate_allowlist_cache() -> None:
    """Drop the cached emptiness flag.

    Called by admin endpoints after a successful add/remove so the next
    signup observes the new state immediately.
    """
    _ALLOWLIST_EMPTY_CACHE.clear()


async def allowlist_is_empty(op_store: OperationalStore) -> bool:
    """Return True when no rows exist in ``signup_allowed_domains``.

    Cached in-process for ``_ALLOWLIST_EMPTY_TTL_SECONDS`` to keep the
    signup hot path off the DB when the allowlist is unused.
    """
    now = time.monotonic()
    cached = _ALLOWLIST_EMPTY_CACHE.get("v")
    if cached is not None and (now - cached[0]) < _ALLOWLIST_EMPTY_TTL_SECONDS:
        return cached[1]

    async with _ALLOWLIST_EMPTY_LOCK:
        # Re-check after acquiring the lock to coalesce concurrent callers.
        cached = _ALLOWLIST_EMPTY_CACHE.get("v")
        if cached is not None and (now - cached[0]) < _ALLOWLIST_EMPTY_TTL_SECONDS:
            return cached[1]
        is_empty = await op_store.signup_allowlist_is_empty()
        _ALLOWLIST_EMPTY_CACHE["v"] = (time.monotonic(), is_empty)
        return is_empty


async def is_domain_allowed(email: str, op_store: OperationalStore) -> bool:
    """True if *email*'s domain is on the allowlist (exact or wildcard).

    Wildcard rows match any subdomain of the stored suffix but not the
    bare suffix. Empty input or malformed email returns False.
    """
    return await op_store.is_signup_domain_allowed(email)
