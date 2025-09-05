from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from serving.servers.deps import get_db_logger, get_rate_limiter, get_router, get_services


router = APIRouter()


@router.get("/stats")
@router.get("/admin/stats")
async def get_stats(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    hours: int = 24,
    db_logger = Depends(get_db_logger),
) -> Dict[str, Any]:
    """Return usage statistics from the database logger."""
    if not db_logger:
        return {"error": "Database logging not configured"}

    stats = await db_logger.get_stats(model_id=model, provider=provider, hours=hours)
    return {
        "period_hours": hours,
        "filters": {"model": model, "provider": provider},
        "stats": stats,
    }


@router.get("/rate-limits/{model_id}")
@router.get("/admin/rate-limits/{model_id}")
async def get_rate_limit_status(model_id: str, rate_limiter = Depends(get_rate_limiter)):
    """Get rate limit status and metrics for a specific model."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    status = rate_limiter.get_status(model_id)
    if not status.get("configured"):
        raise HTTPException(404, f"No rate limit configured for model '{model_id}'")
    return status


@router.get("/rate-limits")
@router.get("/admin/rate-limits")
async def get_all_rate_limits(rate_limiter = Depends(get_rate_limiter)):
    """Get rate limit metrics for all models."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    return rate_limiter.get_metrics()


@router.post("/rate-limits/{model_id}/reset")
@router.post("/admin/rate-limits/{model_id}/reset")
async def reset_circuit_breaker(model_id: str, rate_limiter = Depends(get_rate_limiter)):
    """Reset circuit breaker for a model (admin endpoint)."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    rate_limiter.reset_circuit_breaker(model_id)
    return {"message": f"Circuit breaker reset for {model_id}"}


@router.get("/admin/routing")
async def admin_get_routing(router_exec = Depends(get_router), services = Depends(get_services)) -> Dict[str, Any]:
    """Admin alias for routing information."""
    routing_info = {}
    for model_id, route in router_exec.routes.items():
        routing_info[model_id] = [
            {
                "provider": adapter.config.provider,
                "base_url": adapter.config.base_url,
                "weight": f"{weight * 100:.0f}%",
            }
            for adapter, weight in route.adapters
        ]

    response: Dict[str, Any] = {
        "routes": routing_info,
        "description": "Weight distribution for each model.",
    }
    if services.routing_manager:
        response["manager_status"] = services.routing_manager.get_status()
    return response

