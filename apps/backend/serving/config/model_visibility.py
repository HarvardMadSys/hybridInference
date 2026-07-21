"""Runtime resolver for effective per-model visibility roles."""

from __future__ import annotations

import time
from threading import RLock
from typing import Any

from serving.config.settings import VALID_ROLES
from serving.utils.logging import get_logger

logger = get_logger(__name__)


class ModelVisibilityResolver:
    """Resolve a model's effective required role using runtime overrides."""

    def __init__(self, store: Any, ttl: float = 30.0) -> None:
        self._store = store
        self._ttl = ttl
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._versions: dict[str, int] = {}
        self._lock = RLock()

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        """Return the runtime override for a model or fall back to the default role."""
        while True:
            now = time.monotonic()
            with self._lock:
                cached = self._cache.get(model_id)
                if cached is not None and (now - cached[0]) < self._ttl:
                    override = cached[1]
                    return override or default_role
                version = self._versions.setdefault(model_id, 0)

            row = await self._store.get_model_visibility_override(model_id)
            override = None if row is None else row.get("required_role")
            if override is not None and override not in VALID_ROLES:
                logger.warning(
                    "Model %s has invalid runtime required_role %r; failing closed to admin",
                    model_id,
                    override,
                )
                override = "admin"

            refreshed_at = time.monotonic()
            with self._lock:
                if self._versions.get(model_id, 0) != version:
                    continue
                self._cache[model_id] = (refreshed_at, override)
                return override or default_role

    def invalidate_model(self, model_id: str) -> None:
        """Drop the cached override entry for a single model."""
        with self._lock:
            self._cache.pop(model_id, None)
            self._versions[model_id] = self._versions.get(model_id, 0) + 1

    def invalidate_cache(self) -> None:
        """Clear all cached model visibility overrides."""
        with self._lock:
            self._cache.clear()
            for model_id in self._versions:
                self._versions[model_id] += 1
