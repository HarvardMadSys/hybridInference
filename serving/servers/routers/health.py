from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from serving.servers.deps import get_router, get_services

router = APIRouter()


@router.get("/")
async def root() -> dict[str, Any]:
    """Root endpoint showing API information and static metadata."""
    return {
        "message": "OpenRouter-Compatible API Server",
        "version": "2.0.0",
        "features": [
            "Load balancing",
            "Automatic fallback",
            "Database logging",
            "Advanced rate limiting",
        ],
        "endpoints": {
            "/v1/chat/completions": "Chat completions endpoint",
            "/completion": "Single-shot completion endpoint (alias)",
            "/models": "List available models (OpenRouter schema)",
            "/v1/models": "List available models",
            "/openrouter/models": "OpenRouter format models list",
            "/routing": "Show routing configuration",
            "/stats": "API usage statistics",
            "/health": "Health check",
            "/rate-limits": "Rate limit metrics for all models",
            "/rate-limits/{model_id}": "Rate limit status for specific model",
            "/rate-limits/{model_id}/reset": "Reset circuit breaker (POST)",
        },
    }


@router.get("/health")
async def health(
    router_exec=Depends(get_router),
    services=Depends(get_services),
) -> dict[str, Any]:
    """Health check endpoint."""
    routes_count = len(router_exec.routes)
    return {
        "status": "healthy",
        "routes_configured": routes_count,
        "database_connected": services.db_logger is not None,
    }


@router.get("/routing")
async def get_routing(
    router_exec=Depends(get_router),
    services=Depends(get_services),
) -> dict[str, Any]:
    """Show current routing configuration and manager status if present."""
    routing_info: dict[str, Any] = {}
    for model_id, route in router_exec.routes.items():
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
        "description": "Weight distribution for each model. Requests are randomly distributed based on weights.",
    }

    if services.routing_manager:
        response["manager_status"] = services.routing_manager.get_status()

    return response
