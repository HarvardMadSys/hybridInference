"""Candidate-range views over a read-only route table.

Every backend in a hybrid composition must be told which candidates it may
dispatch to. ``RouteTableView`` is already the composition port for that range,
but the process-scoped table carries every endpoint of every model, so a router
handed the whole table can select a candidate outside its own domain -- most
visibly RouteWise's active latency probe, which walks every endpoint it
classified.

``RouteScopeView`` narrows a table to one backend's candidates without copying
adapter state and without exposing a mutable route table: it implements the
same ``RouteTableView`` port, filters the immutable ``EffectiveRoute`` snapshot
by model and by endpoint, and delegates canonical-id resolution to the source.

Scope is expressed by endpoint identity or provider label, never inferred from a
hostname or URL. Callers decide what "local" and "cloud" mean for their
deployment and pass that decision in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from routing.endpoints import endpoint_id_for_adapter
from routing.route_table import EffectiveRoute, RouteTableView

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from serving.adapters.base import BaseAdapter

__all__ = [
    "RouteScopeView",
    "adapter_in_endpoint_scope",
    "endpoint_ids_in_view",
    "scope_view_for_endpoints",
]


def adapter_in_endpoint_scope(
    adapter: BaseAdapter,
    endpoint_scope: Collection[str],
) -> bool:
    """Return whether ``adapter`` belongs to an explicit endpoint/provider set.

    The set holds canonical endpoint ids (``{model}:{location}``) and provider
    labels. An empty set selects nothing: a backend whose range nobody declared
    must not silently fall back to the whole fleet.
    """
    if not endpoint_scope:
        return False
    return (
        endpoint_id_for_adapter(adapter) in endpoint_scope
        or getattr(adapter.config, "provider", None) in endpoint_scope
    )


def endpoint_ids_in_view(view: RouteTableView) -> frozenset[str]:
    """Return every endpoint id reachable through ``view``.

    Used for observation ownership, which is checked against the *intended*
    range: a candidate the circuit breaker or an empty weight currently
    excludes still belongs to its backend, so its feedback must land there
    instead of being broadcast to a backend that never serves it.
    """
    endpoints: set[str] = set()
    for route in view.iter_effective_routes():
        for adapter, _weight in route.adapters:
            endpoints.add(endpoint_id_for_adapter(adapter))
    return frozenset(endpoints)


def scope_view_for_endpoints(
    source: RouteTableView,
    endpoint_scope: Collection[str],
    *,
    model_scope: Collection[str] | None = None,
) -> RouteScopeView:
    """Build the candidate-range view a scoped backend should be bound to."""
    allowed = frozenset(endpoint_scope)
    return RouteScopeView(
        source,
        filter=lambda adapter, _weight: adapter_in_endpoint_scope(adapter, allowed),
        model_scope=model_scope,
    )


class RouteScopeView:
    """Read-only ``RouteTableView`` restricted to one backend's candidates.

    Args:
        source: The table to project. Never mutated, never retained under an
            extra lock: filtering runs over the immutable snapshot the source
            already returns.
        filter: Candidate predicate applied to every ``(adapter, weight)`` pair.
            A route whose candidates are all filtered out is omitted entirely,
            so a scoped router cannot fall back to a model it does not own.
        model_scope: Optional canonical model ids this view exposes. ``None``
            keeps every model the source publishes.

    The projected snapshot is cached because ``RouteWiseRouter`` re-iterates the
    bound table on every route-table refresh and every probe-target walk. The
    cache never outlives a refresh: a backend rebinds a fresh view whenever it
    rebuilds from the table, and ``clear_cache()`` is available for callers
    that mutate the source table in place.

    Captured weights are preserved rather than renormalized. A cloud adapter
    weighted 0.5 next to a local adapter weighted 0.5 keeps 0.5 inside a
    cloud-scoped view, because that number is its share of the pool the
    operator configured -- not a share of what survived the filter.
    """

    def __init__(
        self,
        source: RouteTableView,
        *,
        filter: Callable[[BaseAdapter, float], bool],
        model_scope: Collection[str] | None = None,
    ) -> None:
        self._source = source
        self._filter = filter
        self._model_scope = frozenset(model_scope) if model_scope is not None else None
        self._cache: tuple[EffectiveRoute, ...] | None = None

    @property
    def source(self) -> RouteTableView:
        """Return the unfiltered table this view projects."""
        return self._source

    @property
    def model_scope(self) -> frozenset[str] | None:
        """Return the models this view exposes, or ``None`` for every model."""
        return self._model_scope

    def includes_model(self, model_id: str) -> bool:
        """Return whether ``model_id`` resolves to a model inside the scope."""
        if self._model_scope is None:
            return True
        return self._source.canonical_id(model_id) in self._model_scope

    def includes_adapter(self, adapter: BaseAdapter, weight: float = 1.0) -> bool:
        """Return whether ``adapter`` is inside this view's candidate range."""
        return self._filter(adapter, weight)

    def clear_cache(self) -> None:
        """Drop the projected snapshot so the next read re-filters the source."""
        self._cache = None

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        """Return the immutable, scope-filtered route snapshot."""
        cached = self._cache
        if cached is not None:
            return cached
        source = self._source
        scope = self._model_scope
        projected: list[EffectiveRoute] = []
        for route in source.iter_effective_routes():
            if scope is not None and route.canonical_model_id not in scope:
                continue
            adapters = tuple(
                (adapter, weight)
                for adapter, weight in route.adapters
                if self._filter(adapter, weight)
            )
            if not adapters:
                continue
            projected.append(
                EffectiveRoute(
                    route_key=route.route_key,
                    canonical_model_id=route.canonical_model_id,
                    adapters=adapters,
                )
            )
        snapshot = tuple(projected)
        self._cache = snapshot
        return snapshot

    def canonical_id(self, model_id: str) -> str:
        """Resolve aliases through the source table."""
        return self._source.canonical_id(model_id)
