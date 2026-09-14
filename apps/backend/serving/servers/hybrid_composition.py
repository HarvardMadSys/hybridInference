"""Hybrid composition for the serving entry points.

This module is the composition root's half of the hybrid seam. ``FixedPolicy``
and the backends decide and execute; this file answers the one question only the
serving layer can answer -- *which endpoints are local for this deployment* --
and builds the ``HybridRouter`` a ``router: fixed`` model's requests enter.

Locality is a deployment decision, and this builder takes it as one. A caller
may declare the local range directly (``local_scope``), supply its own ownership
resolver (``local_ownership``), or say nothing and get the gateway's existing
locality predicate: ``_LOCAL_HOSTS``, the set ``servers.registry.
_make_provider_id`` stamps ``:local-<port>`` from and that ``serving.adapters.
upstream_limiter.is_local_endpoint`` already reuses for outbound limiting. That
predicate is a compatibility default, not a definition -- it classifies by
hostname, so a gateway-owned server reached over the LAN or a cluster DNS name
reads as remote, and ``0.0.0.0`` reads as local. Deployments in that position
should pass one of the two explicit inputs instead.

The split is re-derived from the live route table on every read, because a route
edit can move an endpoint between domains and ``ModelRouterRegistry.
refresh_route_tables()`` is what publishes that edit. Freezing it at build time
would keep a removed endpoint dispatchable and make a newly added one
unreachable.

When nothing classifies as local, the factory returns ``None`` and the model
keeps the plain shared ``FixedRouter``. That is deliberate: a deployment whose
routes are all remote cannot express a local/cloud split, and routing every
route through a cloud backend instead would change behavior for no benefit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from routing.backends import FixedCloudBackend, LocalBackend
from routing.hybrid import HybridRouter
from routing.policies import FixedPolicy
from serving.adapters.upstream_limiter import is_local_endpoint
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Collection

    from routing.model_router_registry import ModelRouterRegistry
    from routing.routers import FixedRouter

logger = get_logger(__name__)

#: A candidate range, given either as a fixed set or as a live reader.
ScopeSpec = frozenset[str] | Callable[[], frozenset[str]]

#: Builds the cloud side of one model: shared ``FixedRouter``, model id, range.
#: Typed loosely on the router so this module keeps its deferred import of
#: ``routing.routers``, which the serving package is imported from.
CloudBackendBuilder = Callable[[Any, str, ScopeSpec], Any]

__all__ = [
    "CloudBackendBuilder",
    "HybridFixedRouterFactory",
    "default_local_ownership",
    "local_endpoint_scope",
]


def local_endpoint_scope(
    router: FixedRouter,
    model_id: str,
    *,
    is_local: Callable[[Any, str], bool] | None = None,
) -> frozenset[str]:
    """Return the endpoint ids of ``model_id`` that name a locally run server.

    Read from the router's registered routes rather than from configuration, so
    the scope describes the candidates routing can actually reach. Weights are
    deliberately *not* consulted: an endpoint whose weight is currently zero is
    still this deployment's own server, and excluding it from the scope would
    let a scoped dispatch fall back into the cloud the moment the operator
    parked it.

    ``is_local`` receives one ``ModelConfig`` and its endpoint id, and answers
    whether that endpoint is this deployment's own. The default is the gateway's
    hostname-based predicate; see the module docstring for what it cannot see.
    """
    owns = is_local if is_local is not None else _default_local_by_host
    route = router.routes.get(model_id)
    if route is None or not route.adapters:
        return frozenset()
    return frozenset(
        str(adapter.config.endpoint_id)
        for adapter, _weight in route.adapters
        if getattr(adapter.config, "endpoint_id", None)
        and owns(adapter.config, str(adapter.config.endpoint_id))
    )


def _default_local_by_host(config: Any, endpoint_id: str) -> bool:
    """Adapt the hostname predicate to the resolver signature."""
    del endpoint_id
    return default_local_ownership(config)


def default_local_ownership(config: Any) -> bool:
    """Return whether ``config`` names a server this gateway runs itself.

    Compatibility default for a deployment that has not declared its local range:
    the ``_LOCAL_HOSTS`` predicate the registry and the outbound limiter already
    share. Prefer an explicit ``local_scope`` or ``local_ownership``.
    """
    return is_local_endpoint(getattr(config, "base_url", None))


class HybridFixedRouterFactory:
    """Build the hybrid entry point for each ``router: fixed`` model.

    Implements ``routing.model_router_registry.HybridRouterFactory``. The local
    backend wraps the shared ``FixedRouter`` with the model's local endpoints as
    its declared scope, so a local attempt's fallback candidates cannot walk
    into the cloud; the cloud backend binds the complementary endpoint set.

    Args:
        registry: Registry the built routers are handed to. Used only to read
            the route table the scopes are derived from.
        health_registry: Process-scoped endpoint health collaborator. The wrapped
            router already carries the one it was built with; the value is
            accepted here so the composition root states which collaborator it
            means, and a mismatch is reported instead of quietly splitting
            circuit state in two.
        local_scope: Optional explicit local range, as canonical endpoint ids or
            provider labels (``{"local-service"}``). ``None`` classifies each
            endpoint with ``local_ownership``.
        local_ownership: Optional ownership resolver, called with one
            ``ModelConfig`` and the endpoint id. ``None`` uses
            :func:`default_local_ownership`, the hostname predicate.
        cloud_scope: Optional explicit cloud endpoint set. ``None`` derives it as
            "every registered endpoint of the model that is not local".
        cloud_backend: The cloud execution algorithm, as a builder taking the
            shared router, the model id and the candidate range. ``None`` uses
            :class:`~routing.backends.FixedCloudBackend`, the operator's
            configured weights. Passing
            :class:`~routing.backends.RouteWiseCloudBackend`'s builder is what
            puts RouteWise inside the cloud domain; see the design doc for the
            constraints that migration still has to satisfy.
    """

    def __init__(
        self,
        *,
        registry: ModelRouterRegistry,
        health_registry: Any,
        local_scope: Collection[str] | None = None,
        local_ownership: Callable[[Any, str], bool] | None = None,
        cloud_scope: frozenset[str] | None = None,
        cloud_backend: CloudBackendBuilder | None = None,
    ) -> None:
        self._registry = registry
        self._health_registry = health_registry
        self._local_scope = frozenset(local_scope) if local_scope is not None else None
        self._local_ownership = local_ownership
        self._cloud_scope = cloud_scope
        self._cloud_backend = cloud_backend
        self._warned_health_mismatch = False

    def _owns_local(self, config: Any, endpoint_id: str) -> bool:
        """Return whether one of this model's endpoints is this deployment's own.

        An explicitly declared ``local_scope`` decides by membership (endpoint id
        or provider label); otherwise the injected resolver does, defaulting to
        the hostname predicate. This is the single place the deployment's answer
        is expressed, so the policy, both backends and feedback attribution
        cannot disagree about which side an endpoint is on.
        """
        declared = self._local_scope
        if declared is not None:
            return endpoint_id in declared or getattr(config, "provider", None) in declared
        if self._local_ownership is not None:
            return bool(self._local_ownership(config, endpoint_id))
        return default_local_ownership(config)

    def build(
        self,
        *,
        shared_fixed: FixedRouter,
        model_id: str,
        params: dict[str, Any],
    ) -> Any | None:
        """Return the hybrid router for ``model_id``, or None to keep the shared one."""
        del params  # router_params stay validated by the strategy, not consumed here
        self._check_health_registry(shared_fixed, model_id)
        scopes = _LiveDomainScopes(shared_fixed, model_id, is_local=self._owns_local)
        if not scopes.local():
            return None
        # A declared cloud range is a constant; a derived one is a live reader,
        # so a route edit moves both sides of the split together.
        cloud_scope: Any = self._cloud_scope if self._cloud_scope is not None else scopes.cloud
        policy = FixedPolicy(
            compute=shared_fixed,
            local_scope=scopes.local,
            cloud_scope=cloud_scope,
        )
        local = LocalBackend(
            shared_fixed,
            endpoint_scope=scopes.local,
            model_scope={model_id},
        )
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
            len(scopes.local()),
            len(_resolved_scope(cloud_scope)),
        )
        return HybridRouter(
            policy=policy,
            local=local,
            cloud=cloud,
            name=f"hybrid:{model_id}",
        )

    def _check_health_registry(self, shared_fixed: FixedRouter, model_id: str) -> None:
        """Report a composition that would split circuit state in two.

        Nothing here needs the collaborator -- the wrapped router carries its own
        -- so the value exists to catch a composition root that built the shared
        router with one health registry and the hybrid seam with another. That
        mistake is otherwise invisible: both halves work, and an endpoint simply
        stops sharing a circuit with itself.
        """
        if self._warned_health_mismatch:
            return
        carried = getattr(shared_fixed, "endpoint_health_registry", None)
        if carried is None or carried is self._health_registry:
            return
        self._warned_health_mismatch = True
        logger.warning(
            "hybrid composition health registry mismatch: model=%s; endpoint circuit "
            "state would not be shared with the shared router",
            model_id,
        )

    def _build_cloud(
        self,
        shared_fixed: FixedRouter,
        *,
        model_id: str,
        scope: frozenset[str] | Callable[[], frozenset[str]],
    ) -> Any | None:
        """Build the cloud side, or None when this model has no cloud candidate.

        The shared router is wrapped rather than copied, exactly as the local side
        does, because the scope is what bounds this domain and it bounds
        selection and both fallback loops alike: a failed cloud attempt cannot
        walk into the local fleet. Wrapping also keeps an alias resolvable
        through the router that knows it, keeps the registered weights as the
        operator's, and makes a route refresh visible here the moment it is
        visible in the shared table. A copy gets all three wrong: it stamps each
        alias key as its own canonical id, it feeds normalized weights in as the
        baseline an admin override is applied to, and it freezes the routes.

        Reading the shared router live is also what keeps admin weight overrides
        working, since ``weight_override_resolver`` is attached to the shared
        router after the registry is built.

        A ``RouteWiseRouter`` is not stood up by default: a ``fixed`` model's
        cloud algorithm is the operator's configured weights, which the shared
        router already implements. ``cloud_backend`` is the seam for a
        deployment that wants RouteWise there instead; the cloud *role* is what
        the hybrid router is typed against, so choosing the algorithm is a
        composition-root decision rather than an edit here.
        """
        if not _resolved_scope(scope):
            return None
        if self._cloud_backend is not None:
            return self._cloud_backend(shared_fixed, model_id, scope)
        return FixedCloudBackend(
            shared_fixed,
            endpoint_scope=scope,
            model_scope={model_id},
        )


def _resolved_scope(
    scope: frozenset[str] | Callable[[], frozenset[str]],
) -> frozenset[str]:
    """Read a declared or live scope once, for emptiness checks and logging."""
    return frozenset(scope() if callable(scope) else scope)


class _LiveDomainScopes:
    """One model's local and cloud endpoint sets, read from the live route table.

    The split is a property of the routes registered for the model *now*, not of
    the moment the hybrid router was built. The policy that chooses a domain, the
    two backends that bound their own dispatch, and feedback attribution all read
    through this object, so none of them can act on a different generation of the
    same table.

    ``cloud`` is the complement of ``local`` over the model's registered
    endpoints. Weights are not consulted: a route parked at weight 0 is still
    part of the model's declared range, and dropping it would turn "temporarily
    parked" into "routed to the other domain".
    """

    __slots__ = ("_is_local", "_model_id", "_router")

    def __init__(
        self,
        router: FixedRouter,
        model_id: str,
        *,
        is_local: Callable[[Any, str], bool] | None = None,
    ) -> None:
        self._router = router
        self._model_id = model_id
        self._is_local = is_local

    def local(self) -> frozenset[str]:
        """Return the model's locally run endpoints."""
        return local_endpoint_scope(self._router, self._model_id, is_local=self._is_local)

    def cloud(self) -> frozenset[str]:
        """Return the model's endpoints that are not locally run."""
        return self._registered() - self.local()

    def _registered(self) -> frozenset[str]:
        """Return every endpoint id currently registered for this model."""
        route = self._router.routes.get(self._model_id)
        if route is None or not route.adapters:
            return frozenset()
        return frozenset(
            str(adapter.config.endpoint_id)
            for adapter, _weight in route.adapters
            if getattr(adapter.config, "endpoint_id", None)
        )
