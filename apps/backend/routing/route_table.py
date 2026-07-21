"""Read-only route-table values and structural interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from serving.adapters.base import BaseAdapter

__all__ = ["EffectiveRoute", "RouteTableSnapshot", "RouteTableView"]


@dataclass(frozen=True, slots=True)
class EffectiveRoute:
    """Structurally immutable effective route captured from a snapshot.

    Adapter objects remain shared references; only route membership, ordering,
    and captured weights are frozen.
    """

    route_key: str
    canonical_model_id: str
    adapters: tuple[tuple[BaseAdapter, float], ...]


class RouteTableSnapshot:
    """Detached route-table view for validating an unpublished transition."""

    def __init__(
        self,
        routes: tuple[EffectiveRoute, ...],
        canonical_ids: Mapping[str, str],
    ) -> None:
        self._routes = tuple(routes)
        self._canonical_ids = dict(canonical_ids)

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        """Return the immutable routes captured when this view was created."""
        return self._routes

    def canonical_id(self, model_id: str) -> str:
        """Resolve a route key from the detached canonical-id snapshot."""
        return self._canonical_ids.get(model_id, model_id)


@runtime_checkable
class RouteTableView(Protocol):
    """Synchronous read-only route metadata required by routing strategies."""

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        """Return a complete immutable snapshot without retaining a live lock."""
        ...

    def canonical_id(self, model_id: str) -> str:
        """Return the canonical identifier for a route key or model identifier."""
        ...
