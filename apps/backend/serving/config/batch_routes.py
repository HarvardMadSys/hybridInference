"""Which routes batch work may use: a per-route admin choice over a locality default.

A batch is work nobody waits on, so by default it runs only on the servers this
gateway runs itself and never spends a cloud provider's quota or money: a route
is batch-allowed when it is local (``is_local_endpoint``: its declared
``deployment_type``, or for ``auto`` its host). An administrator can allow or
forbid any route for batch work (``provider_route_batch_policies``); a route
with no stored choice follows its locality, so a route that moves keeps
following where it now points.

Interactive requests are untouched. A batch request is narrowed to its model's
batch-allowed routes through the routers' own per-request range
(``RoutingRequestOptions.endpoint_scope``), which every selection, retry,
fallback and offload path already honors, so routing gains no new rule. A batch
whose model has no batch-allowed route is refused when it is submitted, and an
item already queued fails with the same reason when its turn comes
(:func:`batch_route_refusal`), rather than waiting for a route that will never
come.

Routes are named by their route id (``routing.endpoints.route_id_for_adapter``)
under the model's canonical id, as the per-route admin state is. The snapshot is
read synchronously during routing, like ``RouteStateResolver``.
"""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Any

from routing.endpoints import endpoint_id_for_adapter, route_id_for_adapter
from serving.adapters.upstream_limiter import is_local_endpoint

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter

#: The item error code of a batch request its model cannot serve as batch work.
BATCH_ROUTE_UNAVAILABLE = "batch_route_unavailable"


def default_batch_allowed(adapter: BaseAdapter) -> bool:
    """Whether a route with no stored choice may serve batch work: a local one."""
    config = adapter.config
    return is_local_endpoint(
        getattr(config, "base_url", None),
        deployment_type=getattr(config, "deployment_type", "auto"),
    )


class BatchRouteResolver:
    """Hold the per-route batch choices for synchronous reads during routing."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._choices: dict[tuple[str, str], bool] = {}
        self._lock = RLock()
        # "never" until a load is attempted, "failed" while none has succeeded.
        self._load_state = "never"

    @property
    def ready(self) -> bool:
        """Whether admin writes can build on the snapshot.

        False from a failed first load until a load succeeds: an unread
        snapshot is unknown, not empty. A later failed reload keeps the last
        good snapshot, and a resolver never asked to load holds what it is given.
        """
        with self._lock:
            return self._load_state != "failed"

    async def load_all(self) -> bool:
        """Reload every stored choice and return whether the snapshot changed."""
        try:
            rows = await self._store.list_all_provider_route_batch_policies()
        except Exception:
            with self._lock:
                if self._load_state == "never":
                    self._load_state = "failed"
            raise
        choices = {
            (str(row["model_id"]), str(row["route_id"])): bool(row["batch_allowed"]) for row in rows
        }
        with self._lock:
            changed = choices != self._choices
            self._choices = choices
            self._load_state = "loaded"
            return changed

    def choice(self, model_id: str, route_id: str) -> bool | None:
        """Return the stored choice for a route, or None when it follows its locality."""
        with self._lock:
            return self._choices.get((model_id, route_id))

    def set_choice(self, model_id: str, route_id: str, allowed: bool | None) -> None:
        """Update the snapshot after a successful admin write (None clears it)."""
        with self._lock:
            if allowed is None:
                self._choices.pop((model_id, route_id), None)
            else:
                self._choices[(model_id, route_id)] = allowed

    def clear_route(self, model_id: str, route_id: str) -> None:
        """Forget the choice of a deleted route."""
        self.set_choice(model_id, route_id, None)

    def clear_model(self, model_id: str) -> None:
        """Forget every choice of a removed model."""
        with self._lock:
            self._choices = {
                key: value for key, value in self._choices.items() if key[0] != model_id
            }

    def allows(self, model_id: str, adapter: BaseAdapter) -> bool:
        """Whether batch work may use this route of ``model_id`` (its canonical id)."""
        choice = self.choice(model_id, route_id_for_adapter(adapter))
        return default_batch_allowed(adapter) if choice is None else choice


def route_batch_allowed(resolver: BatchRouteResolver | None, model_id: str, adapter: Any) -> bool:
    """Whether batch work may use a route, by the resolver or else by its locality."""
    if resolver is None:
        return default_batch_allowed(adapter)
    return resolver.allows(model_id, adapter)


def batch_endpoint_scope(router: Any, model_id: str) -> frozenset[str] | None:
    """The endpoint ids of ``model_id``'s routes batch work may use.

    Every configured route counts, whatever its weight, circuit or admin state
    at the moment: those change by the minute and leave an item waiting, as they
    leave an interactive request failing over, while a route batch work may not
    use never serves it. ``None`` when ``router`` has no route table entry for
    the model (an embedding model, or a router without a route table), so the
    caller applies no range.
    """
    routes = getattr(router, "routes", None)
    if not isinstance(routes, dict):
        return None
    route = routes.get(model_id)
    if route is None:
        return None
    canonical = getattr(route, "canonical_model_id", None) or model_id
    resolver = getattr(router, "batch_route_resolver", None)
    return frozenset(
        endpoint_id_for_adapter(adapter)
        for adapter, _weight in route.adapters
        if route_batch_allowed(resolver, canonical, adapter)
    )


def any_batch_route(resolver: BatchRouteResolver | None, model_id: str, adapters: Any) -> bool:
    """Whether any of ``model_id``'s route adapters may serve batch work."""
    return any(route_batch_allowed(resolver, model_id, adapter) for adapter in adapters)


def embedding_backends(adapter: Any) -> list[Any]:
    """The backends of an embedding model's adapter: a fallback chain's, or itself."""
    backends = getattr(adapter, "backends", None)
    return list(backends) if backends is not None else [adapter]


def batch_embedding_adapter(adapter: Any, model_id: str, router: Any) -> Any | None:
    """The adapter a batch embedding request may use, or None when no backend may serve it.

    An embedding model is not in the route table, so it has no stored choice to
    make (``batch_endpoint_scope`` answers None for it); its backends follow
    their locality. A fallback chain keeps the batch-allowed backends, in order.
    """
    from serving.servers.embedding_fallback import FallbackEmbeddingAdapter

    resolver = getattr(router, "batch_route_resolver", None)
    backends = embedding_backends(adapter)
    allowed = [backend for backend in backends if route_batch_allowed(resolver, model_id, backend)]
    if len(allowed) == len(backends):
        return adapter
    if not allowed:
        return None
    return allowed[0] if len(allowed) == 1 else FallbackEmbeddingAdapter(allowed)


def batch_route_available(services: Any, model_id: str, *, embeddings: bool = False) -> bool:
    """Whether a batch item for ``model_id`` (its canonical id) has a route it may use.

    True for a model this gateway does not know at all: the dispatch fails such
    an item as unavailable, which says more than that it has no batch route.
    """
    router = getattr(services, "router", None)
    if embeddings:
        adapter = (getattr(services, "embedding_adapters", None) or {}).get(model_id)
        if adapter is None:
            return True
        resolver = getattr(router, "batch_route_resolver", None)
        return any_batch_route(resolver, model_id, embedding_backends(adapter))
    scope = batch_endpoint_scope(router, model_id)
    return scope is None or bool(scope)


def batch_route_refusal(model_id: str) -> str:
    """The reason a batch request for ``model_id`` is refused or fails, worded once."""
    return (
        f"Model '{model_id}' has no route that batch requests may use. Batch requests run "
        "only on the routes an administrator allows for batch work (by default, the "
        "gateway's own servers)."
    )
