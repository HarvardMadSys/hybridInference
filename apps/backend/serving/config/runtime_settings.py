"""Runtime settings with an in-memory cache and database-backed overrides.

Provides a registry of feature flags / operational knobs that can be toggled
at runtime through the admin API without restarting the server.  Each setting
has a type, a default value, and a human-readable description.

Values are resolved in this order:
1. In-memory cache, reloaded for every setting by :meth:`RuntimeSettings.refresh`
   (one query, every :data:`REFRESH_INTERVAL_SECONDS`)
2. ``site_settings`` table via the OperationalStore
3. ``Settings`` (Pydantic env-var settings) attribute as fallback
4. Registry default

On boot, :func:`import_environment_values` copies a setting's environment
variable (the upper-cased key) into ``site_settings`` when it has no row yet,
so the variable can then be deleted from ``.env``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

from fastapi import Request  # noqa: TC002 — required at runtime for FastAPI Depends
from pydantic import TypeAdapter, ValidationError

from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: How often every process reloads the runtime settings, so a write made by one
#: worker reaches the others.
REFRESH_INTERVAL_SECONDS = 10.0

RUNTIME_SETTINGS_REGISTRY: dict[str, dict[str, Any]] = {
    "user_auth_enabled": {
        "type": "bool",
        "default": True,
        "description": "Require user API keys for inference requests (does not disable account login)",
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
    "signup_admin_notify_enabled": {
        "type": "bool",
        "default": True,
        "description": (
            "Email the configured recipients (SIGNUP_NOTIFY_EMAILS, else "
            "ADMIN_EMAILS) when a new user registers and needs approval. "
            "Disable to stop sending new-user approval notification emails."
        ),
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
            "Persist synthetic probe requests (X-Probe: synthetic, honoured "
            "only from authenticated internal/admin keys) to api_logs so they "
            "— and their real usage/cost — appear in the requests dashboard, "
            "which is useful for tracking monitoring cost. They stay excluded "
            "from the request metrics; quota/cost increments always apply."
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
    "reasoning_small_call_reroute_enabled": {
        "type": "bool",
        "default": False,
        "description": (
            "Reroute tiny-max_tokens Anthropic Messages calls aimed at a reasoning "
            "model to a fast non-reasoning model (qwen3.6-35b) with a larger output "
            "budget. These calls (e.g. agent title/summary helpers) otherwise burn "
            "the whole budget on hidden reasoning and return empty content. "
            "Tool-permission/safety-check calls are never rerouted regardless of "
            "this setting. Off by default."
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
        "default": 0,
        "min": 0,
        "description": "Per-user concurrency cap for admin users (0 = unlimited)",
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
    "routewise_budget_alpha": {
        "type": "float",
        "default": 0.75,
        "min": 0.0,
        "max": 1.0,
        "description": (
            "RouteWise LP cost budget interpolation: 0 favors cheapest feasible routes, "
            "1 allows the full effective-cost range."
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
        "description": "Legacy RouteWise latency sample threshold retained for compatibility",
    },
    "routewise_probe_enabled": {
        "type": "bool",
        "default": False,
        "description": "Enable RouteWise background active latency probes",
    },
    "routewise_probe_interval_sec": {
        "type": "float",
        "default": 300.0,
        "min": 10.0,
        "description": "Seconds between RouteWise background probe cycles",
    },
}

_SENTINEL = object()


class RuntimeSettings:
    """Cached reader for runtime settings backed by the operational store."""

    def __init__(self, store: Any, ttl: float = 30.0) -> None:
        self._store = store
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        # Bumped whenever this process writes or drops a cached value, so a
        # refresh whose query was already in flight cannot put back the value
        # an administrator just replaced.
        self._versions: dict[str, int] = {}
        self._refresh_task: asyncio.Task[None] | None = None

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

        fallback = self._fallback(key, entry)
        self._cache[key] = (now, fallback)
        return fallback

    @staticmethod
    def _fallback(key: str, entry: dict[str, Any]) -> Any:
        """Return the value a setting without a ``site_settings`` row resolves to."""
        from serving.config.settings import get_settings

        attr_val = getattr(get_settings(), key, _SENTINEL)
        return attr_val if attr_val is not _SENTINEL else entry["default"]

    async def refresh(self) -> None:
        """Reload every registered setting with one query.

        A row that does not parse keeps the setting's previous cached value, and
        is logged rather than raised so it cannot stall the other settings.
        """
        versions = dict(self._versions)
        rows = await self._store.list_settings()
        by_key = {row.get("key"): row for row in rows}
        now = time.monotonic()
        for key, entry in RUNTIME_SETTINGS_REGISTRY.items():
            if self._versions.get(key) != versions.get(key):
                continue  # Written while the query ran; that write is newer.
            row = by_key.get(key)
            if row is None:
                self._cache[key] = (now, self._fallback(key, entry))
                continue
            try:
                value = self._coerce(row.get("value"), row.get("value_type") or entry["type"])
            except (TypeError, ValueError):
                logger.warning(
                    f"Runtime setting {key!r} has an unreadable stored value; ignoring it"
                )
                continue
            self._cache[key] = (now, value)

    async def _refresh_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.refresh()
            except Exception:
                logger.warning("Runtime settings refresh failed", exc_info=True)

    def start_refresh(self, interval_seconds: float = REFRESH_INTERVAL_SECONDS) -> None:
        """Reload every setting every *interval_seconds* until :meth:`stop_refresh`."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop(interval_seconds))

    async def stop_refresh(self) -> None:
        """Stop the loop started by :meth:`start_refresh`."""
        task, self._refresh_task = self._refresh_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def get_cached(self, key: str) -> tuple[bool, Any]:
        """Return ``(found, value)`` for the last value loaded for *key*.

        Synchronous; does **not** touch the database. Use only on hot
        synchronous paths where awaiting :meth:`get_bool` (or peers) is
        impossible. The value does not expire: :meth:`refresh` replaces it,
        and an expiry with nothing to re-populate the cache would silently
        hand callers their environment fallback instead of the stored value.
        Returns ``(False, None)`` only before the first load (or after
        :meth:`invalidate_key`), when the caller should fall back to its
        environment-variable / default behaviour.
        """
        cached = self._cache.get(key)
        if cached is None:
            return False, None
        return True, cached[1]

    def set_cached(self, key: str, value: Any) -> None:
        """Record a value this process just stored, so readers see it at once."""
        self._versions[key] = self._versions.get(key, 0) + 1
        self._cache[key] = (time.monotonic(), value)

    def invalidate_cache(self) -> None:
        """Clear all cached setting values."""
        for key in self._cache:
            self._versions[key] = self._versions.get(key, 0) + 1
        self._cache.clear()

    def invalidate_key(self, key: str) -> None:
        """Remove a single key from the cache."""
        self._versions[key] = self._versions.get(key, 0) + 1
        self._cache.pop(key, None)

    async def list_all(self) -> list[dict[str, Any]]:
        """Return metadata for every registered setting."""
        results: list[dict[str, Any]] = []
        for key, entry in RUNTIME_SETTINGS_REGISTRY.items():
            db_val = await self._read_from_db(key)
            value = db_val if db_val is not None else self._fallback(key, entry)
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


# Written only where the key has no row: several workers boot at once, and an
# administrator's value must never be replaced by an environment one.
_IMPORT_SQL = """
INSERT INTO site_settings (key, value, value_type, updated_at, updated_by)
SELECT key, value, value_type, NOW(), 'env-import'
FROM unnest($1::text[], $2::text[], $3::text[]) AS imported(key, value, value_type)
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

_PARSERS: dict[str, TypeAdapter[Any]] = {
    "bool": TypeAdapter(bool),
    "int": TypeAdapter(int),
    "float": TypeAdapter(float),
}


def _environment_value(key: str, entry: dict[str, Any]) -> str | None:
    """Return a setting's environment value in the form ``site_settings`` stores.

    Parsed the way ``Settings`` parses the same variable, then written the way
    the admin API writes it (``str(value)``), so ``USER_AUTH_ENABLED=on`` is
    stored as ``True`` rather than a word the cache would read as false.
    """
    raw = os.environ.get(key.upper(), "")
    if not raw.strip():
        return None
    parser = _PARSERS.get(entry["type"])
    if parser is None:
        return raw
    try:
        value = parser.validate_python(raw.strip())
    except ValidationError:
        logger.warning(
            f"Not importing {key.upper()}: {raw.strip()!r} is not a valid {entry['type']}"
        )
        return None
    low, high = entry.get("min"), entry.get("max")
    if (low is not None and value < low) or (high is not None and value > high):
        logger.warning(f"Not importing {key.upper()}: {value} is outside the allowed range")
        return None
    return str(value)


async def import_environment_values(pool: Any) -> list[str]:
    """Copy runtime-setting environment variables into ``site_settings``.

    A setting is imported when its upper-cased name has a non-empty value in the
    environment and ``site_settings`` has no row for it. Rows are marked
    ``updated_by='env-import'``.

    Returns:
        The keys this call imported.
    """
    keys: list[str] = []
    values: list[str] = []
    types: list[str] = []
    for key, entry in RUNTIME_SETTINGS_REGISTRY.items():
        value = _environment_value(key, entry)
        if value is not None:
            keys.append(key)
            values.append(value)
            types.append(entry["type"])
    if not keys:
        return []
    async with pool.acquire() as conn:
        records = await conn.fetch(_IMPORT_SQL, keys, values, types)
    imported = [record["key"] for record in records]
    if imported:
        logger.info(
            f"Imported {len(imported)} runtime setting(s) from the environment: "
            f"{', '.join(sorted(imported))}"
        )
    return imported


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
