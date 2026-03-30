"""Health check endpoints."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from serving.config.settings import has_role
from serving.observability.metrics import DATABASE_CONNECTED
from serving.servers.auth import optional_verify_api_key
from serving.servers.deps import get_log_store, get_operational_store, get_router, get_services

router = APIRouter()


async def _test_store_health(op_store: Any, log_store: Any) -> bool:
    """Actively test database connection via store health checks.

    Args:
        op_store: OperationalStore instance
        log_store: LogStore instance

    Returns:
        True if at least one store is reachable and healthy, False otherwise
    """
    healthy = False
    try:
        if op_store and await op_store.health_check():
            healthy = True
        if log_store and await log_store.health_check():
            healthy = True
    except Exception:
        pass

    DATABASE_CONNECTED.set(1 if healthy else 0)
    return healthy


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
    response: Response,
    router_exec=Depends(get_router),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Health check endpoint with active database connection test.

    Performs a lightweight query to detect runtime database failures.
    Returns 200 OK if service is healthy, 503 Service Unavailable if database is disconnected.
    """
    routes_count = len(router_exec.routes)

    db_connected = await _test_store_health(op_store, log_store)

    if not db_connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unhealthy",
            "reason": "database_disconnected",
            "routes_configured": routes_count,
            "database_connected": False,
        }

    return {
        "status": "healthy",
        "routes_configured": routes_count,
        "database_connected": True,
    }


@router.get("/health/deep")
async def deep_health(
    response: Response,
    router_exec=Depends(get_router),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Deep health check with provider/circuit and rate limiter info.

    Performs active database connection test and returns detailed system status.
    """
    routes_count = len(router_exec.routes)

    db_connected = await _test_store_health(op_store, log_store)

    provider_status = (
        router_exec.get_provider_status() if hasattr(router_exec, "get_provider_status") else {}
    )
    rl = services.rate_limiter
    rl_status: dict[str, Any] | None = None
    if rl is not None:
        try:
            # When model_id omitted, returns per-model dict
            rl_status = rl.get_metrics(None)
        except Exception:
            rl_status = None

    overall = "healthy"
    if not db_connected:
        overall = "unhealthy"
    else:
        for _p, s in provider_status.items():
            if s.get("circuit_state") == "open" or (
                s.get("availability") is not None and s.get("availability") < 0.9
            ):
                overall = "degraded"
                break

    if overall == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": overall,
        "routes_configured": routes_count,
        "database_connected": db_connected,
        "providers": provider_status,
        "rate_limiter": rl_status,
    }


@router.get("/health/model-activity")
async def model_activity(
    window: int = Query(default=10, ge=1, le=60),
    user_ctx: dict[str, Any] | None = Depends(optional_verify_api_key),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Per-model, per-provider traffic activity over a recent window.

    Internal+ endpoint used by the prober to decide whether to skip
    synthetic probes when real user traffic provides sufficient signal.
    Requires USER_AUTH_ENABLED=1 — always returns 403 in auth-disabled
    deployments to prevent unintentional exposure of traffic stats.
    """
    if os.getenv("USER_AUTH_ENABLED", "0") != "1":
        raise HTTPException(status_code=403, detail="Requires USER_AUTH_ENABLED=1")

    user_role = (user_ctx or {}).get("role", "free")
    if not user_ctx or not has_role(user_role, "internal"):
        raise HTTPException(status_code=403, detail="Internal access required")
    if not log_store:
        raise HTTPException(status_code=503, detail="Database not available")
    routes = await log_store.get_model_activity(window_minutes=window)
    return {"window_minutes": window, "routes": routes}


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
