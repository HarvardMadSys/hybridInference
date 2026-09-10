"""Opt-in quota reporting for the example's local, credential-free mock server."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from serving.admin.provider_quotas import register_quota_fetcher
from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage

_DEFAULT_BASE_URL = "http://127.0.0.1:18353"
_TIMEOUT_SECONDS = 2


def _result(
    *, error: str | None = None, usage: ProviderQuotaUsage | None = None
) -> list[ProviderQuotaResult]:
    return [
        ProviderQuotaResult(
            name="example_quota",
            display_name="Example Quota",
            key_configured=False,
            key_masked="Local simulation (no credentials)",
            key_ref=None,
            fetched_at=datetime.now(timezone.utc),
            ok=error is None,
            error=error,
            usages=[usage] if usage is not None else [],
        )
    ]


def _parse_usage(payload: Any) -> ProviderQuotaUsage:
    if not isinstance(payload, dict):
        raise ValueError("expected an object")
    used, limit = payload["used"], payload["limit"]
    # The mock reports whole requests; the shared schema stores them as floats.
    if any(type(value) is not int or value > 2**53 - 1 for value in (used, limit)):
        raise ValueError("expected exactly representable request counts")
    if not 0 <= used <= limit or limit <= 0:
        raise ValueError("inconsistent request counts")
    raw_reset = payload["reset_at"]
    if not isinstance(raw_reset, str):
        raise ValueError("expected an ISO timestamp")
    reset_at = datetime.fromisoformat(raw_reset.replace("Z", "+00:00"))
    if reset_at.tzinfo is None:
        raise ValueError("expected an aware timestamp")
    reset_at = reset_at.astimezone(timezone.utc)
    next_reset = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if reset_at != next_reset:
        raise ValueError("expected the next UTC daily boundary")
    return ProviderQuotaUsage(
        label="Daily requests", used=used, limit=limit, unit="requests", reset_at=reset_at
    )


async def fetch(operational_store: Any = None, services: Any = None) -> list[ProviderQuotaResult]:
    """Read only the mock's /usage endpoint, without credentials or redirects."""
    base_url = os.environ.get("EXAMPLE_QUOTA_BASE_URL", _DEFAULT_BASE_URL).strip()
    if not base_url:
        return _result(error="not_configured")
    try:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in base_url)
        ):
            return _result(error="invalid_base_url")
        # Accessing port also rejects malformed or out-of-range port numbers.
        _ = parsed.port
    except ValueError:
        return _result(error="invalid_base_url")

    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            ) as session,
            session.get(f"{base_url.rstrip('/')}/usage", allow_redirects=False) as response,
        ):
            if response.status in {401, 403}:
                return _result(error="auth_failed")
            if response.status != 200:
                return _result(error=f"http_{response.status}")
            usage = _parse_usage(await response.json())
        return _result(usage=usage)
    except asyncio.TimeoutError:
        return _result(error="timeout")
    except (
        aiohttp.ContentTypeError,
        aiohttp.ClientPayloadError,
        ValueError,
        KeyError,
        OverflowError,
    ):
        return _result(error="parse_error")
    except (aiohttp.ClientError, OSError):
        return _result(error="unreachable")


def register() -> None:
    """Register the local simulation only when explicitly loaded as an extension."""
    register_quota_fetcher("example_quota", "Example Quota", fetch)
