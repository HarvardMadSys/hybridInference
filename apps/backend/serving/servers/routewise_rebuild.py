"""Synchronize cached RouteWise routers with effective route-table state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from routing.model_router_registry import ModelRouterRegistry


def rebuild_cached_routewise_routers(
    model_router_registry: ModelRouterRegistry | None,
) -> None:
    """Refresh each cached RouteWise router after route-table state changes."""
    if model_router_registry is None:
        return
    model_router_registry.refresh_route_tables()


def rebuild_routewise_routers(services: Any) -> None:
    """Refresh cached RouteWise routers from an application service container."""
    rebuild_cached_routewise_routers(getattr(services, "model_router_registry", None))
