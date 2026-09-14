"""Hybrid composition for the serving entry points.

This module is the composition root's half of the hybrid seam. ``FixedPolicy``
and the backends decide and execute; this file answers the one question only the
serving layer can answer -- *which endpoints are local for this deployment* --
and builds the ``HybridRouter`` a ``router: fixed`` model's requests enter.

Locality is explicit and reused, never re-inferred. The gateway already has
exactly one notion of "a server we run ourselves": the ``_LOCAL_HOSTS`` set that
``servers.registry._make_provider_id`` stamps ``:local-<port>`` from and that
``serving.adapters.upstream_limiter.is_local_endpoint`` reuses for outbound
limiting. A hybrid policy that invented its own test would drift from both, so
this builder asks that same predicate and classifies a route as local only when
its base_url is one of those hosts.

When nothing classifies as local, the factory returns ``None`` and the model
keeps the plain shared ``FixedRouter``. That is deliberate: a deployment whose
routes are all remote cannot express a local/cloud split, and routing every
route through a cloud backend instead would change behavior for no benefit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.backends import FixedCloudBackend, LocalBackend
from routing.hybrid import HybridRouter
from routing.policies import FixedPolicy
from serving.adapters.upstream_limiter import is_local_endpoint
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.model_router_registry import ModelRouterRegistry
    from routing.routers import FixedRouter

logger = get_logger(__name__)

__all__ = ["HybridFixedRouterFactory", "local_endpoint_scope"]


def local_endpoint_scope(router: FixedRouter, model_id: str) -> frozenset[str]:
    """Return the endpoint ids of ``model_id`` that name a locally run server.

    Read from the router's registered routes rather than from configuration, so
    the scope describes the candidates routing can actually reach. Weights are
    deliberately *not* consulted: an endpoint whose weight is currently zero is
    still this deployment's own server, and excluding it from the scope would
    let a scoped dispatch fall back into the cloud the moment the operator
    parked it.
    """
    route = router.routes.get(model_id)
    if route is None or not route.adapters:
        return frozenset()
    return frozenset(
        str(adapter.config.endpoint_id)
        for adapter, _weight in route.adapters
        if getattr(adapter.config, "endpoint_id", None)
        and is_local_endpoint(adapter.config.base_url)
    )


class HybridFixedRouterFactory:
    """Build the hybrid entry point for each ``router: fixed`` model.

    Implements ``routing.model_router_registry.HybridRouterFactory``. The local
    backend wraps the shared ``FixedRouter`` with the model's local endpoints as
    its declared scope, so a local attempt's fallback candidates cannot walk
    into the cloud; the cloud backend binds the complementary endpoint set.

    Args:
        registry: Registry the built routers are handed to. Used only to read
            the route table the scopes are derived from.
        health_registry: Process-scoped endpoint health collaborator, shared with
            every other router so circuit state stays process-wide.
        cloud_scope: Optional explicit cloud endpoint set. ``None`` derives it as
            "every registered endpoint of the model that is not local".
    """

    def __init__(
        self,
        *,
        registry: ModelRouterRegistry,
        health_registry: Any,
        cloud_scope: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._health_registry = health_registry
        self._cloud_scope = cloud_scope

    def build(
        self,
        *,
        shared_fixed: FixedRouter,
        model_id: str,
        params: dict[str, Any],
    ) -> Any | None:
        """Return the hybrid router for ``model_id``, or None to keep the shared one."""
        del params  # router_params stay validated by the strategy, not consumed here
        local_scope = local_endpoint_scope(shared_fixed, model_id)
        if not local_scope:
            return None
        cloud_scope = self._cloud_scope
        if cloud_scope is None:
            cloud_scope = self._registered_endpoints(shared_fixed, model_id) - local_scope
        policy = FixedPolicy(
            compute=shared_fixed,
            local_scope=local_scope,
            cloud_scope=cloud_scope,
        )
        local = LocalBackend(
            shared_fixed,
            endpoint_scope=local_scope,
            model_scope={model_id},
        )
        # The cloud backend needs a router holding the cloud routes. Handing it
        # the shared one would be wrong: if the policy's cloud target failed,
        # the shared router's own fallback loop would walk the remaining
        # candidates -- including the local replicas -- and the cloud domain
        # would silently answer from the local fleet.
        cloud = self._build_cloud(shared_fixed, model_id=model_id, scope=cloud_scope)
        if cloud is None:
            # No cloud candidate registered for this model: the local backend is
            # the whole composition, and a one-cloud composition would be a
            # backend with an empty range. Keep the shared router, whose behavior
            # is already exactly "local only".
            return None
        logger.info(
            "hybrid router initialized: model=%s local=%d cloud=%d",
            model_id,
            len(local_scope),
            len(cloud_scope),
        )
        return HybridRouter(
            policy=policy,
            local=local,
            cloud=cloud,
            name=f"hybrid:{model_id}",
        )

    @staticmethod
    def _registered_endpoints(shared_fixed: FixedRouter, model_id: str) -> frozenset[str]:
        """Return every endpoint id registered for ``model_id``.

        Weights are not consulted, for the same reason ``local_endpoint_scope``
        ignores them: a route parked at weight 0 is still part of the model's
        declared range, and dropping it from the scope would turn "temporarily
        parked" into "routed to the other domain".
        """
        route = shared_fixed.routes.get(model_id)
        if route is None or not route.adapters:
            return frozenset()
        return frozenset(
            str(adapter.config.endpoint_id)
            for adapter, _weight in route.adapters
            if getattr(adapter.config, "endpoint_id", None)
        )

    def _build_cloud(
        self,
        shared_fixed: FixedRouter,
        *,
        model_id: str,
        scope: frozenset[str],
    ) -> Any | None:
        """Build the cloud side, or None when this model has no cloud candidate.

        A scoped ``FixedRouter`` is used rather than a ``RouteWiseRouter``:
        RouteWise keeps its own full-pool candidacy for models configured with
        ``router: routewise``, and standing one up per ``fixed`` model would
        duplicate its bookkeeping for models whose cloud algorithm is the
        operator's configured weights, not RouteWise's LP.
        """
        if not scope:
            return None
        from routing.routers import FixedRouter

        scoped = FixedRouter(
            weight_override_resolver=shared_fixed.weight_override_resolver,
            disabled_provider_resolver=shared_fixed.disabled_provider_resolver,
            health_registry=shared_fixed.endpoint_health_registry,
        )
        for route_key, route in shared_fixed.routes.items():
            if (route.canonical_model_id or route_key) != model_id:
                continue
            scoped.register_route(
                route_key,
                [(adapter, weight) for adapter, weight in route.adapters],
                admin_only=route.admin_only,
                required_role=route.required_role,
                published=route.published,
            )
        return FixedCloudBackend(
            scoped,
            endpoint_scope=scope,
            model_scope={model_id},
        )
