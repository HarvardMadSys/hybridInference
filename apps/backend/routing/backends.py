"""Routing backends: the interchangeable execution domains of a hybrid router.

A backend exposes one execution capability -- a local inference fleet or a
cloud provider set -- behind the same request contract as every serving router
(:class:`routing.protocols.RouterProtocol`). The backend owns no scheduling
policy: it answers requests it is given, reports the routing metadata it
already produced, and forwards feedback to the collaborators it wraps.

A backend sits on one side of a dispatch boundary, and the boundary has two
shapes. A :class:`LeafBackend` is the end of the recursion: it executes one bound
endpoint and has no selection of its own. A :class:`TreeBackend` holds a routing
subtree and delegates to a scoped internal router. ``LocalBackend`` and
``CloudBackend`` are deployment-ownership wrappers over that second shape; the
distinction that matters for a dispatch is which of the two a backend is, not
which domain it belongs to.

Two implementations ship here, one per side of the composition:

``LocalBackend``
    Wraps an existing, already-scoped local router (``FixedRouter`` or any
    other ``RouterProtocol``). It adds no queue, no resource reservation, no
    token accounting and no cancellation protocol: the local execution path and
    its error semantics are reused as they are.

``CloudBackend`` / ``RouteWiseCloudBackend``
    ``CloudBackend`` is the cloud execution role ``HybridRouter`` dispatches to;
    it is defined by the role, not by any one selection algorithm.
    ``RouteWiseCloudBackend`` is an optional scoped wrapper: given an explicit
    cloud range it delegates to an existing ``RouteWiseRouter``, whose selection,
    fallback, quota and concurrency management and streaming stay where they are.
    It is not RouteWise's architectural home -- RouteWise is a routing policy in
    its own right, able to choose across local and cloud candidates, and
    ``router: routewise`` keeps its existing entry point and full candidate pool.

Candidate ranges are explicit construction inputs. A cloud backend built over a
table that also contains local endpoints binds a
:class:`routing.route_scope.RouteScopeView` so primaries, fallbacks and the
router's own background probes cannot reach outside the declared scope. Nothing
here infers ownership from a hostname, URL or provider name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from routing.dispatch import (
    BackendDispatch,
    DelegatePool,
    DispatchMismatchError,
    EndpointBinding,
    ExecuteEndpoint,
    binding_for_adapter,
)
from routing.endpoints import endpoint_id_for_adapter
from routing.protocols import RoutingRequestOptions
from routing.route_scope import (
    ObservationScope,
    RouteScopeView,
    adapter_in_endpoint_scope,
    endpoint_ids_in_view,
    scope_view_for_endpoints,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection

    from routing.decisions import RoutingTarget
    from routing.protocols import RouterProtocol
    from routing.route_table import RouteTableView
    from routing.routers import RoutingObservation
    from routing.routewise.router import RouteWiseRouter

__all__ = [
    "CloudBackend",
    "FixedCloudBackend",
    "LeafBackend",
    "LocalBackend",
    "RouteWiseCloudBackend",
    "RoutingBackend",
    "RoutingBackendBase",
    "TreeBackend",
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


def _resolve_endpoint_scope(
    spec: Collection[str] | Callable[[], Collection[str] | None] | None,
) -> frozenset[str] | None:
    """Resolve a declared candidate range, which may be given as a live provider.

    A composition whose route table can change under it -- an admin route edit,
    followed by ``ModelRouterRegistry.refresh_route_tables()`` -- cannot freeze
    the local/cloud split at construction. Which endpoints a domain owns is a
    property of the *current* routes: a snapshot keeps dispatching to an endpoint
    that was removed and makes one that was added unreachable, so the split has to
    be re-derived from the same table the router reads.

    ``None`` means no scope was declared, which is distinct from an empty one:
    callers that require a non-empty range reject the former at construction.
    """
    if callable(spec):
        spec = spec()
    return frozenset(spec) if spec is not None else None


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

    def scope_endpoints(self, scope: Collection[str]) -> frozenset[str]:
        """Expand a declared range into the canonical endpoints it resolves to.

        A range may name canonical endpoint ids, provider labels, or both. Every
        range comparison in this module goes through here first, because
        comparing an endpoint id against raw entries rejects a provider label
        that legitimately covers it -- the same range expressed two ways would
        read as disjoint.
        """
        return frozenset(
            endpoint_id_for_adapter(adapter)
            for adapter in _adapters_in_router(self._router)
            if adapter_in_endpoint_scope(adapter, scope)
        )

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


class LeafBackend:
    """One adapter, bound to one endpoint, executed as it is.

    This is the end of the dispatch recursion and nothing more: it calls the
    adapter it was built with and hands back whatever that call produced. It
    keeps no queue, no reservation, no health record, no failed-attempt sample,
    no ``_routing`` block and no connection lifecycle, because every one of those
    already belongs to the router that chose this endpoint -- a second copy here
    would double-count them.

    Binding the adapter is what makes "execute exactly this endpoint" true rather
    than conventional. Nothing is looked up at dispatch time, so a route edit
    cannot redirect a request that is already in flight, and a caller releases
    the capacity it reserved through the reservation it already holds rather than
    through anything this object remembers.

    ``config`` is the adapter's own config object, not a copy: upstream metadata,
    endpoint identity and the response ``_routing`` block are all read from it,
    and a copy would silently stop matching the adapter it came from.

    The call signatures are the adapter's, not the router's. A leaf stands in for
    one adapter wherever an adapter is executed, so the router that wrapped it
    keeps doing the deciding, the admission, the accounting and the metadata.

    Args:
        adapter: The adapter that runs this endpoint.
        endpoint_id: Canonical endpoint id, recorded for identity and diagnostics.
        model_id: Canonical model the endpoint was resolved for.
        pool_id: Pool the binding was resolved inside, when the caller knows it.
        generation: Route-table generation the binding was taken from. Diagnostics
            only; nothing consults it to decide where a request goes.
        name: Identity reported in diagnostics.
    """

    __slots__ = (
        "_adapter",
        "_endpoint_id",
        "_generation",
        "_model_id",
        "_name",
        "_pool_id",
    )

    def __init__(
        self,
        adapter: Any,
        *,
        endpoint_id: str,
        model_id: str,
        pool_id: str | None = None,
        generation: int | None = None,
        name: str = "leaf",
    ) -> None:
        if not endpoint_id:
            raise ValueError("LeafBackend requires a non-empty endpoint_id")
        if not model_id:
            raise ValueError("LeafBackend requires a non-empty model_id")
        if getattr(adapter, "reports_leg_outcomes", False) is True:
            # A composite executor races or fans out across endpoints, so binding
            # it to one endpoint would be a claim it cannot keep -- and the
            # per-leg accounting it already does would sit under a boundary that
            # says one endpoint ran. Composition belongs above the leaf.
            #
            # Read as the literal True, like every other caller of this marker: a
            # test double built from MagicMock answers any attribute with a
            # truthy Mock, and a leaf must not refuse an ordinary adapter because
            # of what the double would have said.
            raise DispatchMismatchError(
                f"{type(adapter).__name__} executes across endpoints and cannot be bound "
                f"to the single endpoint {endpoint_id!r}; wrap each leg in its own leaf"
            )
        self._adapter = adapter
        self._endpoint_id = endpoint_id
        self._model_id = model_id
        self._pool_id = pool_id
        self._generation = generation
        self._name = name

    @classmethod
    def for_binding(cls, binding: EndpointBinding, *, name: str = "leaf") -> LeafBackend:
        """Return the leaf that executes ``binding``."""
        return cls(
            binding.adapter,
            endpoint_id=binding.endpoint_id,
            model_id=binding.model_id,
            pool_id=binding.pool_id,
            generation=binding.generation,
            name=name,
        )

    @property
    def config(self) -> Any:
        """Return the bound adapter's own config object."""
        return self._adapter.config

    @property
    def adapter(self) -> Any:
        """Return the bound adapter."""
        return self._adapter

    @property
    def endpoint_id(self) -> str:
        """Return the one endpoint this leaf executes."""
        return self._endpoint_id

    @property
    def model_id(self) -> str:
        """Return the model the endpoint was resolved for."""
        return self._model_id

    @property
    def pool_id(self) -> str | None:
        """Return the pool the binding was resolved inside, if it was named."""
        return self._pool_id

    @property
    def generation(self) -> int | None:
        """Return the route-table generation the binding was taken from."""
        return self._generation

    @property
    def name(self) -> str:
        """Return this leaf's identity in diagnostics."""
        return self._name

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        """Run the bound adapter and return its response unchanged."""
        return await self._adapter.chat_completion(messages, **params)

    def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Return the bound adapter's own iterator.

        A plain ``def`` on purpose: wrapping it in another generator would insert
        a frame between the caller and the upstream stream, which changes when the
        connection is opened and closed and can swallow a cancellation.
        """
        return self._adapter.stream_chat_completion(messages, **params)

    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return whether ``obs`` names the endpoint this leaf executed."""
        return obs.model_id == self._model_id and obs.endpoint_id == self._endpoint_id

    def owns_model(self, model_id: str) -> bool:
        """Return whether the bound endpoint belongs to ``model_id``."""
        return model_id == self._model_id

    def canonical_id(self, model_id: str) -> str:
        """Return ``model_id``; a leaf has no route table to resolve aliases in."""
        return model_id

    def serves(self, model_id: str) -> bool:
        """Return whether this leaf can serve ``model_id``."""
        return self.owns_model(model_id)

    def resolve_target(self, target: RoutingTarget, model_id: str) -> str | None:
        """Resolve a target, which here can only be the bound endpoint."""
        return self._endpoint_id if target.endpoint_id == self._endpoint_id else None

    def dispatch_scope(self, model_id: str) -> frozenset[str]:
        """Return the single-endpoint range this leaf may dispatch to."""
        return frozenset({self._endpoint_id})

    def check_instruction(self, instruction: BackendDispatch, model_id: str) -> None:
        """Refuse anything but an exact dispatch of the bound endpoint."""
        if isinstance(instruction, ExecuteEndpoint):
            bound = instruction.binding.endpoint_id
            if bound != self._endpoint_id:
                raise DispatchMismatchError(
                    f"{type(self).__name__} {self._name!r} is bound to {self._endpoint_id!r} "
                    f"and was asked to execute {bound!r}"
                )
            return
        raise DispatchMismatchError(
            f"{type(self).__name__} {self._name!r} executes one endpoint and has no "
            f"pool to delegate to, but was handed pool {instruction.pool_id!r}"
        )


class TreeBackend(RoutingBackendBase):
    """A routing subtree: the request is delegated to a scoped internal router.

    This backend is the entry to a pool. It carries the pool's identity and its
    candidate scope, and it hands selection to the router it wraps, which keeps
    its own retries, hedging and admission. What it does not do is reimplement
    any of that.

    An exact instruction is still honored inside the declared scope -- that is
    what the existing local and cloud wrappers already do, and their behavior is
    unchanged -- but a binding for an endpoint outside the scope is refused
    before any upstream I/O, because the alternative is a subtree quietly serving
    a request it was not granted.

    Args:
        router: The router implementing this pool's selection and execution.
        name: Backend identity reported in routing metadata and diagnostics.
        pool_id: Identity a delegation must name to reach this pool. Defaults to
            ``name``: a backend built without an explicit pool is its own pool.
        manage_lifecycle: When True this backend starts and stops the wrapped
            router. Default False: the composition root owns it.
    """

    def __init__(
        self,
        router: RouterProtocol,
        *,
        name: str = "tree",
        pool_id: str | None = None,
        manage_lifecycle: bool = False,
    ) -> None:
        super().__init__(router, name=name, manage_lifecycle=manage_lifecycle)
        self._pool_id = pool_id or name

    @property
    def pool_id(self) -> str:
        """Return the identity a delegation must name to reach this pool."""
        return self._pool_id

    def dispatch_scope(self, model_id: str) -> frozenset[str] | None:
        """Return the candidate range this pool may dispatch inside.

        ``None`` means the pool declares no range of its own and the wrapped
        router's full table is its scope. Subclasses that know their range
        override this.
        """
        return None

    def check_instruction(self, instruction: BackendDispatch, model_id: str) -> None:
        """Refuse a delegation to another pool, or a binding outside this range."""
        if isinstance(instruction, DelegatePool):
            if instruction.pool_id != self._pool_id:
                raise DispatchMismatchError(
                    f"{type(self).__name__} {self.name!r} is pool {self._pool_id!r} and was "
                    f"asked to serve pool {instruction.pool_id!r}"
                )
            return
        scope = self.dispatch_scope(model_id)
        if scope is None:
            return
        # Both sides normalized to canonical endpoints first: the declared range
        # may be expressed as provider labels, and the binding names an endpoint.
        allowed = self.scope_endpoints(scope)
        bound = instruction.binding.endpoint_id
        if bound not in allowed:
            raise DispatchMismatchError(
                f"{type(self).__name__} {self.name!r} may dispatch inside {sorted(allowed)} "
                f"and was asked to execute {bound!r}"
            )

    def binding_for(self, model_id: str, endpoint_id: str) -> EndpointBinding | None:
        """Return the execution binding for one endpoint inside this pool.

        The endpoint is looked up in the pool's own route table, so a binding can
        only name something this pool actually serves. ``None`` means it serves
        no such endpoint, which a caller must read as "not dispatchable here"
        rather than as a licence to choose another one.
        """
        for adapter in _adapters_in_router(self._router):
            if endpoint_id_for_adapter(adapter) == endpoint_id:
                return binding_for_adapter(adapter, model_id=model_id, pool_id=self._pool_id)
        return None

    def _scoped_options(
        self,
        routing_options: RoutingRequestOptions | None,
        model_id: str,
    ) -> RoutingRequestOptions | None:
        """Return options narrowed to the range this pool is allowed to use.

        A delegation grants selection *inside this pool*, so the pool bounds its
        own selection instead of trusting the caller to have done it. The
        effective range is the intersection of this pool's declared range and
        whatever range the caller already imposed -- a child scope may narrow the
        parent's grant, never widen it.

        An empty intersection means the caller granted nothing this pool can
        serve. That is a composition error, not an upstream failure, and it is
        reported as one before anything is sent.
        """
        declared = self.dispatch_scope(model_id)
        if declared is None:
            return routing_options
        allowed = self.scope_endpoints(declared)
        imposed = routing_options.endpoint_scope if routing_options is not None else None
        effective = allowed if imposed is None else (self.scope_endpoints(imposed) & allowed)
        if not effective:
            raise DispatchMismatchError(
                f"{type(self).__name__} {self.name!r} may dispatch inside {sorted(allowed)} "
                f"and was restricted to {sorted(imposed or ())}"
            )
        if routing_options is None:
            return RoutingRequestOptions(endpoint_scope=effective)
        if routing_options.endpoint_scope == effective:
            return routing_options
        return replace(routing_options, endpoint_scope=effective)

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        target: RoutingTarget | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Serve one request inside this pool's range."""
        return await self._router.chat_completion(
            model_id,
            messages,
            routing_options=self._scoped_options(routing_options, model_id),
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
        """Serve one streaming request inside this pool's range."""
        return self._router.stream_chat_completion(
            model_id,
            messages,
            routing_options=self._scoped_options(routing_options, model_id),
            **params,
        )


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
        return self._observation_scope.endpoint_scope if self.serves(model_id) else None

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
