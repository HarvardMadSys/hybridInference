"""Provider API-key dry-run verification helpers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from typing import Any

import aiohttp

from serving.adapters import (
    AnthropicAdapter,
    ClaudeAdapter,
    CodingIdentityAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
    dynamic_keys,
)
from serving.exceptions import scrub_provider_identity
from serving.servers.registry import _make_adapter

DEFAULT_VERIFY_TIMEOUT_SECONDS = 20.0
FEATHERLESS_PLAN_API_DISABLED_MESSAGE = (
    "The current subscription plan does not have API access enabled."
)


class ProviderKeyProbeError(Exception):
    """Provider key probe failed with a UI-safe reason and detail."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ProviderKeyProbeNoRouteError(ProviderKeyProbeError):
    """No suitable registered route exists for probing this provider."""


def truncate_probe_detail(value: str, limit: int = 500) -> str:
    """Trim provider probe details to a bounded UI-safe length."""
    value = value.strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def probe_error_reason(exc: BaseException) -> str:
    """Classify a probe exception into a short admin API reason code."""
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, aiohttp.ClientResponseError):
        if (
            exc.status == 403
            and _upstream_error_message(exc) == FEATHERLESS_PLAN_API_DISABLED_MESSAGE
        ):
            return "plan_api_disabled"
        if exc.status in (301, 302, 303, 307, 308, 401, 403):
            return "auth_failed"
        return "unexpected"
    return "unexpected"


def _upstream_error_message(exc: aiohttp.ClientResponseError) -> str:
    body = getattr(exc, "error_body", "")
    if not body:
        return ""
    body = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""

    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    for key in ("message", "detail"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _safe_probe_detail(value: str, *, api_key: str) -> str:
    if api_key:
        value = value.replace(api_key, "[redacted]")
    return truncate_probe_detail(scrub_provider_identity(value))


def probe_error_detail(
    exc: BaseException,
    *,
    timeout_seconds: float,
    api_key: str = "",
) -> str:
    """Build a redacted provider probe failure message for admin users."""
    if isinstance(exc, asyncio.TimeoutError):
        return f"Provider key verification timed out after {timeout_seconds:.0f}s"
    if isinstance(exc, aiohttp.ClientResponseError):
        body = getattr(exc, "error_body", "")
        body_text = f": {_safe_probe_detail(str(body), api_key=api_key)}" if body else ""
        return f"Provider key verification failed with HTTP {exc.status}{body_text}"
    detail = _safe_probe_detail(str(exc) or type(exc).__name__, api_key=api_key)
    return f"Provider key verification failed: {detail}"


def _config_to_dict(config: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in fields(ModelConfig):
        if hasattr(config, field.name):
            values[field.name] = getattr(config, field.name)
    return values


def _adapter_kind(adapter: object) -> str:
    config = getattr(adapter, "config", None)
    if config is None:
        return ""
    provider = str(getattr(config, "provider", "") or "")
    if isinstance(adapter, OpenRouterAdapter):
        return "openrouter"
    if isinstance(adapter, ClaudeAdapter):
        return "claude"
    if isinstance(adapter, GeminiAdapter):
        return "gemini"
    if isinstance(adapter, AnthropicAdapter):
        return "anthropic"
    if isinstance(adapter, CodingIdentityAdapter):
        return provider
    if isinstance(adapter, OpenAICompatAdapter):
        return "openai_compat" if provider == "openai" else provider
    return provider


def _route_entries(route: Any) -> list[tuple[object, float]]:
    raw_adapters = getattr(route, "raw_adapters", None)
    if raw_adapters:
        return [(adapter, float(weight)) for adapter, weight, _endpoint_id in raw_adapters]
    return list(getattr(route, "adapters", []) or [])


def _adapter_key_provider(adapter: object) -> str:
    config = getattr(adapter, "config", None)
    if config is None:
        return ""
    if isinstance(adapter, OpenRouterAdapter) or getattr(
        config, "openrouter_pinned_provider", None
    ):
        return "openrouter"
    provider = str(getattr(config, "provider", "") or "")
    return dynamic_keys.normalize_key_provider(provider)


def find_verification_adapter(services: Any, provider: str) -> object | None:
    """Find a route adapter suitable for probing a candidate provider key."""
    routes = getattr(getattr(services, "router", None), "routes", {}) or {}
    for route in routes.values():
        for adapter, _weight in _route_entries(route):
            config = getattr(adapter, "config", None)
            if config is None:
                continue
            if _adapter_key_provider(adapter) != provider:
                continue
            if (getattr(config, "model_type", None) or "chat") != "chat":
                continue
            if not getattr(config, "base_url", None):
                continue
            if not (getattr(config, "provider_model_id", None) or getattr(config, "id", None)):
                continue
            return adapter
    return None


async def probe_provider_key_with_existing_route(
    services: Any,
    *,
    provider: str,
    api_key: str,
    timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> None:
    """Probe a key using the model/base URL from an already registered route."""
    adapter = find_verification_adapter(services, provider)
    if adapter is None:
        raise ProviderKeyProbeNoRouteError(
            "probe_unavailable",
            f"No registered route available to verify provider {provider!r}",
        )

    cfg = _config_to_dict(adapter.config)
    cfg["api_key"] = api_key
    cfg["api_keys"] = None

    dry_run_adapter = _make_adapter(_adapter_kind(adapter), cfg)
    try:
        await asyncio.wait_for(
            dry_run_adapter.chat_completion(
                [{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        detail = probe_error_detail(exc, timeout_seconds=timeout_seconds, api_key=api_key)
        raise ProviderKeyProbeError(probe_error_reason(exc), detail) from exc
