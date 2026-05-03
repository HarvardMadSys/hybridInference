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
    "enable_routewise": {
        "type": "bool",
        "default": False,
        "description": "Enable RouteWise online routing subsystem",
    },
    "experiment_mode": {
        "type": "bool",
        "default": False,
        "description": "Experiment mode: disable fallback for A/B testing",
    },
    "log_full_payload": {
        "type": "bool",
        "default": False,
        "description": "Log full request payloads at DEBUG level",
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


def get_runtime_settings(request: Any) -> RuntimeSettings:
    """Return the RuntimeSettings from app state (FastAPI dependency)."""
    return request.app.state.services.runtime_settings
