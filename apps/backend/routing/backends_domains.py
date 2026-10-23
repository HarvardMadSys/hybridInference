"""The two deployment-ownership domains a hybrid composition dispatches between.

LocalBackend wraps an existing, already-scoped local router and adds no admission
control, queue, reservation, token accounting or cancellation protocol: whatever
the local path does today is what it does here.

CloudBackend is the cloud execution role HybridRouter dispatches to. It is
defined by the role, not by one selection algorithm, and it is abstract on
purpose: ownership cannot be defaulted usefully, and a wrong default would only
appear on the feedback path, after a dispatch record was evicted. FixedCloudBackend
is the fixed-router implementation of that role.

Both domains own their candidate range explicitly. A composition whose route
table can change under it cannot freeze the split at construction, so a declared
range may be a live callable and is re-derived on every refresh.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from routing.backends_contract import _adapters_in_router, _resolve_endpoint_scope
from routing.backends_tree import TreeBackend
from routing.endpoints import endpoint_id_for_adapter
from routing.route_scope import ObservationScope

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from routing.decisions import RoutingTarget
    from routing.protocols import RouterProtocol
    from routing.routers import RoutingObservation

__all__ = [
    "CloudBackend",
    "FixedCloudBackend",
    "LocalBackend",
]


class LocalBackend(TreeBackend):
    """Local execution domain: an existing, already-scoped local router.

    The caller composes the local candidate set by building the wrapped router
    with its local adapters -- the same way the production bootstrap already
    populates a ``FixedRouter``. This class deliberately adds no admission
    control: no queue, no reservation, no token accounting, no TTFT model and
    no new GPU cancellation protocol. Whatever the local path does today is
    what it does here.

    Args:
        router: The local execution path (typically a ``FixedRouter``
            registered with local adapters only).
        endpoint_scope: Canonical endpoint ids and/or provider labels this
            backend serves. Optional, but a hybrid composition should always
            declare it: two backends that both claim every endpoint cannot be
            told apart from an observation alone, so feedback for a cloud
            request would also reach this local router. May also be a callable
            returning the current set, for a composition whose route table is
            edited while it runs; the callable is re-read on every refresh.
        model_scope: Optional canonical model ids this backend owns.
        name: Backend identity reported in routing metadata and diagnostics.
        manage_lifecycle: When True this backend starts and stops the wrapped
            router. Default False: the composition root that built the router
            owns it.
    """

    def __init__(
        self,
        router: RouterProtocol,
        *,
        endpoint_scope: Collection[str] | Callable[[], Collection[str] | None] | None = None,
        model_scope: Collection[str] | None = None,
        name: str = "local",
        manage_lifecycle: bool = False,
    ) -> None:
        super().__init__(router, name=name, manage_lifecycle=manage_lifecycle)
        self._model_scope = frozenset(model_scope) if model_scope is not None else None
        # Kept as given rather than resolved once: see refresh_route_table.
        self._endpoint_scope_spec = endpoint_scope
        self._observation_scope = self._build_observation_scope()

    def _build_observation_scope(self) -> ObservationScope:
        """Return the observation scope for the currently declared range.

        Seed the provider index from the wrapped router's own route table so a
        provider-label scope resolves to the endpoint ids that provider actually
        serves. Without this, declaring {"local-service"} would only match an
        observation that carried the label itself, and the canonical endpoint id
        an observation really carries would look out of scope.
        """
        return ObservationScope(
            _resolve_endpoint_scope(self._endpoint_scope_spec),
            adapters=_adapters_in_router(self._router),
        )

    @property
    def endpoint_scope(self) -> frozenset[str] | None:
        """Return the declared local endpoints and provider labels, if any."""
        return self._observation_scope.endpoint_scope

    @property
    def is_local(self) -> bool:
        """Return whether this backend is the local execution domain."""
        return True

    def owns_model(self, model_id: str) -> bool:
        """Return whether the local range covers ``model_id``."""
        if self._model_scope is None:
            return True
        return self.canonical_id(model_id) in self._model_scope

    def serves(self, model_id: str) -> bool:
        """Return whether this pool grants access to ``model_id``."""
        return self.owns_model(model_id)

    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return whether ``obs`` names an endpoint this local backend serves."""
        if not self.owns_model(obs.model_id):
            return False
        return self._observation_scope.includes_endpoint(obs.endpoint_id)

    def adapter_in_scope(self, adapter: Any) -> bool:
        """Return whether ``adapter`` is inside the declared local range."""
        return self._observation_scope.includes_adapter(adapter)

    def dispatch_scope(self, model_id: str) -> frozenset[str] | None:
        """Return the declared local endpoints, or None when none were declared.

        ``None`` is deliberate: an undeclared local scope means the wrapped
        router already holds only local candidates, so narrowing it again would
        be guesswork rather than a constraint the caller expressed.
        """
        endpoints = self._observation_scope.endpoint_scope
        if endpoints is None:
            return None
        return frozenset(
            endpoint_id
            for endpoint_id in self.allowed_endpoints()
            if self._observation_scope.includes_endpoint(endpoint_id)
        )

    def allowed_endpoints(self) -> frozenset[str]:
        """Return every endpoint id this local backend currently serves.

        Reads through the same dual-shape helper the provider index uses, so a
        router holding its table in ``route_table`` works here too.
        """
        return frozenset(
            endpoint_id_for_adapter(adapter) for adapter in _adapters_in_router(self._router)
        )

    def resolve_target(
        self,
        target: RoutingTarget,
        model_id: str,
    ) -> str | None:
        """Resolve a policy target to one endpoint this backend can dispatch to.

        An endpoint target is accepted only when it is inside the declared local
        range. A provider target is resolved through the scoped router's own
        selection, so weights, circuit admission and the prefill-aware draw
        decide among that provider's endpoints. ``None`` means out of range:
        the caller then runs the ordinary selection instead of fabricating a
        target the backend cannot serve.
        """
        if target.endpoint_id:
            return (
                target.endpoint_id
                if self._observation_scope.includes_endpoint(target.endpoint_id)
                else None
            )
        resolver = getattr(self._router, "preferred_endpoint_for_provider", None)
        if callable(resolver) and target.provider:
            endpoint_id = resolver(model_id, target.provider)
            # The resolver runs the wrapped router's own selection, which may
            # degrade to a candidate this domain does not own. Reporting it as
            # resolved would put an endpoint in the routing metadata that this
            # backend never dispatched to.
            if endpoint_id is not None and self._observation_scope.includes_endpoint(endpoint_id):
                return endpoint_id
        return None

    def refresh_route_table(self) -> None:
        """Delegate the refresh and re-derive the range it may have changed.

        Both halves are rebuilt, not just the provider index: the declared split
        itself can move under a route edit. An endpoint added to this model's
        route as a local server must start being claimed and dispatched to, and
        one that stopped being local must stop, or this backend keeps serving an
        endpoint the composition has already handed to the other domain.
        """
        refresh = getattr(self._router, "refresh_route_table", None)
        if callable(refresh):
            refresh()
        self._observation_scope = self._build_observation_scope()


class CloudBackend(TreeBackend, ABC):
    """Cloud execution domain: a set of remote providers behind one contract.

    This is the role ``HybridRouter`` dispatches a cloud request to, and the
    extension point a different selection algorithm joins at. It exists apart
    from :class:`LocalBackend` because the two sides are not symmetric: a local
    backend serves whatever its own router was populated with, while a cloud
    backend is constructed over a shared route table and must be told which
    endpoints of it are its own.

    Abstract on purpose. Everything except ownership is delegated by
    :class:`RoutingBackendBase`; ``owns_observation`` is left to the subclass
    because it cannot be defaulted usefully. A cloud implementation that cannot
    name the endpoints it served still passes the request contract, so a wrong
    default would only appear on the feedback path -- after a dispatch record
    was evicted, where a missing attribution silently costs a learning sample.
    Declaring it abstract makes an incomplete implementation fail when it is
    constructed rather than when it is first asked to attribute.

    Subclasses supply their own router and their own ownership rule;
    ``FixedCloudBackend`` and the optional ``RouteWiseCloudBackend`` are the two
    that ship here.

    Args:
        router: The router implementing cloud selection and execution.
        name: Backend identity reported in routing metadata and diagnostics.
        manage_lifecycle: When True this backend starts and stops the wrapped
            router. Default False: the composition root that built the router
            owns it.
    """

    @abstractmethod
    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return whether ``obs`` names an endpoint this cloud backend served.

        Runs on the feedback path without a surviving dispatch record, so it
        must be cheap and side-effect free.
        """
        ...

    @property
    def is_cloud(self) -> bool:
        """Return whether this backend is the cloud execution domain."""
        return True

    def owns_model(self, model_id: str) -> bool:
        """Return whether this backend serves ``model_id``.

        A cloud backend with no candidate-range projection answers for every
        model the wrapped router knows, which is the right default for a router
        that was built for exactly one domain. Override to narrow it.
        """
        return True


class FixedCloudBackend(CloudBackend):
    """Cloud execution domain over a router that already holds cloud candidates.

    The counterpart of :class:`LocalBackend`, and built the same way: it wraps
    the router that holds the model's routes and is bounded by its declared
    scope, so selection, fallback and execution are delegated whole while the
    candidate range stays inside the cloud domain. It is the cloud execution
    path for a ``fixed`` model, which selects by the operator's configured
    weights. A model that configures ``router: routewise`` is not routed here:
    RouteWise is a peer policy with its own full-pool entry point.

    Wrapping the shared router rather than a copy of its routes is what keeps
    this domain consistent with the rest of the process: an alias resolves
    through the router that knows it, the weights stay the ones the operator
    registered, and ``refresh_route_tables()`` is visible here the moment it is
    visible there. Handing this class a router whose route *is* the cloud range
    is still supported and still correct; it is simply not required, because the
    scope bounds dispatch and both fallback loops.

    Args:
        router: The router holding this model's routes.
        endpoint_scope: The endpoints (or provider labels) this backend may
            dispatch to. Recorded so target resolution and feedback attribution
            agree with execution. May be a callable returning the current set,
            for a composition whose route table is edited while it runs.
        model_scope: Optional canonical model ids this backend owns.
        name: Backend identity reported in routing metadata and diagnostics.
        manage_lifecycle: When True this backend starts and stops the wrapped
            router. Default False: the composition root owns it.
    """

    def __init__(
        self,
        router: RouterProtocol,
        *,
        endpoint_scope: Collection[str] | Callable[[], Collection[str] | None],
        model_scope: Collection[str] | None = None,
        name: str = "cloud",
        manage_lifecycle: bool = False,
    ) -> None:
        if not _resolve_endpoint_scope(endpoint_scope):
            raise ValueError(
                "FixedCloudBackend requires a non-empty endpoint_scope; with no "
                "cloud endpoints declared this backend cannot serve anything"
            )
        super().__init__(router, name=name, manage_lifecycle=manage_lifecycle)
        self._model_scope = frozenset(model_scope) if model_scope is not None else None
        # Kept as given rather than resolved once: see refresh_route_table.
        self._endpoint_scope_spec = endpoint_scope
        self._observation_scope = self._build_observation_scope()

    def _build_observation_scope(self) -> ObservationScope:
        """Return the ownership scope for the currently declared range.

        Built from the same provider index :class:`LocalBackend` uses, so a
        provider-label entry claims the endpoints that provider serves. A literal
        ``endpoint_id in scope`` test cannot see a label at all: declaring
        ``{"zai"}`` bounded dispatch but silently rejected every cloud target as
        out of range and dropped every cloud observation.
        """
        return ObservationScope(
            _resolve_endpoint_scope(self._endpoint_scope_spec),
            adapters=_adapters_in_router(self._router),
        )

    @property
    def endpoint_scope(self) -> frozenset[str]:
        """Return the declared cloud endpoints and provider labels."""
        return self._observation_scope.endpoint_scope or frozenset()

    def serves(self, model_id: str) -> bool:
        """Return whether the cloud range covers ``model_id``."""
        if self._model_scope is None:
            return True
        return self.canonical_id(model_id) in self._model_scope

    def dispatch_scope(self, model_id: str) -> frozenset[str] | None:
        """Return the declared cloud endpoints for ``model_id``."""
        return self._observation_scope.endpoint_scope if self.serves(model_id) else frozenset()

    def resolve_target(self, target: RoutingTarget, model_id: str) -> str | None:
        """Resolve a policy target inside the declared cloud range."""
        if not self.serves(model_id):
            return None
        if target.endpoint_id:
            scope = self._observation_scope
            return target.endpoint_id if scope.includes_endpoint(target.endpoint_id) else None
        resolver = getattr(self._router, "preferred_endpoint_for_provider", None)
        if callable(resolver) and target.provider:
            endpoint_id = resolver(model_id, target.provider)
            if endpoint_id is not None and self._observation_scope.includes_endpoint(endpoint_id):
                return endpoint_id
        return None

    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return whether ``obs`` names an endpoint inside the cloud range."""
        if not self.serves(obs.model_id):
            return False
        return self._observation_scope.includes_endpoint(obs.endpoint_id)

    def adapter_in_scope(self, adapter: Any) -> bool:
        """Return whether ``adapter`` is inside the declared cloud range."""
        return self._observation_scope.includes_adapter(adapter)

    def refresh_route_table(self) -> None:
        """Forward the refresh and re-derive the range it may have changed.

        The re-derivation matters as much as the delegation: a route edit can
        move an endpoint into or out of this domain, and a scope left at its
        construction value would keep a removed endpoint dispatchable and make a
        newly added one unreachable.
        """
        refresh = getattr(self._router, "refresh_route_table", None)
        if callable(refresh):
            refresh()
        self._observation_scope = self._build_observation_scope()
