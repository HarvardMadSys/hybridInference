"""Routing backends: the interchangeable execution domains of a hybrid router.

A backend exposes one execution capability -- a local inference fleet or a
cloud provider set -- behind the same request contract as every serving router
(:class:`routing.protocols.RouterProtocol`). The backend owns no scheduling
policy: it answers requests it is given, reports the routing metadata it
already produced, and forwards feedback to the collaborators it wraps.

Two implementations ship here:

``LocalBackend``
    Wraps an existing, already-scoped local router (``FixedRouter`` or any
    other ``RouterProtocol``). It adds no queue, no resource reservation, no
    token accounting and no cancellation protocol: the local execution path and
    its error semantics are reused as they are.

``RouteWiseCloudBackend``
    Wraps an existing ``RouteWiseRouter``. Selection, fallback, quota and
    concurrency management and streaming stay in that router; this class
    contributes the candidate range and the delegation.

Candidate ranges are explicit construction inputs. A cloud backend built over a
table that also contains local endpoints binds a
:class:`routing.route_scope.RouteScopeView` so primaries, fallbacks and the
router's own background probes cannot reach outside the declared scope. Nothing
here infers ownership from a hostname, URL or provider name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from routing.route_scope import (
    RouteScopeView,
    adapter_in_endpoint_scope,
    endpoint_ids_in_view,
    scope_view_for_endpoints,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

    from routing.protocols import RouterProtocol, RoutingRequestOptions
    from routing.route_table import RouteTableView
    from routing.routers import RoutingObservation
    from routing.routewise.router import RouteWiseRouter

__all__ = [
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
        **params: Any,
    ) -> dict[str, Any]:
        """Serve one non-streaming request inside this backend's range."""
        ...

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
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


class RoutingBackendBase:
    """Shared delegation for backends that wrap one existing router.

    Concrete backends add their identity rules on top (``owns_observation``);
    everything else is forwarded verbatim, so wrapping cannot silently drop an
    operation the router already supported while the wrapped router keeps
    ownership of its lifecycle, its adapters and its background work.
    """

    def __init__(self, router: RouterProtocol, *, name: str) -> None:
        missing = [method for method in _REQUIRED_ROUTER_METHODS if not hasattr(router, method)]
        if missing:
            raise TypeError(
                f"{type(self).__name__} requires a router exposing "
                f"{', '.join(_REQUIRED_ROUTER_METHODS)}; "
                f"{type(router).__name__} is missing {', '.join(missing)}"
            )
        self._router = router
        self._name = name
        self._started = False

    @property
    def name(self) -> str:
        """Return the stable identity of this backend in its composition."""
        return self._name

    @property
    def router(self) -> RouterProtocol:
        """Return the wrapped router."""
        return self._router

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
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

    async def start(self) -> None:
        """Start the wrapped router's background work, once.

        Wrapping an already-started router must not start a second set of
        maintenance tasks, so this is a no-op when it was not the caller that
        started it. Only a start performed here is undone by :meth:`stop`.
        """
        if self._started:
            return
        start = getattr(self._router, "start", None)
        if not callable(start):
            return
        await start()
        self._started = True

    async def stop(self) -> None:
        """Stop background work this backend started."""
        if not self._started:
            return
        stop = getattr(self._router, "stop", None)
        if callable(stop):
            await stop()
        self._started = False


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
        name: Backend identity reported in routing metadata and diagnostics.
    """

    def __init__(self, router: RouterProtocol, *, name: str = "local") -> None:
        super().__init__(router, name=name)

    def owns_observation(self, obs: RoutingObservation) -> bool:
        """Return True: a local backend owns every observation routed to it.

        The hybrid router only hands an observation to a backend the selection
        policy chose or that claimed the endpoint, so the local side does not
        re-derive a range it was already constructed with.
        """
        return True


class RouteWiseCloudBackend(RoutingBackendBase):
    """Cloud execution domain implemented by the existing RouteWise router.

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
    ) -> None:
        if not endpoint_scope:
            raise ValueError(
                "RouteWiseCloudBackend requires a non-empty endpoint_scope; "
                "declaring no cloud endpoints would let the backend fall back to "
                "every endpoint in the process route table"
            )
        super().__init__(router, name=name)
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
        """Rebuild route-derived state from a fresh projection of the table.

        A new view instance is bound so the wrapped router re-reads the source
        table instead of a cached snapshot; the wrapped router's own refresh
        keeps its synchronization boundary and re-derives its state.
        """
        self._view = self._build_view()
        self._allowed_endpoints_cache = None
        self._bind_view()

    def canonical_id(self, model_id: str) -> str:
        """Resolve ``model_id`` through the cloud candidate view."""
        return self._view.canonical_id(model_id)

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
