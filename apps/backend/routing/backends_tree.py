"""The tree backend: a routing subtree entered through a pool identity.

A tree backend is the entry to a pool. It carries the pool identity and its
candidate scope, and hands selection to the router it wraps, which keeps its own
retries, hedging and admission. What it does not do is reimplement any of that.
An exact instruction is still honored inside the declared scope, but a binding
for an endpoint outside the scope is refused before any upstream I/O, because the
alternative is a subtree quietly serving a request it was not granted.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from routing.backends_contract import RoutingBackendBase, _adapters_in_router
from routing.dispatch import (
    BackendDispatch,
    DelegatePool,
    DispatchMismatchError,
    EndpointBinding,
    binding_for_adapter,
)
from routing.endpoints import endpoint_id_for_adapter
from routing.protocols import RoutingRequestOptions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.decisions import RoutingTarget
    from routing.protocols import RouterProtocol

__all__ = ["TreeBackend"]


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
        self._require_model(model_id)
        if isinstance(instruction, DelegatePool):
            if instruction.pool_id != self._pool_id:
                raise DispatchMismatchError(
                    f"{type(self).__name__} {self.name!r} is pool {self._pool_id!r} and was "
                    f"asked to serve pool {instruction.pool_id!r}"
                )
            return
        if self.canonical_id(instruction.binding.model_id) != self.canonical_id(model_id):
            raise DispatchMismatchError("Execution binding belongs to a different model")
        scope = self.dispatch_scope(model_id)
        if scope is None:
            return
        # Both sides normalized to canonical endpoints first: the declared range
        # may be expressed as provider labels, and the binding names an endpoint.
        allowed = self.scope_endpoints(scope, model_id)
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
        self._require_model(model_id)
        scope = self.dispatch_scope(model_id)
        if scope is not None and endpoint_id not in self.scope_endpoints(scope, model_id):
            return None
        for adapter in _adapters_in_router(self._router, model_id):
            if endpoint_id_for_adapter(adapter) == endpoint_id:
                return binding_for_adapter(adapter, model_id=model_id, pool_id=self._pool_id)
        return None

    def _require_model(self, model_id: str) -> None:
        """Refuse models outside this pool before checking endpoint grants."""
        if not self.serves(model_id):
            raise DispatchMismatchError(f"Pool {self.pool_id!r} does not serve model {model_id!r}")

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
        self._require_model(model_id)
        if (
            routing_options is not None
            and routing_options.bound_endpoint is not None
            and self.canonical_id(routing_options.bound_endpoint.model_id)
            != self.canonical_id(model_id)
        ):
            raise DispatchMismatchError("Execution binding belongs to a different model")
        declared = self.dispatch_scope(model_id)
        if declared is None:
            return routing_options
        allowed = self.scope_endpoints(declared, model_id)
        imposed = routing_options.endpoint_scope if routing_options is not None else None
        effective = (
            allowed if imposed is None else (self.scope_endpoints(imposed, model_id) & allowed)
        )
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
