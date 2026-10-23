"""The RouteWise cloud domain: a scoped view over an existing RouteWise router.

Selection, fallback, quota and concurrency accounting, hedging and streaming all
execute inside the wrapped RouteWiseRouter. This wrapper contributes two things
only: the candidate range and the delegation.

The wrapped router attach_route_table is called once with the scoped view, so
every route-derived structure it builds -- candidates, endpoint map, latency
profiles, resource pools and active probe targets -- is restricted to the
declared endpoint scope. It is not RouteWise architectural home: RouteWise is a
routing policy in its own right, able to choose across local and cloud
candidates, and a model with router: routewise keeps its existing entry point and
full candidate pool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.backends_domains import CloudBackend
from routing.route_scope import (
    RouteScopeView,
    adapter_in_endpoint_scope,
    endpoint_ids_in_view,
    scope_view_for_endpoints,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from routing.decisions import RoutingTarget
    from routing.route_table import RouteTableView
    from routing.routers import RoutingObservation
    from routing.routewise.router import RouteWiseRouter

__all__ = ["RouteWiseCloudBackend"]


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

    def serves(self, model_id: str) -> bool:
        """Return whether the scoped cloud view includes ``model_id``."""
        return self._view.includes_model(model_id)

    def dispatch_scope(self, model_id: str) -> frozenset[str] | None:
        """Return the declared cloud endpoints for ``model_id``."""
        if not self._view.includes_model(model_id):
            return frozenset()
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
