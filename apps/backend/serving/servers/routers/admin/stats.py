"""Admin stats + routing-info endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from serving.servers.deps import (
    get_log_store,
    get_router,
    get_services,
    verify_admin_access,
)

router = APIRouter(prefix="/admin")


@router.get("/stats")
async def get_stats(
    model: str | None = None,
    provider: str | None = None,
    hours: int = 24,
    log_store=Depends(get_log_store),
    _admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Return usage statistics from the log store.

    Note: a bare `/stats` alias previously existed without admin auth; it has
    been removed. Use `/admin/stats` (admin-authenticated) instead.
    """
    if not log_store:
        return {"error": "Database logging not configured"}

    stats = await log_store.get_stats(model_id=model, provider=provider, hours=hours)
    return {
        "period_hours": hours,
        "filters": {"model": model, "provider": provider},
        "stats": stats,
    }


@router.get("/routing")
async def admin_get_routing(
    router_exec=Depends(get_router),
    services=Depends(get_services),
    _admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Admin alias for routing information."""
    routing_info = {}
    for model_id, route in router_exec.routes.items():
        if not getattr(route, "published", True):
            continue
        routing_info[model_id] = [
            {
                "provider": adapter.config.provider,
                "base_url": adapter.config.base_url,
                "weight": f"{weight * 100:.0f}%",
            }
            for adapter, weight in route.adapters
        ]

    response: dict[str, Any] = {
        "routes": routing_info,
        "description": "Weight distribution for each model.",
    }
    if services.routing_manager:
        response["manager_status"] = services.routing_manager.get_status()
    return response
