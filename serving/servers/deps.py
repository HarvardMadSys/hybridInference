from __future__ import annotations

"""FastAPI dependency helpers for application services.

This module exposes small dependency functions that retrieve shared services
from ``app.state``. Keeping these helpers thin makes route handlers easy to
test and avoids hidden global state.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Depends, Request

if TYPE_CHECKING:
    from database.database import DatabaseLogger
    from routing.executor import RouteExecutor
    from routing.manager import RoutingManager

    from .rate_limiter import PersistentRateLimiter


@dataclass
class AppServices:
    """Typed container for application-wide services.

    Using a dataclass improves discoverability and avoids fragile string keys
    when accessing ``app.state``.
    """

    router: RouteExecutor
    rate_limiter: PersistentRateLimiter | None = None
    db_logger: DatabaseLogger | None = None
    routing_manager: RoutingManager | None = None


def get_services(request: Request) -> AppServices:
    """Return the shared services object from the application state."""

    return request.app.state.services  # type: ignore[attr-defined]


def get_router(services: AppServices = Depends(get_services)) -> RouteExecutor:
    """Dependency to obtain the RouteExecutor."""

    return services.router


def get_rate_limiter(
    services: AppServices = Depends(get_services),
) -> PersistentRateLimiter | None:
    """Dependency to obtain the rate limiter (if configured)."""

    return services.rate_limiter


def get_db_logger(
    services: AppServices = Depends(get_services),
) -> DatabaseLogger | None:
    """Dependency to obtain the database logger (if configured)."""

    return services.db_logger
