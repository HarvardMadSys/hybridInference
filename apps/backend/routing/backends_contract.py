"""What a routing backend is, and the delegation every router wrapper shares.

RoutingBackend is the execution-domain contract HybridRouter dispatches against:
the request surface of routing.protocols.RouterProtocol, plus an identity and an
ownership predicate, so feedback reaches exactly one backend when two domains
could serve the same model id.

RoutingBackendBase implements that contract by forwarding to one wrapped router.
Concrete backends add their identity rules on top (owns_observation); everything
else is forwarded verbatim, so wrapping cannot silently drop an operation the
router already supported. Lifecycle is opt-in, and even then a backend only stops
work this backend itself started.

The route-introspection helpers here are how a backend reads the wrapped router
current candidate table. They are deliberately tolerant of both shapes in the
tree -- a router that is the table, and one that holds it in route_table --
because a frozen snapshot would keep dispatching to a removed endpoint and make a
newly added one unreachable.

See routing.backends for the composition entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from routing.endpoints import endpoint_id_for_adapter
from routing.route_scope import adapter_in_endpoint_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection

    from routing.decisions import RoutingTarget
    from routing.protocols import RouterProtocol, RoutingRequestOptions
    from routing.routers import RoutingObservation

__all__ = [
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


def _adapters_in_router(router: Any, model_id: str | None = None) -> tuple[Any, ...]:
    """Return every adapter the wrapped router currently routes to.

    Two supported shapes. ``FixedRouter`` *is* the table, so it answers
    ``iter_effective_routes()`` itself. ``RouteWiseRouter`` holds its table in
    ``route_table`` and probes that binding, so reading only the router-level
    protocol would leave the index empty and a provider-label scope unusable. A
    router exposing neither contributes no entries instead of failing
    construction.
    """
    source = _effective_route_source(router)
    routes = _routes_from(source)
    if model_id is not None:
        canonical = source.canonical_id(model_id)
        routes = tuple(route for route in routes if route.canonical_model_id == canonical)
    return tuple(adapter for route in routes for adapter, _weight in route.adapters)


def _effective_route_source(router: Any) -> Any:
    """Return the object that can enumerate routes for ``router``."""
    iterate = getattr(router, "iter_effective_routes", None)
    if callable(iterate):
        return router
    route_table = getattr(router, "route_table", None)
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

    def scope_endpoints(
        self, scope: Collection[str], model_id: str | None = None
    ) -> frozenset[str]:
        """Expand a declared range into the canonical endpoints it resolves to.

        A range may name canonical endpoint ids, provider labels, or both. Every
        range comparison in this module goes through here first, because
        comparing an endpoint id against raw entries rejects a provider label
        that legitimately covers it -- the same range expressed two ways would
        read as disjoint.
        """
        return frozenset(
            endpoint_id_for_adapter(adapter)
            for adapter in _adapters_in_router(self._router, model_id)
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
