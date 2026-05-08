"""Per-model router registry for routing strategy dispatch.

Maps each model_id to a specific BaseRouter instance, enabling different
models to use different routing strategies (Fixed, Nimbus, RouteWise, etc.).
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.routers import BaseRouter

log = get_logger(__name__)


class ModelRouterRegistry:
    """Registry mapping model_id -> BaseRouter for per-model routing dispatch.

    Models without an explicit registration fall back to the default router.
    Supports canary rollout: when enabled, only a configurable fraction of
    traffic for allowlisted models is routed to the registered (experimental)
    router; the rest falls back to the default.
    """

    def __init__(self, default_router: BaseRouter) -> None:
        self._default = default_router
        self._registry: dict[str, BaseRouter] = {}
        self._canary_enabled: bool = False
        self._canary_router: BaseRouter | None = None
        self._canary_models: set[str] | None = None
        self._canary_fraction: float = 1.0

    def register(self, model_id: str, router: BaseRouter) -> None:
        """Register a specific router for a model."""
        self._registry[model_id] = router

    def configure_canary(
        self,
        enabled: bool,
        target_router: BaseRouter,
        enabled_models: list[str] | None,
        traffic_fraction: float,
    ) -> None:
        """Configure canary rollout parameters.

        Args:
            enabled: Whether canary gating is active.
            target_router: The specific router instance to gate.
                Only models registered to this router are subject to
                canary logic; other routers (e.g. Nimbus) are unaffected.
            enabled_models: Model allowlist for canary routing.
                ``None`` means all models using *target_router* participate.
                An empty list means no models participate.
            traffic_fraction: Fraction of traffic (0.0-1.0) routed to
                the target (experimental) router.
        """
        self._canary_enabled = enabled
        self._canary_router = target_router
        self._canary_models = set(enabled_models) if enabled_models is not None else None
        self._canary_fraction = traffic_fraction

    def get_router(self, model_id: str) -> BaseRouter:
        """Return the router for *model_id*, applying canary gate if enabled.

        Canary logic only applies to models whose registered router is the
        specific ``_canary_router`` instance.  Other routers (e.g. Nimbus)
        are never affected.
        """
        router = self._registry.get(model_id, self._default)
        if not self._canary_enabled or router is not self._canary_router:
            return router
        # Model allowlist: None = all canary-router models; empty set = none.
        if self._canary_models is not None and model_id not in self._canary_models:
            return self._default
        # Traffic fraction gate.
        if random.random() > self._canary_fraction:
            self._emit_canary_metric(model_id, "default")
            return self._default
        self._emit_canary_metric(model_id, "experimental")
        return router

    def _emit_canary_metric(self, model_id: str, outcome: str) -> None:
        """Emit a structured log event for each canary routing decision.

        Surfaces the canary gate's choice (default vs experimental router)
        as a log event so it can be aggregated by the alerting framework
        or downstream log queries. Replaces the prior counter metric
        (``routewise_canary_decisions_total``).
        """
        log.info(
            "canary_decision",
            extra={
                "event": "canary_decision",
                "model": model_id,
                "outcome": outcome,
            },
        )

    def registered_models(self) -> dict[str, str]:
        """Return a mapping of model_id -> router class name."""
        return {mid: type(r).__name__ for mid, r in self._registry.items()}
