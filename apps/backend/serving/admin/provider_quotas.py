"""Provider quota fetchers for the admin dashboard 'Providers' tab.

Each public fetcher returns a ``list[ProviderQuotaResult]`` — one entry per
configured API key.  When a provider has a single key the list contains one
element with ``key_index=None`` (backward compatible).  When multiple keys are
configured via numbered env var suffixes (e.g. ``ZAI_API_KEY2``) the list
contains one element per key with ``key_index=1, 2, …``.

Errors are converted to structured results — fetchers never raise out of the
gather.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from serving.adapters import dynamic_keys
from serving.admin.provider_key_probe import (
    ProviderKeyProbeError,
    probe_provider_key_with_existing_route,
)
from serving.config.settings import settings
from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8
_FEATHERLESS_CONCURRENCY_URL = "https://api.featherless.ai/account/concurrency"
_FEATHERLESS_FETCH_LOCK = asyncio.Lock()
_FEATHERLESS_CACHE_TTL_SECONDS = 15.0
_FEATHERLESS_FETCH_TASK: asyncio.Task[list[ProviderQuotaResult]] | None = None
_FEATHERLESS_FETCH_SIGNATURE: tuple[str, ...] | None = None
_FEATHERLESS_CACHE: tuple[float, tuple[str, ...], list[ProviderQuotaResult]] | None = None


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
    for i in range(2, _MAX_KEYS):
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
                keys.append((len(keys) + 1, key))
                seen.add(key)

    for pool in dynamic_keys.get_pools_for_provider(provider):
        for key in pool.snapshot_keys():
            if not key or key in seen or is_disabled_env_key(key):
                continue
            keys.append((len(keys) + 1, key))
            seen.add(key)

    return keys


def _process_multi_key_results(
    name: str,
    display_name: str,
    keys: list[tuple[int, str]],
    results: list[ProviderQuotaResult | BaseException],
) -> list[ProviderQuotaResult]:
    """Process parallel fetch results into a list of ProviderQuotaResult."""
    out: list[ProviderQuotaResult] = []
    multi = len(keys) > 1
    for (idx, _key), result in zip(keys, results, strict=True):
        if isinstance(result, ProviderQuotaResult):
            out.append(
                result.model_copy(
                    update={
                        "key_index": idx if multi else None,
                        "display_name": f"{display_name} #{idx}" if multi else display_name,
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


async def _fetch_chutes_for_key(key: str) -> ProviderQuotaResult:
    """Fetch quota usage from Chutes for a single API key."""
    url = "https://api.chutes.ai/users/me/subscription_usage"
    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308, 401, 403):
                    return _err("chutes", "Chutes", key, "auth_failed")
                if resp.status >= 400:
                    return _err("chutes", "Chutes", key, "unexpected")
                try:
                    data: dict[str, Any] = await resp.json()
                except Exception:
                    return _err("chutes", "Chutes", key, "parse_error")
            usages = _parse_chutes_usage(data)
            usages.extend(await _fetch_chutes_request_counts(session, headers))
    except asyncio.TimeoutError:
        return _err("chutes", "Chutes", key, "timeout")
    except aiohttp.ClientError:
        return _err("chutes", "Chutes", key, "unexpected")
    except Exception:
        logger.exception("fetch_chutes: unexpected error")
        return _err("chutes", "Chutes", key, "unexpected")

    return ProviderQuotaResult(
        name="chutes",
        display_name="Chutes",
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


async def fetch_chutes(operational_store: Any | None = None) -> list[ProviderQuotaResult]:
    """Fetch quota usage from Chutes for all configured API keys."""
    keys = await _discover_provider_keys(
        "chutes",
        "CHUTES_API_KEY",
        "CHUTES_API_KEY",
        operational_store,
    )
    if not keys:
        return [
            ProviderQuotaResult(
                name="chutes",
                display_name="Chutes",
                key_configured=False,
                key_masked=None,
                fetched_at=_now(),
                ok=False,
                error="not_configured",
                usages=[],
            )
        ]

    results = await asyncio.gather(
        *[_fetch_chutes_for_key(k) for _, k in keys],
        return_exceptions=True,
    )

    return _process_multi_key_results("chutes", "Chutes", keys, results)


async def _fetch_chutes_request_counts(
    session: aiohttp.ClientSession,
    headers: dict[str, str],
) -> list[ProviderQuotaUsage]:
    """Fetch the daily request quota and today's request count.

    Chutes enforces a per-day request cap (default 5000) exposed via
    /users/me/quotas; today's usage is the sum of `count` from
    /users/me/usage hourly buckets since 00:00 UTC. Returns an empty list
    on any HTTP/parse failure so the parent fetcher can still surface the
    USD usages.
    """
    try:
        daily_cap = await _fetch_chutes_daily_cap(session, headers)
        if daily_cap is None:
            return []

        now = _now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_reset = day_start + timedelta(days=1)

        url = "https://api.chutes.ai/users/me/usage?limit=2000"
        async with session.get(url, headers=headers, allow_redirects=False) as resp:
            if resp.status >= 400:
                return []
            try:
                payload: dict[str, Any] = await resp.json()
            except Exception:
                return []

        items = payload.get("items")
        if not isinstance(items, list):
            return []

        daily_count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            bucket_dt = _parse_iso(item.get("bucket"))
            if bucket_dt is None:
                continue
            count_raw = item.get("count")
            if isinstance(count_raw, bool) or not isinstance(count_raw, int | float):
                continue
            if not float(count_raw).is_integer():
                continue
            if day_start <= bucket_dt < day_reset:
                daily_count += int(count_raw)

        return [
            ProviderQuotaUsage(
                label="Daily requests",
                used=float(daily_count),
                limit=float(daily_cap),
                unit="requests",
                reset_at=day_reset,
            ),
        ]
    except Exception:
        logger.exception("_fetch_chutes_request_counts: unexpected error")
        return []


async def _fetch_chutes_daily_cap(
    session: aiohttp.ClientSession,
    headers: dict[str, str],
) -> int | None:
    """Return the default daily request cap from /users/me/quotas, or None on failure.

    The endpoint returns a list of per-chute quotas; we take the entry
    with `chute_id == "*"` (or `is_default == True`) as the global cap.
    """
    url = "https://api.chutes.ai/users/me/quotas"
    try:
        async with session.get(url, headers=headers, allow_redirects=False) as resp:
            if resp.status >= 400:
                return None
            try:
                payload = await resp.json()
            except Exception:
                return None
    except (asyncio.TimeoutError, aiohttp.ClientError):
        return None

    if not isinstance(payload, list):
        return None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if entry.get("chute_id") == "*" or entry.get("is_default") is True:
            quota = entry.get("quota")
            if isinstance(quota, int | float):
                return int(quota)
    return None


def _parse_chutes_usage(data: dict[str, Any]) -> list[ProviderQuotaUsage]:
    """Best-effort parse of the Chutes subscription_usage payload.

    The exact response schema is not formally documented; we look for
    common keys and degrade gracefully if missing.
    """
    usages: list[ProviderQuotaUsage] = []
    for key, label in (("four_hour", "4-hour window"), ("monthly", "Monthly")):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        used = block.get("usage")
        limit = block.get("cap")
        reset_dt = _parse_iso(block.get("reset_at"))
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used) if isinstance(used, int | float) else None,
                limit=float(limit) if isinstance(limit, int | float) else None,
                unit="USD",
                reset_at=reset_dt,
            )
        )
    return usages


async def _fetch_zai_for_key(key: str) -> ProviderQuotaResult:
    """Fetch quota for a single ZAI API key."""
    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, allow_redirects=False) as resp,
        ):
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                return _err("zai", "ZAI", key, "auth_failed")
            if resp.status >= 400:
                return _err("zai", "ZAI", key, "unexpected")
            try:
                data: dict[str, Any] = await resp.json()
            except Exception:
                return _err("zai", "ZAI", key, "parse_error")
    except asyncio.TimeoutError:
        return _err("zai", "ZAI", key, "timeout")
    except aiohttp.ClientError:
        return _err("zai", "ZAI", key, "unexpected")
    except Exception:
        logger.exception("fetch_zai: unexpected error")
        return _err("zai", "ZAI", key, "unexpected")

    body = data.get("data") if isinstance(data.get("data"), dict) else data
    limits = body.get("limits") if isinstance(body, dict) else None
    if not isinstance(limits, list):
        return _err("zai", "ZAI", key, "parse_error")

    usages: list[ProviderQuotaUsage] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", "")).upper()
        if kind == "TOKENS_LIMIT":
            label, unit = "Tokens", "tokens"
        elif kind == "TIME_LIMIT":
            label, unit = "Time", "minutes"
        else:
            label, unit = kind.replace("_", " ").title() or "Quota", ""

        entry_reset_at = _parse_epoch_ms(entry.get("nextResetTime"))
        used_raw = entry.get("currentValue") if "currentValue" in entry else entry.get("used")
        limit_raw = entry.get("usage")

        if used_raw is None and limit_raw is None and "percentage" in entry:
            pct = entry.get("percentage")
            usages.append(
                ProviderQuotaUsage(
                    label=label,
                    used=float(pct) if isinstance(pct, int | float) else None,
                    limit=100.0,
                    unit="%",
                    reset_at=entry_reset_at,
                )
            )
            continue

        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used_raw) if isinstance(used_raw, int | float) else None,
                limit=float(limit_raw) if isinstance(limit_raw, int | float) else None,
                unit=unit,
                reset_at=entry_reset_at,
            )
        )

    return ProviderQuotaResult(
        name="zai",
        display_name="ZAI",
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


async def fetch_zai(operational_store: Any | None = None) -> list[ProviderQuotaResult]:
    """Fetch quota usage from ZAI for all configured API keys.

    Endpoint discovered from ZAI's official ``glm-plan-usage`` plugin.
    """
    keys = await _discover_provider_keys(
        "zai",
        "ZAI_API_KEY",
        "ZAI_API_KEY",
        operational_store,
    )
    if not keys:
        return [
            ProviderQuotaResult(
                name="zai",
                display_name="ZAI",
                key_configured=False,
                key_masked=None,
                fetched_at=_now(),
                ok=False,
                error="not_configured",
                usages=[],
            )
        ]

    results = await asyncio.gather(
        *[_fetch_zai_for_key(k) for _, k in keys],
        return_exceptions=True,
    )

    return _process_multi_key_results("zai", "ZAI", keys, results)


async def _fetch_minimax_for_key(cookie: str) -> ProviderQuotaResult:
    """Fetch coding-plan quota for a single MiniMax session cookie."""
    url = "https://platform.minimax.io/v1/api/openplatform/coding_plan/remains"
    headers = {
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://platform.minimax.io/console/usage",
        "User-Agent": "Mozilla/5.0 (compatible; freeinference-admin/1.0)",
    }
    group_id = settings.minimax_group_id or _extract_cookie_value(cookie, "minimax_group_id_v2")
    if group_id:
        headers["x-group-id"] = group_id
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, allow_redirects=False) as resp,
        ):
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                return _err("minimax", "MiniMax", cookie, "auth_failed")
            if resp.status >= 400:
                return _err("minimax", "MiniMax", cookie, "unexpected")
            try:
                data: dict[str, Any] = await resp.json()
            except Exception:
                return _err("minimax", "MiniMax", cookie, "parse_error")
    except asyncio.TimeoutError:
        return _err("minimax", "MiniMax", cookie, "timeout")
    except aiohttp.ClientError:
        return _err("minimax", "MiniMax", cookie, "unexpected")
    except Exception:
        logger.exception("fetch_minimax: unexpected error")
        return _err("minimax", "MiniMax", cookie, "unexpected")

    return _minimax_result_from_payload(data, cookie)


def _minimax_result_from_payload(data: Any, key: str) -> ProviderQuotaResult:
    """Parse a MiniMax ``*_plan/remains`` payload into a quota result.

    Shared by the cookie-auth (``coding_plan/remains``) and API-key
    (``token_plan/remains``) fetchers; both endpoints return the same
    ``model_remains`` body shape. ``key`` is the cookie or API key, used only
    for masking in error results.
    """
    if not isinstance(data, dict):
        return _err("minimax", "MiniMax", key, "parse_error")

    base_resp = data.get("base_resp") if isinstance(data.get("base_resp"), dict) else None
    if base_resp and base_resp.get("status_code") == 1004:
        return _err("minimax", "MiniMax", key, "auth_failed")
    if base_resp and base_resp.get("status_code") == 2062:
        return _err("minimax", "MiniMax", key, "not_configured")
    if base_resp and base_resp.get("status_code") not in (None, 0):
        return _err("minimax", "MiniMax", key, "unexpected")

    body = data.get("data") if isinstance(data.get("data"), dict) else data
    model_remains = body.get("model_remains") if isinstance(body, dict) else None
    if not isinstance(model_remains, list) or not model_remains:
        return _err("minimax", "MiniMax", key, "parse_error")

    usages: list[ProviderQuotaUsage] = []
    for entry in model_remains:
        if not isinstance(entry, dict):
            continue
        model_name = str(entry.get("model_name", "Coding plan"))
        total = _first_float(
            entry,
            "current_interval_total_count",
            "current_interval_total_credits",
            "current_interval_total_credit",
            "total_count",
            "total_credits",
            "total_credit",
            "total",
            "limit",
        )
        remains = _first_float(
            entry,
            "current_interval_usage_count",
            "current_interval_remain_count",
            "current_interval_remaining_count",
            "current_interval_remains_credits",
            "current_interval_remaining_credits",
            "current_interval_remain_credit",
            "remain_count",
            "remaining_count",
            "remaining",
            "remain",
        )
        used = _first_float(
            entry,
            "current_interval_used_count",
            "current_interval_used_credits",
            "current_interval_used_credit",
            "used_count",
            "used_credits",
            "used_credit",
            "used",
        )
        remaining_percent = _first_float(
            entry,
            "current_interval_remaining_percent",
            "current_interval_remain_percent",
            "remain_percent",
            "remaining_percent",
        )
        end = entry.get("end_time")
        reset_dt = _parse_epoch_ms(end) or _parse_iso(end)
        used_val = used
        if used_val is None and total is not None and remains is not None:
            used_val = max(0.0, total - remains)
        unit = "credits" if _entry_has_credit_field(entry) else "requests"
        if (total is None or total <= 0) and remaining_percent is not None:
            total = 100.0
            used_val = max(0.0, 100.0 - remaining_percent)
            unit = "%"
        usages.append(
            ProviderQuotaUsage(
                label=f"{model_name} (interval)",
                used=used_val,
                limit=total,
                unit=unit,
                reset_at=reset_dt,
            )
        )
        weekly_total = _first_float(
            entry,
            "current_weekly_total_count",
            "current_weekly_total_credits",
            "current_weekly_total_credit",
            "weekly_total_count",
            "weekly_total_credits",
            "weekly_total_credit",
        )
        weekly_remains = _first_float(
            entry,
            "current_weekly_usage_count",
            "current_weekly_remain_count",
            "current_weekly_remaining_count",
            "current_weekly_remains_credits",
            "current_weekly_remaining_credits",
            "current_weekly_remain_credit",
            "weekly_remain_count",
            "weekly_remaining_count",
        )
        weekly_used = _first_float(
            entry,
            "current_weekly_used_count",
            "current_weekly_used_credits",
            "current_weekly_used_credit",
            "weekly_used_count",
            "weekly_used_credits",
            "weekly_used_credit",
        )
        weekly_unit = (
            "credits"
            if any("weekly" in key.lower() and "credit" in key.lower() for key in entry)
            else unit
        )
        weekly_remaining_percent = _first_float(
            entry,
            "current_weekly_remaining_percent",
            "current_weekly_remain_percent",
            "weekly_remaining_percent",
            "weekly_remain_percent",
        )
        if weekly_total is not None and weekly_total > 0:
            weekly_end = entry.get("weekly_end_time")
            weekly_reset_dt = _parse_epoch_ms(weekly_end) or _parse_iso(weekly_end)
            weekly_used_val = weekly_used
            if weekly_used_val is None and weekly_remains is not None:
                weekly_used_val = max(0.0, weekly_total - weekly_remains)
            usages.append(
                ProviderQuotaUsage(
                    label=f"{model_name} (weekly)",
                    used=weekly_used_val,
                    limit=weekly_total,
                    unit=weekly_unit,
                    reset_at=weekly_reset_dt,
                )
            )
        elif (weekly_total is None or weekly_total <= 0) and weekly_remaining_percent is not None:
            weekly_end = entry.get("weekly_end_time")
            usages.append(
                ProviderQuotaUsage(
                    label=f"{model_name} (weekly)",
                    used=max(0.0, 100.0 - weekly_remaining_percent),
                    limit=100.0,
                    unit="%",
                    reset_at=_parse_epoch_ms(weekly_end) or _parse_iso(weekly_end),
                )
            )

    if not usages:
        return _err("minimax", "MiniMax", key, "parse_error")

    return ProviderQuotaResult(
        name="minimax",
        display_name="MiniMax",
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


def _minimax_token_plan_url() -> str:
    """Resolve the API-key quota endpoint, honoring ``MINIMAX_BASE_URL``.

    Defaults to the documented global host. The base already includes the
    ``/v1`` prefix (e.g. ``https://api.minimax.io/v1``), matching the value
    used for inference requests.
    """
    base = (os.getenv("MINIMAX_BASE_URL") or "https://api.minimax.io/v1").rstrip("/")
    return f"{base}/token_plan/remains"


async def _fetch_minimax_via_api_key(key: str) -> ProviderQuotaResult:
    """Fetch token-plan quota for a single MiniMax API key.

    Uses the officially documented ``/token_plan/remains`` endpoint with
    ``Authorization: Bearer`` auth. Unlike the browser cookie endpoint this
    does not expire, avoiding the ``1004 "cookie is missing"`` auth failure.
    """
    url = _minimax_token_plan_url()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, allow_redirects=False) as resp,
        ):
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                return _err("minimax", "MiniMax", key, "auth_failed")
            if resp.status >= 400:
                return _err("minimax", "MiniMax", key, "unexpected")
            try:
                data: dict[str, Any] = await resp.json()
            except Exception:
                return _err("minimax", "MiniMax", key, "parse_error")
    except asyncio.TimeoutError:
        return _err("minimax", "MiniMax", key, "timeout")
    except aiohttp.ClientError:
        return _err("minimax", "MiniMax", key, "unexpected")
    except Exception:
        logger.exception("fetch_minimax: unexpected error")
        return _err("minimax", "MiniMax", key, "unexpected")

    return _minimax_result_from_payload(data, key)


async def fetch_minimax(operational_store: Any | None = None) -> list[ProviderQuotaResult]:
    """Fetch coding/token-plan quota from MiniMax for all configured keys.

    Prefers API-key auth (``MINIMAX_API_KEY``) against the documented
    ``/token_plan/remains`` endpoint, which does not expire. Falls back to the
    legacy browser session cookie when no API key is configured, or when the
    key is rejected (e.g. a standard pay-as-you-go key, which the token-plan
    endpoint does not accept) and a cookie is available.
    """
    cookie_keys = _discover_env_keys("MINIMAX_SESSION_COOKIE", "MINIMAX_SESSION_COOKIE")
    if not cookie_keys and settings.minimax_session_cookie:
        cookie_keys = [(1, settings.minimax_session_cookie)]

    api_keys = await _discover_provider_keys(
        "minimax",
        "MINIMAX_API_KEY",
        "MINIMAX_API_KEY",
        operational_store,
    )
    if api_keys:
        results = await asyncio.gather(
            *[_fetch_minimax_via_api_key(k) for _, k in api_keys],
            return_exceptions=True,
        )
        processed = _process_multi_key_results("minimax", "MiniMax", api_keys, results)
        # A standard PAYG key is rejected by the token-plan endpoint. Only fall
        # back to a configured cookie when every key failed auth, so a partial
        # success is never discarded.
        key_rejected = all(not r.ok and r.error == "auth_failed" for r in processed)
        if not (cookie_keys and key_rejected):
            return processed

    if not cookie_keys:
        return [
            ProviderQuotaResult(
                name="minimax",
                display_name="MiniMax",
                key_configured=False,
                key_masked=None,
                fetched_at=_now(),
                ok=False,
                error="not_configured",
                usages=[],
            )
        ]

    results = await asyncio.gather(
        *[_fetch_minimax_for_key(k) for _, k in cookie_keys],
        return_exceptions=True,
    )

    return _process_multi_key_results("minimax", "MiniMax", cookie_keys, results)


_USAGE_PATTERN = re.compile(
    r"(?P<label>session|weekly|monthly|daily)\s+usage\s+(?P<pct>[\d.]+)%\s+used",
    re.IGNORECASE,
)


async def _fetch_ollama_for_key(cookie: str) -> ProviderQuotaResult:
    """Scrape Ollama Cloud usage for a single session cookie."""
    url = "https://ollama.com/settings"
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (compatible; freeinference-admin/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, allow_redirects=False) as resp,
        ):
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                return _err("ollama", "Ollama Cloud", cookie, "auth_failed")
            if resp.status >= 400:
                return _err("ollama", "Ollama Cloud", cookie, "unexpected")
            html = await resp.text()
    except asyncio.TimeoutError:
        return _err("ollama", "Ollama Cloud", cookie, "timeout")
    except aiohttp.ClientError:
        return _err("ollama", "Ollama Cloud", cookie, "unexpected")
    except Exception:
        logger.exception("fetch_ollama: unexpected error")
        return _err("ollama", "Ollama Cloud", cookie, "unexpected")

    usages = _parse_ollama_html(html)
    if not usages:
        if "sign in" in html.lower() or "login" in html.lower():
            return _err("ollama", "Ollama Cloud", cookie, "auth_failed")
        return _err("ollama", "Ollama Cloud", cookie, "parse_error")

    return ProviderQuotaResult(
        name="ollama",
        display_name="Ollama Cloud",
        key_configured=True,
        key_masked=_mask_key(cookie),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


async def fetch_ollama(_operational_store: Any | None = None) -> list[ProviderQuotaResult]:
    """Scrape Ollama Cloud usage for all configured session cookies."""
    keys = _discover_env_keys("OLLAMA_SESSION_COOKIE", "OLLAMA_SESSION_COOKIE")
    if not keys:
        return [
            ProviderQuotaResult(
                name="ollama",
                display_name="Ollama Cloud",
                key_configured=False,
                key_masked=None,
                fetched_at=_now(),
                ok=False,
                error="not_configured",
                usages=[],
            )
        ]

    results = await asyncio.gather(
        *[_fetch_ollama_for_key(k) for _, k in keys],
        return_exceptions=True,
    )

    return _process_multi_key_results("ollama", "Ollama Cloud", keys, results)


def _parse_ollama_html(html: str) -> list[ProviderQuotaUsage]:
    """Best-effort extraction of usage figures from the Ollama settings page.

    Looks for text matches like 'Session usage 0% used'. Returns
    empty list if no recognizable usage rows found.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    usages: list[ProviderQuotaUsage] = []
    for match in _USAGE_PATTERN.finditer(text):
        try:
            pct = float(match.group("pct"))
        except ValueError:
            continue
        label = f"{match.group('label').capitalize()} usage"
        period = match.group("label").lower()
        reset_at = _next_reset(period)
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=pct,
                limit=100.0,
                unit="%",
                reset_at=reset_at,
            )
        )
    return usages


def _kimi_usages_url() -> str:
    """Resolve the Kimi coding-plan usage endpoint, honoring ``KIMI_CODING_BASE_URL``.

    The base already includes the ``/coding/v1`` prefix (e.g.
    ``https://api.kimi.com/coding/v1``), matching the value used for inference
    requests. The official Kimi Code CLI fetches quota from ``{base}/usages``.
    """
    base = (os.getenv("KIMI_CODING_BASE_URL") or "https://api.kimi.com/coding/v1").rstrip("/")
    return f"{base}/usages"


def _kimi_reset_at(data: dict[str, Any]) -> datetime | None:
    """Extract a reset time from a Kimi usage entry.

    Prefers absolute ISO timestamps (``reset_at``/``resetAt``/…); falls back to
    a relative ``reset_in``/``ttl`` count of seconds added to *now*.
    """
    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        parsed = _parse_iso(data.get(key))
        if parsed is not None:
            return parsed
    for key in ("reset_in", "resetIn", "ttl"):
        seconds = _as_float(data.get(key))
        if seconds is not None and seconds > 0:
            return _now() + timedelta(seconds=seconds)
    return None


def _kimi_limit_label(
    item: dict[str, Any],
    detail: dict[str, Any],
    window: dict[str, Any],
    idx: int,
) -> str:
    """Build a human-readable label for a Kimi ``limits`` entry.

    Mirrors the Kimi CLI: prefer an explicit name/title/scope, otherwise derive
    one from the window duration (e.g. 300 MINUTE -> "5h limit").
    """
    for key in ("name", "title", "scope"):
        val = item.get(key) or detail.get(key)
        if val:
            return str(val)

    duration = _as_float(window.get("duration") or item.get("duration") or detail.get("duration"))
    time_unit = str(
        window.get("timeUnit") or item.get("timeUnit") or detail.get("timeUnit") or ""
    ).upper()
    if duration:
        amount = int(duration)
        if "MINUTE" in time_unit:
            if amount >= 60 and amount % 60 == 0:
                return f"{amount // 60}h limit"
            return f"{amount}m limit"
        if "HOUR" in time_unit:
            return f"{amount}h limit"
        if "DAY" in time_unit:
            return f"{amount}d limit"
        return f"{amount}s limit"
    return f"Limit #{idx + 1}"


def _kimi_usage_row(data: dict[str, Any], default_label: str) -> ProviderQuotaUsage | None:
    """Parse a single Kimi usage object into a ``ProviderQuotaUsage``.

    Kimi reports request counts. ``used`` is derived from ``limit - remaining``
    when not given directly. Returns None when neither used nor limit is present.
    """
    limit = _first_float(data, "limit", "total", "quota", "max")
    used = _first_float(data, "used", "usage", "consumed")
    if used is None:
        remaining = _first_float(data, "remaining", "remain", "left")
        if remaining is not None and limit is not None:
            used = max(0.0, limit - remaining)
    if used is None and limit is None:
        return None
    label = str(data.get("name") or data.get("title") or data.get("scope") or default_label)
    return ProviderQuotaUsage(
        label=label,
        used=used,
        limit=limit,
        unit="requests",
        reset_at=_kimi_reset_at(data),
    )


def _parse_kimi_limits(limits: list[Any]) -> list[ProviderQuotaUsage]:
    """Parse a Kimi ``limits``/``usages`` array into quota usages.

    Each item may nest its figures under ``detail`` and its period under
    ``window``; bare items are parsed directly.
    """
    usages: list[ProviderQuotaUsage] = []
    for idx, item in enumerate(limits):
        if not isinstance(item, dict):
            continue
        detail = item.get("detail")
        detail = detail if isinstance(detail, dict) else item
        window = item.get("window")
        window = window if isinstance(window, dict) else {}
        label = _kimi_limit_label(item, detail, window, idx)
        row = _kimi_usage_row(detail, label)
        if row is not None:
            usages.append(row)
    return usages


def _parse_kimi_usage(payload: Any) -> list[ProviderQuotaUsage]:
    """Parse the Kimi ``/usages`` payload into a list of usages.

    The body has an optional ``usage`` summary plus a ``limits`` array; each
    limit may nest its figures under ``detail`` and its period under ``window``.
    The plural ``/usages`` endpoint can also return a bare array of limit
    objects, and some deployments wrap the body in a ``data``/``result``
    envelope (object *or* array), so we unwrap those before parsing.
    """
    # A bare array of limit/usage objects — the gateway's plural list response.
    if isinstance(payload, list):
        return _parse_kimi_limits(payload)
    if not isinstance(payload, dict):
        return []

    body = payload
    if not (
        isinstance(payload.get("usage"), dict)
        or isinstance(payload.get("limits"), list)
        or isinstance(payload.get("usages"), list)
    ):
        for envelope_key in ("data", "result"):
            inner = payload.get(envelope_key)
            if isinstance(inner, list):
                return _parse_kimi_limits(inner)
            if isinstance(inner, dict) and (
                isinstance(inner.get("usage"), dict)
                or isinstance(inner.get("limits"), list)
                or isinstance(inner.get("usages"), list)
            ):
                body = inner
                break

    usages: list[ProviderQuotaUsage] = []

    usage = body.get("usage")
    if isinstance(usage, dict):
        row = _kimi_usage_row(usage, "Weekly limit")
        if row is not None:
            usages.append(row)

    # Accept both ``limits`` and the plural ``usages`` array some responses use.
    limits = body.get("limits")
    if not isinstance(limits, list):
        limits = body.get("usages")
    if isinstance(limits, list):
        usages.extend(_parse_kimi_limits(limits))

    return usages


async def _fetch_kimi_for_key(key: str) -> ProviderQuotaResult:
    """Fetch coding-plan quota for a single Kimi API key."""
    url = _kimi_usages_url()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, allow_redirects=False) as resp,
        ):
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                return _err("kimi", "Kimi", key, "auth_failed")
            # The usage endpoint exists only for coding-plan keys; a plain
            # Moonshot API key gets a 404 here.
            if resp.status == 404:
                return _err("kimi", "Kimi", key, "no_quota_api")
            if resp.status >= 400:
                return _err("kimi", "Kimi", key, "unexpected")
            try:
                # Kimi's Connect/gRPC-gateway endpoint may serve the body with a
                # non-``application/json`` content type, which aiohttp rejects by
                # default; ``content_type=None`` parses it regardless.
                data: dict[str, Any] = await resp.json(content_type=None)
            except Exception:
                return _err("kimi", "Kimi", key, "parse_error")
    except asyncio.TimeoutError:
        return _err("kimi", "Kimi", key, "timeout")
    except aiohttp.ClientError:
        return _err("kimi", "Kimi", key, "unexpected")
    except Exception:
        logger.exception("fetch_kimi: unexpected error")
        return _err("kimi", "Kimi", key, "unexpected")

    if not isinstance(data, dict | list):
        return _err("kimi", "Kimi", key, "parse_error")
    usages = _parse_kimi_usage(data)
    if not usages:
        # Log the payload shape (structure only, no values) so an unexpected
        # schema can be diagnosed from server logs without leaking quota figures.
        if isinstance(data, dict):
            shape: Any = sorted(data.keys())
        else:
            shape = f"list[{len(data)}]"
        logger.warning(
            "fetch_kimi: no usages parsed",
            extra={"event": "kimi_quota_parse_empty", "payload_keys": shape},
        )
        return _err("kimi", "Kimi", key, "parse_error")

    return ProviderQuotaResult(
        name="kimi",
        display_name="Kimi",
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


async def fetch_kimi(operational_store: Any | None = None) -> list[ProviderQuotaResult]:
    """Fetch coding-plan quota from Kimi for all configured API keys.

    Endpoint discovered from the official Kimi Code CLI ``/usage`` command,
    which queries ``{base}/usages`` with ``Authorization: Bearer`` auth.
    """
    keys = await _discover_provider_keys(
        "kimi",
        "KIMI_CODING_API_KEY",
        "KIMI_CODING_API_KEY",
        operational_store,
    )
    if not keys:
        return [
            ProviderQuotaResult(
                name="kimi",
                display_name="Kimi",
                key_configured=False,
                key_masked=None,
                fetched_at=_now(),
                ok=False,
                error="not_configured",
                usages=[],
            )
        ]

    results = await asyncio.gather(
        *[_fetch_kimi_for_key(k) for _, k in keys],
        return_exceptions=True,
    )

    return _process_multi_key_results("kimi", "Kimi", keys, results)


async def gather_all(
    operational_store: Any | None = None,
    services: Any | None = None,
) -> list[ProviderQuotaResult]:
    """Run all provider fetchers in parallel; never raise.

    Each fetcher returns a ``list[ProviderQuotaResult]`` (one per key).
    Results are flattened into a single list. If a fetcher raises, the
    exception is caught and converted to a single error result.
    """
    fetchers = [
        ("chutes", "Chutes", fetch_chutes(operational_store)),
        ("zai", "ZAI", fetch_zai(operational_store)),
        ("minimax", "MiniMax", fetch_minimax(operational_store)),
        ("kimi", "Kimi", fetch_kimi(operational_store)),
        ("ollama", "Ollama Cloud", fetch_ollama(operational_store)),
        ("featherless", "Featherless", fetch_featherless(operational_store, services)),
    ]
    raw = await asyncio.gather(
        *(task for _, _, task in fetchers),
        return_exceptions=True,
    )
    out: list[ProviderQuotaResult] = []
    for (name, display_name, _), result in zip(fetchers, raw, strict=True):
        if isinstance(result, list):
            out.extend(result)
        else:
            logger.error("gather_all: %s fetcher raised", name, exc_info=result)
            out.append(
                ProviderQuotaResult(
                    name=name,
                    display_name=display_name,
                    key_configured=False,
                    key_masked=None,
                    fetched_at=_now(),
                    ok=False,
                    error="unexpected",
                    usages=[],
                )
            )
    return out


async def _fetch_featherless_for_key(
    key: str,
    services: Any | None,
) -> ProviderQuotaResult:
    """Probe whether a Featherless key works for the configured chat route."""
    if services is None:
        return _err("featherless", "Featherless", key, "probe_unavailable")
    try:
        await probe_provider_key_with_existing_route(
            services,
            provider="featherless",
            api_key=key,
            timeout_seconds=_TIMEOUT_SECONDS,
        )
    except ProviderKeyProbeError as exc:
        usage = await _fetch_featherless_concurrency_usage(key)
        return ProviderQuotaResult(
            name="featherless",
            display_name="Featherless",
            key_configured=True,
            key_masked=_mask_key(key),
            fetched_at=_now(),
            ok=False,
            error=exc.reason,
            usages=[usage] if usage is not None else [],
        )

    usage = await _fetch_featherless_concurrency_usage(key)
    return ProviderQuotaResult(
        name="featherless",
        display_name="Featherless",
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=[usage] if usage is not None else [],
    )


async def _fetch_featherless_concurrency_usage(key: str) -> ProviderQuotaUsage | None:
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                _FEATHERLESS_CONCURRENCY_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
                allow_redirects=False,
            ) as resp,
        ):
            if resp.status >= 400:
                return None
            data = await resp.json()
    except Exception as exc:
        logger.debug("fetch_featherless: concurrency check failed: %s", exc)
        return None

    if not isinstance(data, dict):
        return None
    used = _as_float(data.get("used_cost"))
    limit = _as_float(data.get("limit"))
    if used is None and limit is None:
        return None
    return ProviderQuotaUsage(
        label="Concurrency",
        used=used,
        limit=limit,
        unit="units",
        reset_at=None,
    )


def _clone_provider_quota_results(
    results: list[ProviderQuotaResult],
) -> list[ProviderQuotaResult]:
    return [result.model_copy(deep=True) for result in results]


async def _fetch_featherless_for_keys(
    keys: list[tuple[int, str]],
    services: Any | None,
) -> list[ProviderQuotaResult]:
    raw = await asyncio.gather(
        *[_fetch_featherless_for_key(key, services) for _idx, key in keys],
        return_exceptions=True,
    )
    for result in raw:
        if isinstance(result, asyncio.CancelledError):
            raise result
    return _process_multi_key_results("featherless", "Featherless", keys, raw)


async def fetch_featherless(
    operational_store: Any | None = None,
    services: Any | None = None,
) -> list[ProviderQuotaResult]:
    """Show configured Featherless keys and probe concurrency-based availability."""
    global _FEATHERLESS_CACHE, _FEATHERLESS_FETCH_SIGNATURE, _FEATHERLESS_FETCH_TASK

    keys = await _discover_provider_keys(
        "featherless",
        "FEATHERLESS_API_KEY",
        "FEATHERLESS_API_KEY",
        operational_store,
    )
    if not keys:
        return [
            ProviderQuotaResult(
                name="featherless",
                display_name="Featherless",
                key_configured=False,
                key_masked=None,
                fetched_at=_now(),
                ok=False,
                error="not_configured",
                usages=[],
            )
        ]

    signature = tuple(key for _idx, key in keys)
    now = asyncio.get_running_loop().time()
    async with _FEATHERLESS_FETCH_LOCK:
        if (
            _FEATHERLESS_CACHE is not None
            and _FEATHERLESS_CACHE[1] == signature
            and now - _FEATHERLESS_CACHE[0] <= _FEATHERLESS_CACHE_TTL_SECONDS
        ):
            return _clone_provider_quota_results(_FEATHERLESS_CACHE[2])

        if (
            _FEATHERLESS_FETCH_TASK is None
            or _FEATHERLESS_FETCH_TASK.done()
            or signature != _FEATHERLESS_FETCH_SIGNATURE
        ):
            _FEATHERLESS_FETCH_TASK = asyncio.create_task(
                _fetch_featherless_for_keys(keys, services)
            )
            _FEATHERLESS_FETCH_SIGNATURE = signature
        task = _FEATHERLESS_FETCH_TASK

    try:
        results = await task
    except Exception:
        async with _FEATHERLESS_FETCH_LOCK:
            if _FEATHERLESS_FETCH_TASK is task:
                _FEATHERLESS_FETCH_TASK = None
                _FEATHERLESS_FETCH_SIGNATURE = None
        raise

    async with _FEATHERLESS_FETCH_LOCK:
        if _FEATHERLESS_FETCH_TASK is task:
            _FEATHERLESS_CACHE = (asyncio.get_running_loop().time(), signature, results)
            _FEATHERLESS_FETCH_TASK = None
            _FEATHERLESS_FETCH_SIGNATURE = None
    return _clone_provider_quota_results(results)
