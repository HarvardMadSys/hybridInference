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
    """Router-owned request controls that must not reach provider adapters."""

    pin_provider: str | None = None


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
