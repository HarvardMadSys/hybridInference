"""Runtime settings with TTL cache and database-backed overrides.

Provides a registry of feature flags / operational knobs that can be toggled
at runtime through the admin API without restarting the server.  Each setting
has a type, a default value, and a human-readable description.

Values are resolved in this order:
1. In-memory TTL cache (avoids DB round-trips)
2. ``site_settings`` table via the OperationalStore
3. ``Settings`` (Pydantic env-var settings) attribute as fallback
4. Registry default
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Request  # noqa: TC002 — required at runtime for FastAPI Depends

from serving.utils.logging import get_logger

logger = get_logger(__name__)

RUNTIME_SETTINGS_REGISTRY: dict[str, dict[str, Any]] = {
    "user_auth_enabled": {
        "type": "bool",
        "default": True,
        "description": "Enable user authentication (JWT-based)",
    },
    "signup_enabled": {
        "type": "bool",
        "default": True,
        "description": "Allow new user signups",
    },
    "signup_require_email_verification": {
        "type": "bool",
        "default": True,
        "description": "Require email verification for new signups",
    },
    "log_full_payload": {
        "type": "bool",
        "default": False,
        "description": "Log full request payloads at DEBUG level",
    },
    "log_rejected_requests": {
        "type": "bool",
        "default": False,
        "description": (
            "Persist rejected inference requests (rate-limit, quota, auth, "
            "model-not-found) to api_logs with metadata.rejection=true."
        ),
    },
    "log_synthetic_probes": {
        "type": "bool",
        "default": False,
        "description": (
            "Persist synthetic probe requests (X-Probe: synthetic) to api_logs "
            "so they — and their real usage/cost — appear in the requests "
            "dashboard, which is useful for tracking monitoring cost. They stay "
            "excluded from per-user quota increments and the request metrics."
        ),
    },
    "force_chat_completions_streaming": {
        "type": "bool",
        "default": False,
        "description": (
            "Send non-streaming chat completions upstream as streaming requests, "
            "then buffer and return a normal non-streaming response to clients."
        ),
    },
    "kimi_coding_identity_enabled": {
        "type": "bool",
        "default": True,
        "description": (
            "Inject the coding-tool identity on Kimi coding-plan requests: set "
            "User-Agent: claude-code/0.1.0 and prepend a 'You are OpenCode' "
            "system message. Disable to forward the caller's own User-Agent and "
            "skip the system message."
        ),
    },
    "user_concurrency_free": {
        "type": "int",
        "default": 3,
        "min": 1,
        "description": "Per-user concurrency cap for free-tier users",
    },
    "user_concurrency_pro": {
        "type": "int",
        "default": 3,
        "min": 1,
        "description": "Per-user concurrency cap for pro-tier users",
    },
    "user_concurrency_internal": {
        "type": "int",
        "default": 10,
        "min": 1,
        "description": "Per-user concurrency cap for internal users",
    },
    "user_concurrency_admin": {
        "type": "int",
        "default": 10,
        "min": 1,
        "description": "Per-user concurrency cap for admin users",
    },
    "user_daily_quota_free": {
        "type": "float",
        "default": 100.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto a free-tier user's active API key at signup."
        ),
    },
    "user_daily_quota_pro": {
        "type": "float",
        "default": 100.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto a pro-tier user's active API key at signup."
        ),
    },
    "user_daily_quota_internal": {
        "type": "float",
        "default": 1000.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto an internal user's active API key at signup."
        ),
    },
    "user_daily_quota_admin": {
        "type": "float",
        "default": 1000.00,
        "min": 0.0,
        "description": (
            "Default daily USD spend quota seeded onto an admin user's active API key at signup."
        ),
    },
    "routewise_latency_slo_sec": {
        "type": "float",
        "default": 3.0,
        "min": 0.1,
        "description": "Latency SLO in seconds for Routewise LP decisions",
    },
    "routewise_latency_min_samples": {
        "type": "int",
        "default": 10,
        "min": 1,
        "description": "Minimum samples before Routewise latency LP warmup ends",
    },
}

_SENTINEL = object()


class RuntimeSettings:
    """TTL-cached reader for runtime settings backed by the operational store."""

    def __init__(self, store: Any, ttl: float = 30.0) -> None:
        self._store = store
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    def _coerce(self, raw: str | None, value_type: str) -> Any:
        if raw is None:
            return None
        if value_type == "bool":
            return raw.lower() in ("true", "1", "yes")
        if value_type == "int":
            return int(raw)
        if value_type == "float":
            return float(raw)
        return raw

    async def _read_from_db(self, key: str) -> Any:
        entry = RUNTIME_SETTINGS_REGISTRY.get(key)
        if entry is None:
            raise KeyError(f"Unknown runtime setting: {key}")
        row = await self._store.get_setting(key)
        if row is not None:
            return self._coerce(row.get("value"), row.get("value_type", entry["type"]))
        return None

    async def get_bool(self, key: str) -> bool:
        """Return the setting value as a bool."""
        val = await self._get(key)
        return bool(val)

    async def get_int(self, key: str) -> int:
        """Return the setting value as an int."""
        val = await self._get(key)
        return int(val) if val is not None else 0

    async def get_float(self, key: str) -> float:
        """Return the setting value as a float."""
        val = await self._get(key)
        return float(val) if val is not None else 0.0

    async def get_str(self, key: str) -> str:
        """Return the setting value as a string."""
        val = await self._get(key)
        return str(val) if val is not None else ""

    async def _get(self, key: str) -> Any:
        entry = RUNTIME_SETTINGS_REGISTRY.get(key)
        if entry is None:
            raise KeyError(f"Unknown runtime setting: {key}")

        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]

        db_val = await self._read_from_db(key)
        if db_val is not None:
            self._cache[key] = (now, db_val)
            return db_val

        from serving.config.settings import get_settings

        settings = get_settings()
        attr_val = getattr(settings, key, _SENTINEL)
        if attr_val is not _SENTINEL:
            self._cache[key] = (now, attr_val)
            return attr_val

        default = entry["default"]
        self._cache[key] = (now, default)
        return default

    def get_cached(self, key: str) -> tuple[bool, Any]:
        """Return ``(found, value)`` for a non-expired cached entry.

        Synchronous; does **not** touch the database. Use only on hot
        synchronous paths where awaiting :meth:`get_bool` (or peers) is
        impossible. If the cache hasn't been populated yet, returns
        ``(False, None)`` and the caller should fall back to its
        environment-variable / default behaviour.
        """
        cached = self._cache.get(key)
        if cached is None:
            return False, None
        cached_at, value = cached
        if (time.monotonic() - cached_at) >= self._ttl:
            return False, None
        return True, value

    def invalidate_cache(self) -> None:
        """Clear all cached setting values."""
        self._cache.clear()

    def invalidate_key(self, key: str) -> None:
        """Remove a single key from the cache."""
        self._cache.pop(key, None)

    async def list_all(self) -> list[dict[str, Any]]:
        """Return metadata for every registered setting."""
        from serving.config.settings import get_settings

        settings = get_settings()
        results: list[dict[str, Any]] = []
        for key, entry in RUNTIME_SETTINGS_REGISTRY.items():
            db_val = await self._read_from_db(key)
            if db_val is not None:
                value = db_val
            else:
                attr_val = getattr(settings, key, _SENTINEL)
                value = attr_val if attr_val is not _SENTINEL else entry["default"]
            results.append(
                {
                    "key": key,
                    "value": value,
                    "value_type": entry["type"],
                    "default_value": entry["default"],
                    "description": entry["description"],
                    "min": entry.get("min"),
                    "max": entry.get("max"),
                }
            )
        return results


_runtime_settings: RuntimeSettings | None = None


def init_runtime_settings(store: Any) -> RuntimeSettings:
    """Create and register the global RuntimeSettings singleton."""
    global _runtime_settings
    _runtime_settings = RuntimeSettings(store)
    return _runtime_settings


def get_runtime_settings_instance() -> RuntimeSettings:
    """Return the global RuntimeSettings singleton or raise if not initialized."""
    if _runtime_settings is None:
        raise RuntimeError("RuntimeSettings not initialized")
    return _runtime_settings


def get_runtime_settings(request: Request) -> RuntimeSettings | None:
    """Return the RuntimeSettings from app state (FastAPI dependency).

    May be ``None`` early in startup before bootstrap initializes services.
    Callers that require a non-``None`` instance should raise an HTTP 503
    when they receive ``None``.
    """
    services = getattr(request.app.state, "services", None)
    if services is None:
        return None
    return getattr(services, "runtime_settings", None)
