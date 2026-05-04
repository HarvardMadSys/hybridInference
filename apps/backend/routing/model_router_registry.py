"""Per-model router registry with YAML-config-driven dispatch.

Reads each model's ``router`` and ``router_params`` from the parsed
``models.yaml`` config and constructs the corresponding strategy via
``routing.strategies.build_router``.  Routers are cached per model_id, so
the first ``get_router(model_id)`` call pays the construction cost and
subsequent calls return the same instance.

When a model omits ``router:``, ``default_router_name`` (typically read
from ``routing.yaml``'s ``default_router`` field) is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.strategies import build_router
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.routers import BaseRouter

logger = get_logger(__name__)


class ModelRouterRegistry:
    """Maps ``model_id -> BaseRouter`` via per-model YAML configuration.

    Args:
        models_config: Mapping ``{model_id: per_model_dict}``.  Each
            ``per_model_dict`` may contain ``router`` (strategy name) and
            ``router_params`` (dict).  Models not in the mapping fall back
            to ``default_router_name`` with empty params.
        default_router_name: Strategy name used when a model omits
            ``router``.  Must be a registered strategy (e.g. ``"fixed"``).
    """

    def __init__(
        self,
        models_config: dict[str, dict[str, Any]],
        default_router_name: str = "fixed",
    ) -> None:
        self._configs = models_config
        self._default = default_router_name
        self._cache: dict[str, BaseRouter] = {}
        # The shared FixedRouter is bound after construction (see
        # bind_fixed_router); RouteWise needs it for classification.
        self._shared_fixed: BaseRouter | None = None

    def bind_fixed_router(self, fixed_router: BaseRouter) -> None:
        """Provide the shared ``FixedRouter`` for late-bound strategies.

        Strategies like RouteWise need a handle on the live ``FixedRouter``
        (whose ``routes`` dict provides the per-model adapter lists).  The
        registry constructs the strategy first, then calls
        ``attach_fixed_router(self._shared_fixed)`` on it if available.

        Must be called before the first ``get_router(...)`` call for any
        model whose strategy late-binds to the FixedRouter.
        """
        self._shared_fixed = fixed_router

    def get_router(self, model_id: str) -> BaseRouter:
        """Return (constructing on first call) the router for ``model_id``."""
        cached = self._cache.get(model_id)
        if cached is not None:
            return cached
        cfg = self._configs.get(model_id, {})
        name = cfg.get("router") or self._default
        params = cfg.get("router_params") or {}
        logger.info(
            "router_initialized",
            extra={
                "event": "router_initialized",
                "model": model_id,
                "strategy": name,
                "param_keys": sorted(params.keys()),
            },
        )
        router = build_router(name, params)
        # Late-bind FixedRouter for RouteWise (and any future late-bound
        # strategy that exposes attach_fixed_router).
        attach = getattr(router, "attach_fixed_router", None)
        if attach is not None and self._shared_fixed is not None:
            attach(self._shared_fixed)
        self._cache[model_id] = router
        return router

    def registered_models(self) -> dict[str, str]:
        """Return ``{model_id: router_class_name}`` for every cached entry."""
        return {mid: type(r).__name__ for mid, r in self._cache.items()}
