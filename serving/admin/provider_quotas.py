"""Provider quota fetchers for the admin dashboard 'Providers' tab.

Each public fetcher returns a `ProviderQuotaResult`. Errors are converted
to structured results — fetchers never raise out of the gather.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


async def fetch_chutes() -> ProviderQuotaResult:
    """Fetch quota usage from Chutes via /users/me/subscription_usage."""
    key = os.getenv("CHUTES_API_KEY", "")
    if not key:
        return ProviderQuotaResult(
            name="chutes",
            display_name="Chutes",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

    url = "https://api.chutes.ai/users/me/subscription_usage"
    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (401, 403):
                    return _err("chutes", "Chutes", key, "auth_failed")
                if resp.status >= 400:
                    return _err("chutes", "Chutes", key, "unexpected")
                try:
                    data: dict[str, Any] = await resp.json()
                except Exception:
                    return _err("chutes", "Chutes", key, "parse_error")
    except asyncio.TimeoutError:
        return _err("chutes", "Chutes", key, "timeout")
    except aiohttp.ClientError:
        return _err("chutes", "Chutes", key, "unexpected")
    except Exception:
        logger.exception("fetch_chutes: unexpected error")
        return _err("chutes", "Chutes", key, "unexpected")

    usages = _parse_chutes_usage(data)
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


def _parse_chutes_usage(data: dict[str, Any]) -> list[ProviderQuotaUsage]:
    """Best-effort parse of the Chutes subscription_usage payload.

    The exact response schema is not formally documented; we look for
    common keys and degrade gracefully if missing.
    """
    usages: list[ProviderQuotaUsage] = []
    for key, label_default in (("monthly", "Monthly"), ("rolling", "Rolling window")):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        used = block.get("used")
        limit = block.get("limit")
        unit = block.get("unit", "USD")
        window = block.get("window")
        label = f"{label_default} ({window})" if window else label_default
        reset = block.get("reset_at")
        reset_dt = None
        if isinstance(reset, str):
            try:
                reset_dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            except ValueError:
                reset_dt = None
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used) if isinstance(used, (int, float)) else None,
                limit=float(limit) if isinstance(limit, (int, float)) else None,
                unit=str(unit),
                reset_at=reset_dt,
            )
        )
    return usages


async def fetch_zai() -> ProviderQuotaResult:
    """Fetch quota usage from ZAI via /api/monitor/usage/quota/limit.

    Endpoint discovered from ZAI's official `glm-plan-usage` plugin. The
    plugin uses `ANTHROPIC_AUTH_TOKEN`; we attempt with `ZAI_API_KEY`. If
    the API key is rejected we surface `auth_failed`.
    """
    key = os.getenv("ZAI_API_KEY", "")
    if not key:
        return ProviderQuotaResult(
            name="zai",
            display_name="ZAI",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (401, 403):
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

    # Some ZAI responses wrap data in a "data" key
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    limits = body.get("limits") if isinstance(body, dict) else None
    if not isinstance(limits, list):
        return _err("zai", "ZAI", key, "parse_error")

    usages: list[ProviderQuotaUsage] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", "")).upper()
        used = entry.get("currentValue") if "currentValue" in entry else entry.get("used")
        limit = entry.get("limit") if "limit" in entry else None
        if kind == "TOKENS_LIMIT":
            label, unit = "Tokens", "tokens"
        elif kind == "TIME_LIMIT":
            label, unit = "Time", "minutes"
        else:
            label, unit = kind.replace("_", " ").title() or "Quota", ""
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used) if isinstance(used, (int, float)) else None,
                limit=float(limit) if isinstance(limit, (int, float)) else None,
                unit=unit,
                reset_at=None,
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
