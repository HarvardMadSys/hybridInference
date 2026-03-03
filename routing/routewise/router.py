"""RouteWise cost-aware router scaffold.

This module provides the ``RouteWiseRouter``, a ``BaseRouter`` subclass that
classifies adapters by subscription type (quota / concurrency / API) and
selects among them.  In this scaffold the selection logic defaults to the
cheapest available API adapter; PR-3 will replace ``_select_adapter`` with
the full primal-dual / look-ahead primal-dual decision logic.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from routing.routers import BaseRouter, RoutingObservation
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter

    from .config import RouteWiseConfig

logger = get_logger(__name__)


class SubscriptionType(Enum):
    """Subscription tier for an adapter endpoint.

    Each route entry in ``models.yaml`` can declare a ``subscription_type``
    field.  RouteWiseRouter reads this field from ``ModelConfig`` and uses
    it to classify adapters into one of the three tiers.
    """

    QUOTA = "quota"  # S_Q: daily quota subscription
    CONCURRENCY = "concurrency"  # S_C: concurrency-limited subscription
    API = "api"  # S_A: pay-per-token (default)


class RouteWiseRouter(BaseRouter):
    """Cost-aware router that classifies adapters by subscription type.

    RouteWiseRouter is a **peer** of NimbusRouter -- both extend BaseRouter
    directly and operate on routes registered in a shared ``FixedRouter``.

    In this scaffold the router simply selects the first available quota
    adapter, or falls back to the cheapest API adapter (by prompt + completion
    price).  PR-3 will wire in the primal-dual decision logic and predictor
    updates.

    Attributes:
        fixed_router: The shared ``FixedRouter`` whose ``routes`` dict
            provides the per-model adapter lists.
        config: ``RouteWiseConfig`` policy parameters.
        classified: Per-model adapter classification:
            ``{model_id: [(adapter, weight, SubscriptionType), ...]}``.
    """

    def __init__(
        self,
        fixed_router: Any,
        config: RouteWiseConfig,
        experiment_mode: bool = False,
    ) -> None:
        super().__init__(experiment_mode=experiment_mode)
        self.fixed_router = fixed_router
        self.config = config
        # Lazily populated on first access per model.
        self.classified: dict[str, list[tuple[Any, float, SubscriptionType]]] = {}
        self._classify_all()

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify_all(self) -> None:
        """Walk every route in FixedRouter and classify adapters."""
        for model_id, route_cfg in self.fixed_router.routes.items():
            entries: list[tuple[Any, float, SubscriptionType]] = []
            for adapter, weight in route_cfg.adapters:
                sub_str = getattr(adapter.config, "subscription_type", "api")
                try:
                    sub_type = SubscriptionType(sub_str)
                except ValueError:
                    logger.warning(
                        "Unknown subscription_type '%s' for %s; defaulting to API",
                        sub_str,
                        adapter.config.endpoint_id or adapter.config.id,
                    )
                    sub_type = SubscriptionType.API
                entries.append((adapter, weight, sub_type))
            self.classified[model_id] = entries

    # ------------------------------------------------------------------
    # BaseRouter abstract method implementations
    # ------------------------------------------------------------------

    def _is_eligible(self, sub: SubscriptionType) -> bool:
        """Check whether an adapter with *sub* type is eligible in Stage 1.

        Concurrency adapters are only eligible when ``concurrency_enabled``
        is True in the config.  Quota and API adapters are always eligible.
        """
        if sub is SubscriptionType.CONCURRENCY:
            return self.config.concurrency_enabled
        return True

    @staticmethod
    def _adapter_cost(adapter: Any) -> float:
        """Return prompt + completion price for *adapter*.

        Aligns with ADR 4.6: ``v_t = min(price_in * n_in + price_out * n_out)``
        -- for the scaffold we use the sum of unit prices as a proxy.
        """
        pricing = adapter.config.pricing
        return float(pricing.get("prompt", "0")) + float(pricing.get("completion", "0"))

    def _select_adapter(self, model_id: str, context: dict[str, Any]) -> BaseAdapter | None:
        """Select an adapter for *model_id*.

        Scaffold strategy (PR-2):
        1. If a quota adapter is available, return it.
        2. Otherwise return the API adapter with the lowest
           prompt + completion price (ADR 4.6 baseline).
        3. Concurrency adapters are skipped when ``concurrency_enabled``
           is False.

        PR-3 replaces this with primal-dual / LA-PD logic.
        """
        entries = self.classified.get(model_id)
        if entries is None:
            raise ValueError(f"RouteWiseRouter has no route for model '{model_id}'")

        # Prefer quota adapters.
        for adapter, _w, sub in entries:
            if sub is SubscriptionType.QUOTA:
                return adapter

        # Among eligible API adapters, pick the cheapest (prompt+completion).
        api_adapters: list[tuple[Any, float]] = [
            (a, self._adapter_cost(a)) for a, _w, s in entries if s is SubscriptionType.API
        ]
        if api_adapters:
            api_adapters.sort(key=lambda t: t[1])
            return api_adapters[0][0]

        # Last resort: pick first *eligible* adapter (respects concurrency gate).
        for adapter, _w, sub in entries:
            if self._is_eligible(sub):
                return adapter

        return None

    def _get_fallback_adapters(
        self,
        model_id: str,
        failed_adapter: BaseAdapter,
    ) -> list[BaseAdapter]:
        """Return eligible adapters for *model_id*, excluding the failed one.

        Concurrency adapters are excluded when ``concurrency_enabled`` is
        False, mirroring the gate in ``_select_adapter``.
        """
        entries = self.classified.get(model_id, [])
        return [a for a, _w, s in entries if a is not failed_adapter and self._is_eligible(s)]

    def record_observation(self, obs: RoutingObservation) -> None:
        """Record a routing observation.

        Scaffold: log only.  PR-3 wires predictor update + quota tracking.
        """
        logger.debug(
            "RouteWise observation: model=%s endpoint=%s ttft=%s success=%s",
            obs.model_id,
            obs.endpoint_id,
            obs.ttft_ms,
            obs.success,
        )
