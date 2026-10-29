"""The leaf backend: one adapter, bound to one endpoint, executed as it is.

This is the end of the dispatch recursion and nothing more: it calls the adapter
it was built with and hands back whatever that call produced. It keeps no queue,
no reservation, no health record, no failed-attempt sample, no routing metadata
and no connection lifecycle, because every one of those already belongs to the
router that chose this endpoint -- a second copy here would double-count them.

The call signatures are the adapter own, not the router. A leaf stands in for one
adapter wherever an adapter is executed, so the router that wrapped it keeps
doing the deciding, the admission, the accounting and the metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.dispatch import (
    BackendDispatch,
    DispatchMismatchError,
    EndpointBinding,
    ExecuteEndpoint,
)
from routing.endpoints import endpoint_id_for_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.decisions import RoutingTarget
    from routing.routers import RoutingObservation

__all__ = ["LeafBackend"]


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
        if endpoint_id_for_adapter(adapter) != endpoint_id:
            raise DispatchMismatchError("LeafBackend endpoint does not match its adapter")
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
