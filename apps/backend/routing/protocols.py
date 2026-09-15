"""Structural interfaces implemented by serving routers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.dispatch import EndpointBinding
    from routing.routers import RoutingObservation

__all__ = ["RouteTableRefreshable", "RouterProtocol", "RoutingRequestOptions"]


@dataclass(frozen=True, slots=True)
class RoutingRequestOptions:
    """Router-owned request controls that must not reach provider adapters.

    Attributes:
        pin_provider: The caller's explicit hard pin. It collapses the route to
            that provider, disables fallback, and raises when absent. Unchanged.
        preferred_endpoint_id: The scheduling policy's preferred endpoint. A
            *preference*, not a pin: a router that can dispatch to it must honor
            it instead of re-sampling, a router that cannot falls back to its own
            selection, and a failed attempt still enters the caller's ordinary
            fallback loop. Kept separate from ``pin_provider`` precisely because
            the two have opposite failure semantics.
        endpoint_scope: Optional candidate range for this dispatch. A backend
            that owns only one execution domain sets it to that domain's
            endpoints, so its *fallback* candidates cannot escape the domain
            either -- preferring an endpoint is not enough when the preferred
            one fails and the router walks the rest of the route. ``None``
            keeps the router's full range.
        allow_fallback: Whether this dispatch may walk the rest of the route when
            the attempt it committed to fails. ``False`` makes it a
            single-candidate attempt: the failure is surfaced instead. A
            scheduling layer that plans the candidate order itself -- the hybrid
            layer does -- sets it so the order it planned is the order that
            happens, and so no candidate is attempted twice.
        bound_endpoint: The caller's resolved binding for this dispatch. A caller
            that already chose the endpoint -- the hybrid layer, when it planned
            one candidate per attempt -- hands over the binding it validated, so
            the dispatch runs the adapter it was admitted for even if the route
            table has since replaced that adapter. Selection, admission, prefill
            and accounting still run against the route: only the executed object
            comes from here. ``None`` keeps the router's own resolution.
        require_target: Whether ``preferred_endpoint_id`` is the only endpoint
            this dispatch may use. The default treats a target as a preference:
            an endpoint that is not admissible is replaced by the router's own
            selection. ``True`` forbids that substitution and raises
            :class:`~routing.routers.TargetUnavailableError` instead, so a caller
            that planned one candidate per attempt learns the candidate is
            unavailable rather than silently getting a different one.
        required_modalities: Non-text input modalities the request needs.
    """

    pin_provider: str | None = None
    preferred_endpoint_id: str | None = None
    endpoint_scope: frozenset[str] | None = None
    allow_fallback: bool = True
    bound_endpoint: EndpointBinding | None = None
    require_target: bool = False
    required_modalities: frozenset[str] = frozenset()


@runtime_checkable
class RouteTableRefreshable(Protocol):
    """Capability for routers that can reload their bound route-table view."""

    def refresh_route_table(self) -> None:
        """Refresh route-derived state under the router's synchronization boundary."""
        ...


@runtime_checkable
class RouterProtocol(Protocol):
    """Common request and observation interface for serving routers."""

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Route a non-streaming chat completion request."""
        ...

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Route a streaming chat completion request."""
        ...

    def record_observation(self, obs: RoutingObservation) -> None:
        """Record a completed request for routers with online state."""
        ...

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return endpoint health and circuit state."""
        ...
