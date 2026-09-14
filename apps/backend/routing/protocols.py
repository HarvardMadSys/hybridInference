"""Structural interfaces implemented by serving routers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
        required_modalities: Non-text input modalities the request needs.
    """

    pin_provider: str | None = None
    preferred_endpoint_id: str | None = None
    endpoint_scope: frozenset[str] | None = None
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
