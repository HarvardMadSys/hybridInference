"""Provider quota reporting for the admin dashboard's Providers tab.

The gateway ships the framework — key discovery, per-key result shaping, the
disabled-key cards and ``gather_all`` — and no fetchers. Which vendors' account
or usage endpoints get read is a property of a deployment, so a backend
extension registers one fetcher per provider through ``register_quota_fetcher``
at startup (see the developer docs on backend extensions). RouteWise quota
sources resolve through the same registry.

A fetcher is awaited as ``fetch(operational_store, services)`` and returns a
``list[ProviderQuotaResult]`` — one entry per configured API key. When a
provider has a single key the list contains one element with
``key_index=None``; with numbered env var suffixes (e.g. ``ZAI_API_KEY2``) it
contains one element per key with ``key_index=1, 2, …``. Fetchers convert
their own errors into structured results and never raise out of the gather.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any

from serving.adapters import dynamic_keys
from serving.schemas_admin import ProviderQuotaResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8


def _mask_key(key: str) -> str:
    """Mask an API key or cookie for display.

    Returns first 8 + '...' + last 4 if key is at least 16 chars; otherwise
    returns a generic placeholder so we never leak short secrets.
    """
    if len(key) >= 16:
        return f"{key[:8]}...{key[-4:]}"
    return "***configured***"


_MAX_KEYS = 20


def _discover_env_keys(base_var: str, numbered_prefix: str) -> list[tuple[int, str]]:
    """Discover all configured API keys via numbered env var suffixes.

    Returns list of ``(index, value)``.  ``index=1`` for *base_var*,
    ``index=N`` for ``{numbered_prefix}{N}``.  Numbered suffixes are
    only scanned when the base var is set.  Stops at the first missing
    numbered var.
    """
    keys: list[tuple[int, str]] = []
    val = os.getenv(base_var, "")
    if not val:
        return keys
    keys.append((1, val))
    for i in range(2, _MAX_KEYS + 1):
        val = os.getenv(f"{numbered_prefix}{i}", "")
        if not val:
            break
        keys.append((i, val))
    return keys


async def _discover_provider_keys(
    provider: str,
    base_var: str,
    numbered_prefix: str,
    operational_store: Any | None = None,
) -> list[tuple[int, str]]:
    """Discover configured keys from env, DB, and live adapter key pools."""
    disabled_hashes: set[str] = set()
    route_bound_key_ids: set[str] | None = set()
    if operational_store is not None:
        try:
            disabled_hashes = set(
                await operational_store.list_disabled_provider_env_key_hashes(provider)
            )
        except Exception as exc:
            logger.warning("failed to load disabled env keys for provider=%s: %s", provider, exc)

        try:
            route_bound_key_ids = await dynamic_keys._list_route_bound_db_key_ids(operational_store)
        except Exception as exc:
            logger.warning(
                "failed to load route-bound provider key ids for provider=%s; "
                "skipping DB keys to avoid global key leakage: %s",
                provider,
                exc,
            )
            route_bound_key_ids = None

    def is_disabled_env_key(key: str) -> bool:
        key_hash = dynamic_keys.env_key_hash(key)
        return key_hash in disabled_hashes or dynamic_keys.is_env_key_disabled(provider, key_hash)

    keys = [
        (index, key)
        for index, key in _discover_env_keys(base_var, numbered_prefix)
        if not is_disabled_env_key(key)
    ]
    seen = {key for _, key in keys}
    # Env keys keep their env-var suffix number, which an operator can map back
    # to ZAI_API_KEY2 and which therefore must not be renumbered. Filtering
    # disabled keys punches holes in that sequence, so `len(keys) + 1` is not a
    # free index: with ZAI_API_KEY disabled, `keys` is [(2, ...)] -- length 1 --
    # and the next append would collide on 2, producing two keys with the same
    # index and the same display name. Allocate past the highest suffix in use.
    next_index = max((index for index, _ in keys), default=0) + 1

    if operational_store is not None and route_bound_key_ids is not None:
        try:
            db_keys = await operational_store.list_provider_keys_full(
                provider,
                exclude_ids=route_bound_key_ids,
            )
        except Exception as exc:
            logger.warning("failed to load DB keys for provider=%s: %s", provider, exc)
        else:
            for key in db_keys:
                if not key or key in seen:
                    continue
                keys.append((next_index, key))
                next_index += 1
                seen.add(key)

    for pool in dynamic_keys.get_pools_for_provider(
        provider,
        include_db_injection_disabled=False,
    ):
        for key in pool.snapshot_keys():
            if not key or key in seen or is_disabled_env_key(key):
                continue
            keys.append((next_index, key))
            next_index += 1
            seen.add(key)

    return keys


def key_ref(raw_key: str) -> str:
    """Return the opaque per-key handle used by the admin dashboard.

    Derived from the raw credential so a key discovered by a quota fetcher can
    be matched back to its DB row or env var by
    ``/admin/provider-keys/by-ref/{disable,enable}`` without the dashboard ever
    seeing the secret. Shares the truncated-hash form of the env key ids
    exposed by ``/admin/provider-keys`` (``env:{hash[:32]}``).
    """
    return dynamic_keys.env_key_hash(raw_key)[:32]


def _process_multi_key_results(
    name: str,
    display_name: str,
    keys: list[tuple[int, str]],
    results: list[ProviderQuotaResult | BaseException],
    *,
    manageable: bool = True,
) -> list[ProviderQuotaResult]:
    """Process parallel fetch results into a list of ProviderQuotaResult.

    ``manageable`` marks the credentials as API keys the admin provider-key
    endpoints can enable/disable; pass False for session cookies, which have no
    such handle.
    """
    out: list[ProviderQuotaResult] = []
    multi = len(keys) > 1
    for (idx, _key), result in zip(keys, results, strict=True):
        if isinstance(result, ProviderQuotaResult):
            out.append(
                result.model_copy(
                    update={
                        "key_index": idx if multi else None,
                        "display_name": f"{display_name} #{idx}" if multi else display_name,
                        "key_ref": key_ref(_key) if manageable else None,
                    }
                )
            )
        else:
            logger.error("fetch_%s: key #%d raised", name, idx, exc_info=result)
            out.append(
                ProviderQuotaResult(
                    name=name,
                    display_name=f"{display_name} #{idx}" if multi else display_name,
                    key_index=idx if multi else None,
                    key_configured=True,
                    key_masked=_mask_key(_key),
                    fetched_at=_now(),
                    ok=False,
                    error="unexpected",
                    usages=[],
                    key_ref=key_ref(_key) if manageable else None,
                )
            )
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_reset(period: str, now: datetime | None = None) -> datetime:
    """Compute the next reset datetime in UTC for a given period.

    Supported periods: daily, weekly, monthly, session.
    - daily → next midnight UTC
    - weekly → next Monday 00:00 UTC
    - monthly → 1st of next month 00:00 UTC
    - session → next midnight UTC (same as daily)
    """
    now = now or _now()
    period = period.lower().strip()
    if period == "weekly":
        days_until_monday = (7 - now.weekday()) % 7 or 7
        return (now + timedelta(days=days_until_monday)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    if period == "monthly":
        if now.month == 12:
            return now.replace(
                year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    day_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return day_reset


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string into a UTC-aware datetime; None on failure.

    Naive inputs are assumed UTC. Offset-aware inputs are converted to UTC so
    callers always see a single canonical timezone.
    """
    if not isinstance(value, str):
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_epoch_ms(value: Any) -> datetime | None:
    """Parse a Unix epoch milliseconds value (int/float) into a UTC datetime; None on failure."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Convert a JSON number to float while rejecting bool and non-finite values.

    Numeric strings (e.g. ``"1200"``) are also accepted: some providers — notably
    Kimi's ``/usages`` endpoint — return quota figures as strings rather than
    JSON numbers. Blank/non-numeric strings and ``inf``/``nan`` yield ``None``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        converted = float(value)
        return converted if math.isfinite(converted) else None
    if isinstance(value, str):
        try:
            converted = float(value.strip())
        except ValueError:
            return None
        return converted if math.isfinite(converted) else None
    return None


def _first_float(data: dict[str, Any], *keys: str) -> float | None:
    """Return the first numeric value among possible response-field aliases."""
    for key in keys:
        converted = _as_float(data.get(key))
        if converted is not None:
            return converted
    return None


def _extract_cookie_value(cookie: str, name: str) -> str:
    """Extract a single cookie value from a browser Cookie header string."""
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except Exception:
        return ""
    morsel = parsed.get(name)
    return morsel.value if morsel is not None else ""


def _entry_has_credit_field(entry: dict[str, Any]) -> bool:
    """Detect MiniMax's newer credit-based quota fields."""
    return any("credit" in key.lower() for key in entry)


def _err(name: str, display_name: str, key: str, reason: str) -> ProviderQuotaResult:
    return ProviderQuotaResult(
        name=name,
        display_name=display_name,
        key_configured=bool(key),
        key_masked=_mask_key(key) if key else None,
        fetched_at=_now(),
        ok=False,
        error=reason,
        usages=[],
    )


# ---------------------------------------------------------------- fetcher registry

QuotaFetcher = Callable[[Any, Any], Awaitable[list[ProviderQuotaResult]]]


@dataclass(frozen=True)
class QuotaFetcherSpec:
    """One provider's quota fetcher, as registered by a backend extension."""

    provider: str
    display_name: str
    fetch: QuotaFetcher


_FETCHERS: dict[str, QuotaFetcherSpec] = {}


def register_quota_fetcher(
    provider: str,
    display_name: str,
    fetch: QuotaFetcher,
    *,
    override: bool = False,
) -> None:
    """Register the fetcher that reports *provider*'s quota.

    Called from a backend extension's ``register()``. ``fetch`` is awaited as
    ``fetch(operational_store, services)`` and returns one result per
    configured key; it must turn its own failures into results rather than
    raise. Registering a provider twice is an error unless ``override`` is
    set, so two extensions cannot silently fight over one tile.
    """
    slug = provider.strip()
    if not slug:
        raise ValueError("quota fetcher provider must not be blank")
    if slug in _FETCHERS and not override:
        raise ValueError(f"a quota fetcher for {slug!r} is already registered")
    _FETCHERS[slug] = QuotaFetcherSpec(
        provider=slug,
        display_name=display_name.strip() or slug,
        fetch=fetch,
    )


def registered_quota_fetchers() -> list[QuotaFetcherSpec]:
    """Return the registered fetchers in registration order."""
    return list(_FETCHERS.values())


def quota_fetcher(provider: str) -> QuotaFetcherSpec | None:
    """Return the fetcher registered for *provider*, if any."""
    return _FETCHERS.get(provider)


def reset_quota_fetchers() -> None:
    """Forget every registered fetcher. Test helper — not used in production paths."""
    _FETCHERS.clear()


def _disabled_key_result(
    provider: str,
    display_name: str,
    key_masked: str,
    key_ref_value: str,
) -> ProviderQuotaResult:
    """Build the card shown for a key an admin has disabled."""
    return ProviderQuotaResult(
        name=provider,
        display_name=display_name,
        key_configured=True,
        key_masked=key_masked,
        fetched_at=None,
        ok=False,
        error="key_disabled",
        usages=[],
        key_ref=key_ref_value,
        key_disabled=True,
    )


async def _disabled_keys_for_provider(
    operational_store: Any,
    provider: str,
    display_name: str,
) -> list[ProviderQuotaResult]:
    """Return cards for every disabled key of *provider* (env and DB alike).

    Disabled keys are filtered out of key discovery, so without this they would
    silently vanish from the dashboard with no way to turn them back on.
    """
    out: list[ProviderQuotaResult] = []

    try:
        tombstones = await operational_store.list_disabled_provider_env_keys(provider)
    except Exception as exc:
        logger.warning("failed to load disabled env keys for provider=%s: %s", provider, exc)
        tombstones = []
    for key_hash, key_prefix in tombstones:
        out.append(_disabled_key_result(provider, display_name, key_prefix, key_hash[:32]))

    try:
        rows = await operational_store.list_provider_keys(provider)
    except Exception as exc:
        logger.warning("failed to load DB keys for provider=%s: %s", provider, exc)
        rows = []
    for row in rows:
        if row.status != "disabled":
            continue
        try:
            full = await operational_store.get_provider_key_full(row.id)
        except Exception as exc:
            logger.warning("failed to load DB key %s for provider=%s: %s", row.id, provider, exc)
            continue
        if full is None:
            continue
        out.append(_disabled_key_result(provider, display_name, row.key_prefix, key_ref(full[1])))

    return out


async def gather_all(
    operational_store: Any | None = None,
    services: Any | None = None,
) -> list[ProviderQuotaResult]:
    """Run every registered fetcher in parallel; never raise.

    Each fetcher returns a ``list[ProviderQuotaResult]`` (one per key).
    Results are flattened into a single list, followed by a card per
    admin-disabled key so the dashboard can re-enable it. If a fetcher raises,
    the exception is caught and converted to a single error result. With no
    fetcher registered the list is empty: the tab has nothing to report on
    until a backend extension supplies one.

    One credential yields at most one card. The same raw key can be recorded in
    several places (an env var and a DB row, or several DB rows), so a disabled
    record is dropped when a live card already covers that ``key_ref`` — the
    dashboard would otherwise show two cards for one key.
    """
    specs = registered_quota_fetchers()
    raw = await asyncio.gather(
        *(spec.fetch(operational_store, services) for spec in specs),
        return_exceptions=True,
    )

    out: list[ProviderQuotaResult] = []
    for spec, result in zip(specs, raw, strict=True):
        if isinstance(result, list):
            out.extend(result)
        else:
            logger.error("gather_all: %s fetcher raised", spec.provider, exc_info=result)
            out.append(
                ProviderQuotaResult(
                    name=spec.provider,
                    display_name=spec.display_name,
                    key_configured=False,
                    key_masked=None,
                    fetched_at=_now(),
                    ok=False,
                    error="unexpected",
                    usages=[],
                )
            )

    if operational_store is not None:
        seen_refs = {(r.name, r.key_ref) for r in out if r.key_ref is not None}
        for spec in specs:
            for card in await _disabled_keys_for_provider(
                operational_store, spec.provider, spec.display_name
            ):
                ref = (card.name, card.key_ref)
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                out.append(card)
    return out
