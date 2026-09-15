"""Structural interfaces implemented by serving routers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from routing.routers import RoutingObservation

__all__ = ["RouteTableRefreshable", "RouterProtocol", "RoutingRequestOptions"]


@dataclass(frozen=True, slots=True)
class RoutingRequestOptions:
    """Router-owned request controls that must not reach provider adapters.

    Traffic fields are server-generated online evidence. They are carried as a
    typed routing hint so strategies can make an explicit, conservative
    scheduling decision without reading arbitrary request-context keys. The
    admission callback is consumed by typed routers and installed in the
    dispatch context; provider adapters fire it after their outbound admission
    gate succeeds. It is never forwarded through provider adapter kwargs.
    """

    pin_provider: str | None = None
    required_modalities: frozenset[str] = frozenset()
    traffic_classification: str | None = None
    traffic_automation_score: float | None = None
    traffic_confidence: float | None = None
    traffic_reasons: tuple[str, ...] = ()
    on_dispatch_admitted: Callable[[], None] | None = None


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
