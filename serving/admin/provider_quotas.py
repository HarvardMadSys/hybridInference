"""Provider quota fetchers for the admin dashboard 'Providers' tab.

Each public fetcher returns a `ProviderQuotaResult`. Errors are converted
to structured results — fetchers never raise out of the gather.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

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
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, allow_redirects=False) as resp,
        ):
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
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
    for key, label in (("four_hour", "4-hour window"), ("monthly", "Monthly")):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        used = block.get("usage")
        limit = block.get("cap")
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
                unit="USD",
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
        if kind == "TOKENS_LIMIT":
            label, unit = "Tokens", "tokens"
        elif kind == "TIME_LIMIT":
            label, unit = "Time", "minutes"
        else:
            label, unit = kind.replace("_", " ").title() or "Quota", ""

        used_raw = entry.get("currentValue") if "currentValue" in entry else entry.get("used")
        limit_raw = entry.get("usage")  # "usage" is the total cap in the ZAI API

        # For entries with no absolute values, fall back to percentage
        if used_raw is None and limit_raw is None and "percentage" in entry:
            pct = entry.get("percentage")
            usages.append(
                ProviderQuotaUsage(
                    label=label,
                    used=float(pct) if isinstance(pct, (int, float)) else None,
                    limit=100.0,
                    unit="%",
                    reset_at=None,
                )
            )
            continue

        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used_raw) if isinstance(used_raw, (int, float)) else None,
                limit=float(limit_raw) if isinstance(limit_raw, (int, float)) else None,
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


async def fetch_minimax() -> ProviderQuotaResult:
    """Fetch coding-plan quota from MiniMax via cookie-authed endpoint.

    The endpoint requires browser session cookies; API key auth returns
    `{"base_resp": {"status_code": 1004, "status_msg": "cookie missing"}}`.
    """
    cookie = os.getenv("MINIMAX_SESSION_COOKIE", "")
    if not cookie:
        return ProviderQuotaResult(
            name="minimax",
            display_name="MiniMax",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

    url = "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains"
    headers = {"Cookie": cookie}
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

    base_resp = data.get("base_resp") if isinstance(data.get("base_resp"), dict) else None
    if base_resp and base_resp.get("status_code") == 1004:
        return _err("minimax", "MiniMax", cookie, "auth_failed")
    if base_resp and base_resp.get("status_code") not in (None, 0):
        return _err("minimax", "MiniMax", cookie, "unexpected")

    body = data.get("data") if isinstance(data.get("data"), dict) else data
    model_remains = body.get("model_remains") if isinstance(body, dict) else None
    if not isinstance(model_remains, list) or not model_remains:
        return _err("minimax", "MiniMax", cookie, "parse_error")

    usages: list[ProviderQuotaUsage] = []
    for entry in model_remains:
        if not isinstance(entry, dict):
            continue
        model_name = str(entry.get("model_name", "Coding plan"))
        remain = entry.get("remain_count")
        total = entry.get("total_count")
        end = entry.get("end_time")
        reset_dt = None
        if isinstance(end, str):
            try:
                reset_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError:
                reset_dt = None
        used = None
        if isinstance(remain, (int, float)) and isinstance(total, (int, float)):
            used = float(total - remain)
        usages.append(
            ProviderQuotaUsage(
                label=model_name,
                used=used,
                limit=float(total) if isinstance(total, (int, float)) else None,
                unit="requests",
                reset_at=reset_dt,
            )
        )

    if not usages:
        return _err("minimax", "MiniMax", cookie, "parse_error")

    return ProviderQuotaResult(
        name="minimax",
        display_name="MiniMax",
        key_configured=True,
        key_masked=_mask_key(cookie),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


_USAGE_PATTERN = re.compile(
    r"(?P<label>session|weekly|monthly|daily)\s+usage\s+(?P<pct>[\d.]+)%\s+used",
    re.IGNORECASE,
)


async def fetch_ollama() -> ProviderQuotaResult:
    """Scrape Ollama Cloud usage from the settings page (cookie-authenticated).

    Ollama exposes no quota API; we GET https://ollama.com/settings with the
    admin's session cookie and parse usage figures from the HTML. If the
    page structure changes, the fetcher returns parse_error so the admin
    knows the parser needs updating.
    """
    cookie = os.getenv("OLLAMA_SESSION_COOKIE", "")
    if not cookie:
        return ProviderQuotaResult(
            name="ollama",
            display_name="Ollama Cloud",
            key_configured=False,
            key_masked=None,
            fetched_at=_now(),
            ok=False,
            error="not_configured",
            usages=[],
        )

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
        # Authenticated pages have usage figures; their absence usually
        # means cookie expired and we got a sign-in page instead.
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
        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=pct,
                limit=100.0,
                unit="%",
                reset_at=None,
            )
        )
    return usages


async def gather_all() -> list[ProviderQuotaResult]:
    """Run all 4 provider fetchers in parallel; never raise.

    If a fetcher raises (rather than returning an error result), the
    exception is caught and converted to a `ProviderQuotaResult(ok=False,
    error='unexpected')` so the admin endpoint can always respond with a
    well-formed payload.
    """
    fetchers = [
        ("chutes", "Chutes", fetch_chutes),
        ("zai", "ZAI", fetch_zai),
        ("minimax", "MiniMax", fetch_minimax),
        ("ollama", "Ollama Cloud", fetch_ollama),
    ]
    raw = await asyncio.gather(
        *(f() for _, _, f in fetchers),
        return_exceptions=True,
    )
    out: list[ProviderQuotaResult] = []
    for (name, display_name, _), result in zip(fetchers, raw, strict=True):
        if isinstance(result, ProviderQuotaResult):
            out.append(result)
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
