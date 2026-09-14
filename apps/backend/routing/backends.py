"""Routing backends: the interchangeable execution domains of a hybrid router.

A backend exposes one execution capability -- a local inference fleet or a
cloud provider set -- behind the same request contract as every serving router
(:class:`routing.protocols.RouterProtocol`). The backend owns no scheduling
policy: it answers requests it is given, reports the routing metadata it
already produced, and forwards feedback to the collaborators it wraps.

Two implementations ship here, one per side of the composition:

``LocalBackend``
    Wraps an existing, already-scoped local router (``FixedRouter`` or any
    other ``RouterProtocol``). It adds no queue, no resource reservation, no
    token accounting and no cancellation protocol: the local execution path and
    its error semantics are reused as they are.

``CloudBackend`` / ``RouteWiseCloudBackend``
    ``CloudBackend`` is the cloud execution role ``HybridRouter`` dispatches to;
    ``RouteWiseCloudBackend`` is its shipped implementation, wrapping an
    existing ``RouteWiseRouter``. Selection, fallback, quota and concurrency
    management and streaming stay in that router; the wrapper contributes the
    candidate range and the delegation.

Candidate ranges are explicit construction inputs. A cloud backend built over a
table that also contains local endpoints binds a
:class:`routing.route_scope.RouteScopeView` so primaries, fallbacks and the
router's own background probes cannot reach outside the declared scope. Nothing
here infers ownership from a hostname, URL or provider name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from routing.endpoints import endpoint_id_for_adapter
from routing.route_scope import (
    ObservationScope,
    RouteScopeView,
    adapter_in_endpoint_scope,
    endpoint_ids_in_view,
    scope_view_for_endpoints,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

    from routing.decisions import RoutingTarget
    from routing.protocols import RouterProtocol, RoutingRequestOptions
    from routing.route_table import RouteTableView
    from routing.routers import RoutingObservation
    from routing.routewise.router import RouteWiseRouter

__all__ = [
    "CloudBackend",
    "FixedCloudBackend",
    "LocalBackend",
    "RouteWiseCloudBackend",
    "RoutingBackend",
    "RoutingBackendBase",
]

#: Methods every router must expose to be usable as a backend execution path.
_REQUIRED_ROUTER_METHODS = (
    "chat_completion",
    "stream_chat_completion",
    "record_observation",
    "get_provider_status",
)


def _adapters_in_router(router: Any) -> tuple[Any, ...]:
    """Return every adapter the wrapped router currently routes to.

    Two supported shapes. ``FixedRouter`` *is* the table, so it answers
    ``iter_effective_routes()`` itself. ``RouteWiseRouter`` holds its table in
    ``route_table`` and probes that binding, so reading only the router-level
    protocol would leave the index empty and a provider-label scope unusable. A
    router exposing neither contributes no entries instead of failing
    construction.
    """
    routes = _routes_from(_effective_route_source(router))
    return tuple(adapter for route in routes for adapter, _weight in route.adapters)


def _effective_route_source(router: Any) -> Any:
    """Return the object that can enumerate routes for ``router``."""
    iterate = getattr(router, "iter_effective_routes", None)
    if callable(iterate):
        return router
    route_table = getattr(router, "route_table", None)
    if route_table is None:
        # One-release compatibility for the former attribute name.
        route_table = getattr(router, "fixed_router", None)
    return route_table if callable(getattr(route_table, "iter_effective_routes", None)) else None


def _routes_from(source: Any) -> tuple[Any, ...]:
    """Return the route snapshot of ``source``, or nothing if it is unusable."""
    if source is None:
        return ()
    try:
        return tuple(source.iter_effective_routes())
    except Exception:  # pragma: no cover - a router with an unusable view
        return ()


@runtime_checkable
class RoutingBackend(Protocol):
    """One execution domain a hybrid router can delegate a request to.

    The request surface is ``RouterProtocol`` verbatim: model id, messages,
    ``RoutingRequestOptions`` and generation parameters. A backend adds its
    identity and the ability to say whether an observation belongs to it, so
    feedback reaches exactly one owner when two domains could serve the same
    model id.
    """

    @property
    def name(self) -> str:
        """Return the stable identity of this backend in its composition."""
        ...

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        target: RoutingTarget | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Serve one non-streaming request inside this backend's range.

        ``target`` is the scheduling policy's preferred provider or endpoint.
        A backend that can serve it must honor it instead of running a fresh
        unconstrained selection; a backend that cannot must fall back to its own
        selection and say so, never silently accept an out-of-range target.
        ``None`` means no preference: the backend's own algorithm decides.
        """
        ...

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        target: RoutingTarget | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Serve one streaming request inside this backend's range."""
        ...

    def record_observation(self, obs: RoutingObservation) -> None:
        """Record feedback for a request this backend actually served."""
        ...

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return endpoint health and circuit state for this backend."""
        ...

    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return whether ``obs`` describes a request this backend served.

        Must be cheap and side-effect free: the hybrid router calls it on the
        feedback path to keep one observation from being counted twice.
        """
        ...

    # Composition metadata, read with ``getattr`` so a minimal backend double
    # stays usable. ``RoutingBackendBase`` sets both to False and each concrete
    # side flips its own, which is how ``HybridRouter`` checks that the
    # injection matches the side it was passed as.
    @property
    def is_local(self) -> bool:
        """Return whether this backend is the local execution domain."""
        ...

    @property
    def is_cloud(self) -> bool:
        """Return whether this backend is the cloud execution domain."""
        ...


class RoutingBackendBase:
    """Shared delegation for backends that wrap one existing router.

    Concrete backends add their identity rules on top (``owns_observation``);
    everything else is forwarded verbatim, so wrapping cannot silently drop an
    operation the router already supported.

    Lifecycle is opt-in. By default the composition root that constructed the
    wrapped router keeps owning it, and this backend neither starts nor stops
    it. ``manage_lifecycle=True`` transfers that responsibility here, and even
    then the backend only stops work this backend itself started.
    """

    def __init__(
        self,
        router: RouterProtocol,
        *,
        name: str,
        manage_lifecycle: bool = False,
    ) -> None:
        missing = [method for method in _REQUIRED_ROUTER_METHODS if not hasattr(router, method)]
        if missing:
            raise TypeError(
                f"{type(self).__name__} requires a router exposing "
                f"{', '.join(_REQUIRED_ROUTER_METHODS)}; "
                f"{type(router).__name__} is missing {', '.join(missing)}"
            )
        self._router = router
        self._name = name
        self._manage_lifecycle = manage_lifecycle
        self._started = False

    @property
    def name(self) -> str:
        """Return the stable identity of this backend in its composition."""
        return self._name

    @property
    def router(self) -> RouterProtocol:
        """Return the wrapped router."""
        return self._router

    @property
    def manages_lifecycle(self) -> bool:
        """Return whether this backend may start and stop the wrapped router."""
        return self._manage_lifecycle

    @property
    def is_local(self) -> bool:
        """Return whether this backend is the local execution domain."""
        return False

    @property
    def is_cloud(self) -> bool:
        """Return whether this backend is the cloud execution domain."""
        return False

    @property
    def started_here(self) -> bool:
        """Return whether this backend started the wrapped router itself."""
        return self._started

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        target: RoutingTarget | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Delegate one non-streaming request to the wrapped router."""
        return await self._router.chat_completion(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        target: RoutingTarget | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Delegate one streaming request to the wrapped router.

        Returns the router's own iterator rather than a wrapper generator: the
        chunks, their order, and the error and cancellation semantics of the
        underlying stream stay exactly as the router produced them.
        """
        return self._router.stream_chat_completion(
            model_id,
            messages,
            routing_options=routing_options,
            **params,
        )

    def resolve_target(self, target: RoutingTarget, model_id: str) -> str | None:
        """Resolve a policy target to an endpoint this backend can dispatch to.

        Default: nothing resolves, so a backend without a target-aware router
        serves the request with its own selection and reports the target as out
        of range rather than failing it.
        """
        return None

    def serves(self, model_id: str) -> bool:
        """Return whether this backend could serve ``model_id`` at all.

        The hybrid layer asks before offering a cross-backend fallback, so a
        backend whose range does not cover the model is skipped instead of
        becoming an attempt that is certain to fail.
        """
        return True

    def dispatch_scope(self, model_id: str) -> frozenset[str] | None:
        """Return the endpoints this backend may dispatch to for ``model_id``.

        The hybrid layer puts this on every dispatch it delegates, so the
        backend's *fallback* candidates stay inside its own domain too. Preferring
        one endpoint is not enough on its own: if that endpoint fails, the
        wrapped router walks the remaining route, and without a scope it would
        walk straight out of the domain.
        """
        return None

    def record_observation(self, obs: RoutingObservation) -> None:
        """Forward feedback to the wrapped router."""
        self._router.record_observation(obs)

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return the wrapped router's endpoint status snapshot."""
        return self._router.get_provider_status()

    def canonical_id(self, model_id: str) -> str:
        """Resolve ``model_id`` through the wrapped router when it can."""
        resolver = getattr(self._router, "canonical_id", None)
        if callable(resolver):
            return str(resolver(model_id))
        return model_id

    async def start(self) -> bool:
        """Start the wrapped router's background work when this backend owns it.

        Returns:
            ``True`` only when this call actually brought the background work up
            and therefore owns stopping it later. A backend that does not manage
            the lifecycle, or whose router was already running under another
            owner, returns ``False`` and never stops that work.
        """
        if not self._manage_lifecycle or self._started:
            return False
        start = getattr(self._router, "start", None)
        if not callable(start):
            return False
        started = await start()
        # A router may report that it was already running, in which case this
        # backend did not create the tasks and must not cancel them later.
        self._started = started is not False
        return self._started

    async def stop(self) -> bool:
        """Stop background work, but only what this backend started.

        Returns:
            ``True`` when this call stopped the wrapped router's work.
        """
        if not self._started:
            return False
        stop = getattr(self._router, "stop", None)
        if callable(stop):
            await stop()
        self._started = False
        return True


class LocalBackend(RoutingBackendBase):
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
            request would also reach this local router.
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
        endpoint_scope: Collection[str] | None = None,
        model_scope: Collection[str] | None = None,
        name: str = "local",
        manage_lifecycle: bool = False,
    ) -> None:
        super().__init__(router, name=name, manage_lifecycle=manage_lifecycle)
        self._model_scope = frozenset(model_scope) if model_scope is not None else None
        # Seed the provider index from the wrapped router's own route table so a
        # provider-label scope resolves to the endpoint ids that provider
        # actually serves. Without this, declaring {"local-service"} would only
        # match an observation that carried the label itself, and the canonical
        # endpoint id an observation really carries would look out of scope.
        self._observation_scope = ObservationScope(
            endpoint_scope,
            adapters=_adapters_in_router(router),
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
            return resolver(model_id, target.provider)
        return None

    def refresh_route_table(self) -> None:
        """Delegate the refresh and re-index the endpoints it may have changed."""
        refresh = getattr(self._router, "refresh_route_table", None)
        if callable(refresh):
            refresh()
        self._observation_scope.prime(_adapters_in_router(self._router))


class CloudBackend(RoutingBackendBase, ABC):
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

    ``RouteWiseCloudBackend`` is the shipped implementation; a different cloud
    algorithm subclasses this with its own router and its own ownership rule.

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

    The counterpart of :class:`LocalBackend`: where the local side wraps the
    shared router and is bounded by its declared scope, this side is given a
    router whose route *is* the cloud range, so selection, fallback and
    execution are delegated whole. It is the cloud algorithm for a ``fixed``
    model -- the operator's configured weights, not RouteWise's LP -- while
    :class:`RouteWiseCloudBackend` remains the algorithm for a model that
    configures ``router: routewise``.

    Args:
        router: A router holding only this backend's candidates.
        endpoint_scope: The endpoints that router may dispatch to. Recorded so
            target resolution and feedback attribution agree with execution.
        model_scope: Optional canonical model ids this backend owns.
        name: Backend identity reported in routing metadata and diagnostics.
        manage_lifecycle: When True this backend starts and stops the wrapped
            router. Default False: the composition root owns it.
    """

    def __init__(
        self,
        router: RouterProtocol,
        *,
        endpoint_scope: Collection[str],
        model_scope: Collection[str] | None = None,
        name: str = "cloud",
        manage_lifecycle: bool = False,
    ) -> None:
        if not endpoint_scope:
            raise ValueError(
                "FixedCloudBackend requires a non-empty endpoint_scope; with no "
                "cloud endpoints declared this backend cannot serve anything"
            )
        super().__init__(router, name=name, manage_lifecycle=manage_lifecycle)
        self._endpoint_scope = frozenset(endpoint_scope)
        self._model_scope = frozenset(model_scope) if model_scope is not None else None

    @property
    def endpoint_scope(self) -> frozenset[str]:
        """Return the declared cloud endpoints and provider labels."""
        return self._endpoint_scope

    def serves(self, model_id: str) -> bool:
        """Return whether the cloud range covers ``model_id``."""
        if self._model_scope is None:
            return True
        return self.canonical_id(model_id) in self._model_scope

    def dispatch_scope(self, model_id: str) -> frozenset[str] | None:
        """Return the declared cloud endpoints for ``model_id``."""
        return self._endpoint_scope if self.serves(model_id) else None

    def resolve_target(self, target: RoutingTarget, model_id: str) -> str | None:
        """Resolve a policy target inside the declared cloud range."""
        if not self.serves(model_id):
            return None
        if target.endpoint_id:
            return target.endpoint_id if target.endpoint_id in self._endpoint_scope else None
        resolver = getattr(self._router, "preferred_endpoint_for_provider", None)
        if callable(resolver) and target.provider:
            endpoint_id = resolver(model_id, target.provider)
            if endpoint_id is not None and endpoint_id in self._endpoint_scope:
                return endpoint_id
        return None

    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return whether ``obs`` names an endpoint inside the cloud range."""
        if not self.serves(obs.model_id):
            return False
        return obs.endpoint_id in self._endpoint_scope

    def adapter_in_scope(self, adapter: Any) -> bool:
        """Return whether ``adapter`` is inside the declared cloud range."""
        return adapter_in_endpoint_scope(adapter, self._endpoint_scope)


class RouteWiseCloudBackend(CloudBackend):
    """RouteWise implementation of the cloud execution role.

    Selection, fallback, quota/concurrency accounting, hedging and streaming
    all execute inside the wrapped :class:`RouteWiseRouter`. This class
    contributes two things only: the candidate range and the delegation.

    Args:
        router: The RouteWise router to delegate to.
        table: Full read-only table the cloud candidates live in.
        endpoint_scope: Canonical endpoint ids and/or provider labels this
            backend may dispatch to. Required and non-empty: a cloud backend
            must be told its range, and a table that also holds local endpoints
            must not leak them into primaries, fallbacks or background probes.
        model_scope: Optional canonical model ids this backend owns.
        name: Backend identity reported in routing metadata and diagnostics.
        manage_lifecycle: When True this backend starts and stops the wrapped
            router. Default False: the composition root that built the router
            owns it, and this wrapper leaves the router's background work alone.

    The wrapped router's ``attach_route_table`` is called once with the scoped
    view, so every route-derived structure it builds -- candidates, endpoint
    map, latency profiles, resource pools and active probe targets -- is
    restricted to ``endpoint_scope``.
    """

    def __init__(
        self,
        router: RouteWiseRouter,
        *,
        table: RouteTableView,
        endpoint_scope: Collection[str],
        model_scope: Collection[str] | None = None,
        name: str = "cloud",
        manage_lifecycle: bool = False,
    ) -> None:
        if not endpoint_scope:
            raise ValueError(
                "RouteWiseCloudBackend requires a non-empty endpoint_scope; "
                "declaring no cloud endpoints would let the backend fall back to "
                "every endpoint in the process route table"
            )
        super().__init__(router, name=name, manage_lifecycle=manage_lifecycle)
        self._table = table
        self._endpoint_scope = frozenset(endpoint_scope)
        self._model_scope = frozenset(model_scope) if model_scope is not None else None
        self._view = self._build_view()
        self._allowed_endpoints_cache: frozenset[str] | None = None
        self._bind_view()

    @property
    def table(self) -> RouteTableView:
        """Return the full table this backend's candidates were drawn from."""
        return self._table

    @property
    def view(self) -> RouteScopeView:
        """Return the scope-restricted view bound to the wrapped router."""
        return self._view

    @property
    def endpoint_scope(self) -> frozenset[str]:
        """Return the declared cloud endpoints and provider labels."""
        return self._endpoint_scope

    def _build_view(self) -> RouteScopeView:
        """Project the declared cloud range out of the full route table."""
        return scope_view_for_endpoints(
            self._table,
            self._endpoint_scope,
            model_scope=self._model_scope,
        )

    def _bind_view(self) -> None:
        """Bind the current scoped view to the wrapped router."""
        attach = getattr(self._router, "attach_route_table", None)
        if attach is None:
            attach = getattr(self._router, "attach_fixed_router", None)
        if callable(attach):
            attach(self._view, model_scope=self._model_scope)

    def refresh_route_table(self) -> None:
        """Rebuild route-derived state from the current route table.

        Mirrors ``RouteWiseRouter.refresh_route_table`` rather than re-binding
        the view: the view is live, so dropping its projection cache is enough
        to make the router's own refresh re-read the source table. Re-binding
        through ``attach_route_table`` would take that method's *attach* path,
        which clears ``pending_prefix_cache`` -- discarding the prefix-cache
        feedback of every in-flight request, a side effect a refresh does not
        have today.
        """
        self._view.clear_cache()
        self._allowed_endpoints_cache = None
        refresh = getattr(self._router, "refresh_route_table", None)
        if callable(refresh):
            refresh()

    def canonical_id(self, model_id: str) -> str:
        """Resolve ``model_id`` through the cloud candidate view."""
        return self._view.canonical_id(model_id)

    def dispatch_scope(self, model_id: str) -> frozenset[str] | None:
        """Return the declared cloud endpoints for ``model_id``."""
        if not self._view.includes_model(model_id):
            return None
        return self.allowed_endpoints()

    def resolve_target(self, target: RoutingTarget, model_id: str) -> str | None:
        """Resolve a policy target inside the declared cloud range.

        Endpoint targets are checked against the same scoped view that bounds
        every other candidate decision here, so a local endpoint can never be
        accepted as a cloud target. Provider targets go through the wrapped
        router's own scope-aware resolution.
        """
        if target.endpoint_id:
            if not self._view.includes_model(model_id):
                return None
            return target.endpoint_id if target.endpoint_id in self.allowed_endpoints() else None
        resolver = getattr(self._router, "preferred_endpoint_for_provider", None)
        if callable(resolver) and target.provider:
            endpoint_id = resolver(model_id, target.provider)
            if endpoint_id is not None and endpoint_id in self.allowed_endpoints():
                return endpoint_id
        return None

    def owns_model(self, model_id: str) -> bool:
        """Return whether the cloud range covers ``model_id``."""
        return self._view.includes_model(model_id)

    def allowed_endpoints(self) -> frozenset[str]:
        """Return every endpoint id inside the declared cloud range."""
        cached = self._allowed_endpoints_cache
        if cached is not None:
            return cached
        allowed = endpoint_ids_in_view(self._view)
        self._allowed_endpoints_cache = allowed
        return allowed

    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return whether ``obs`` belongs to the declared cloud range."""
        if self._model_scope is not None and not self.owns_model(obs.model_id):
            return False
        return obs.endpoint_id in self.allowed_endpoints()

    def adapter_in_scope(self, adapter: Any, weight: float = 1.0) -> bool:
        """Return whether ``adapter`` is inside the declared cloud range."""
        if not adapter_in_endpoint_scope(adapter, self._endpoint_scope):
            return False
        return self._view.includes_adapter(adapter, weight)
