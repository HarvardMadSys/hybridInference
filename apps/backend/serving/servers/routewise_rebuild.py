"""Synchronize cached RouteWise routers with effective route-table state."""

from __future__ import annotations

from typing import Any


def rebuild_cached_routewise_routers(model_router_registry: Any | None) -> None:
    """Refresh each cached RouteWise router after route-table state changes."""
    if model_router_registry is None:
        return
    seen: set[int] = set()
    for router_obj in model_router_registry.cached_routers():
        if id(router_obj) in seen:
            continue
        seen.add(id(router_obj))
        rebuild = getattr(router_obj, "_rebuild_from_route_table", None)
        if not callable(rebuild):
            # One-release fallback for external strategies using the old hook.
            rebuild = getattr(router_obj, "_rebuild_from_fixed_router", None)
        if not callable(rebuild):
            continue
        commit_lock = getattr(router_obj, "_route_commit_lock", None)
        if commit_lock is not None:
            with commit_lock:
                rebuild()
        else:
            rebuild()


def rebuild_routewise_routers(services: Any) -> None:
    """Refresh cached RouteWise routers from an application service container."""
    rebuild_cached_routewise_routers(getattr(services, "model_router_registry", None))
