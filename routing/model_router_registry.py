"""Per-model router registry for routing strategy dispatch.

Maps each model_id to a specific BaseRouter instance, enabling different
models to use different routing strategies (Fixed, Nimbus, RouteWise, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from routing.routers import BaseRouter


class ModelRouterRegistry:
    """Registry mapping model_id -> BaseRouter for per-model routing dispatch.

    Models without an explicit registration fall back to the default router.
    """

    def __init__(self, default_router: BaseRouter) -> None:
        self._default = default_router
        self._registry: dict[str, BaseRouter] = {}

    def register(self, model_id: str, router: BaseRouter) -> None:
        """Register a specific router for a model."""
        self._registry[model_id] = router

    def get_router(self, model_id: str) -> BaseRouter:
        """Return the router for *model_id*, or the default if unregistered."""
        return self._registry.get(model_id, self._default)

    def registered_models(self) -> dict[str, str]:
        """Return a mapping of model_id -> router class name."""
        return {mid: type(r).__name__ for mid, r in self._registry.items()}
