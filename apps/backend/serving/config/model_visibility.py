"""Runtime resolver for effective per-model visibility roles."""

from __future__ import annotations

import time
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

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        """Return the runtime override for a model or fall back to the default role."""
        now = time.monotonic()
        cached = self._cache.get(model_id)
        if cached is not None and (now - cached[0]) < self._ttl:
            override = cached[1]
            return override or default_role

        row = await self._store.get_model_visibility_override(model_id)
        override = None if row is None else row.get("required_role")
        if override is not None and override not in VALID_ROLES:
            logger.warning(
                "Model %s has invalid runtime required_role %r; failing closed to admin",
                model_id,
                override,
            )
            override = "admin"

        self._cache[model_id] = (now, override)
        return override or default_role

    def invalidate_model(self, model_id: str) -> None:
        """Drop the cached override entry for a single model."""
        self._cache.pop(model_id, None)

    def invalidate_cache(self) -> None:
        """Clear all cached model visibility overrides."""
        self._cache.clear()
