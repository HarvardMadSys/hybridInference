"""Health, root, and routing info endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from serving.config.settings import has_role
from serving.observability.metrics import DATABASE_CONNECTED
from serving.servers.auth import is_user_auth_enabled, optional_verify_api_key
from serving.servers.deps import get_db_logger, get_router, get_services

if TYPE_CHECKING:
    from serving.storage.database import DatabaseLogger

router = APIRouter()


async def _test_database_connection(db_logger: DatabaseLogger | None) -> bool:
    """Actively test database connection with a lightweight query.

    This performs a real database query to detect runtime connection failures,
    not just checking if the pool object exists.

    Args:
        db_logger: DatabaseLogger instance from get_db_logger dependency

    Returns:
        True if database responds successfully, False otherwise
    """
    if not db_logger or not db_logger.pool:
        return False

    try:
        import asyncpg

        # Execute lightweight query with timeout to test connection
        async with db_logger.pool.acquire() as conn:
            await conn.fetchval("SELECT 1", timeout=2.0)

        # Database is healthy - mark metric as connected
        DATABASE_CONNECTED.set(1)
        return True

    except asyncpg.PostgresError:
        # Database-specific error - mark as disconnected
        DATABASE_CONNECTED.set(0)
        return False
    except Exception:
        # Other errors (timeout, etc.) - also consider as unhealthy
        DATABASE_CONNECTED.set(0)
        return False


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
        },
    }


@router.get("/health")
async def health(
    response: Response,
    router_exec=Depends(get_router),
    db_logger=Depends(get_db_logger),
) -> dict[str, Any]:
    """Health check endpoint with active database connection test.

    Performs a lightweight SELECT 1 query to detect runtime database failures.
    Returns 200 OK if service is healthy, 503 Service Unavailable if database is disconnected.
    """
    routes_count = len(router_exec.routes)

    # Actively test database connection (metric is updated inside _test_database_connection)
    db_connected = await _test_database_connection(db_logger)

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
    db_logger=Depends(get_db_logger),
) -> dict[str, Any]:
    """Deep health check with provider/circuit info.

    Performs active database connection test and returns detailed system status.
    """
    routes_count = len(router_exec.routes)

    # Actively test database connection (metric is updated inside _test_database_connection)
    db_connected = await _test_database_connection(db_logger)

    provider_status = (
        router_exec.get_provider_status() if hasattr(router_exec, "get_provider_status") else {}
    )

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
    }


@router.get("/health/model-activity")
async def model_activity(
    window: int = Query(default=10, ge=1, le=60),
    user_ctx: dict[str, Any] | None = Depends(optional_verify_api_key),
    db_logger: DatabaseLogger | None = Depends(get_db_logger),
) -> dict[str, Any]:
    """Per-model, per-provider traffic activity over a recent window.

    Internal+ endpoint used by the prober to decide whether to skip
    synthetic probes when real user traffic provides sufficient signal.
    Requires USER_AUTH_ENABLED=1 — always returns 403 in auth-disabled
    deployments to prevent unintentional exposure of traffic stats.
    """
    if not is_user_auth_enabled():
        raise HTTPException(status_code=403, detail="Requires USER_AUTH_ENABLED=1")

    user_role = (user_ctx or {}).get("role", "free")
    if not user_ctx or not has_role(user_role, "internal"):
        raise HTTPException(status_code=403, detail="Internal access required")
    if not db_logger:
        raise HTTPException(status_code=503, detail="Database not available")
    routes = await db_logger.get_model_activity(window_minutes=window)
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
