"""FastAPI application factory and configuration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config.settings import settings
from ..utils.logging import attach_quiet_access_filter
from . import bootstrap
from .middleware.error import install_error_handlers
from .middleware.exception_handler import install_exception_handlers
from .middleware.metrics import MetricsMiddleware
from .middleware.request_id import RequestIdMiddleware
from .middleware.request_log import RequestLogMiddleware
from .routers import (
    admin,
    anthropic_proxy,
    auth_routes,
    compat,
    completions,
    embeddings,
    health,
    internal,
    metrics,
    models,
    playground,
    qdrant_proxy,
    user_routes,
)

if TYPE_CHECKING:
    from .deps import AppServices


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - initialize and cleanup resources."""
    # Attach after uvicorn's own logging setup, which would otherwise wipe filters
    # added at import time. LOG_LEVEL=DEBUG disables suppression.
    attach_quiet_access_filter()

    services: AppServices = await bootstrap.initialize()
    app.state.services = services  # type: ignore[attr-defined]
    try:
        yield
    finally:
        await bootstrap.shutdown(services)


def create_app() -> FastAPI:
    """Create and configure a FastAPI app instance with modular routers."""
    app = FastAPI(
        title="OpenRouter-Compatible API Server",
        description="Unified API server supporting VLLM, DeepSeek, Gemini models",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS: use specific origins when credentials are enabled
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request ID and metrics middlewares
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestLogMiddleware)

    # Error handlers
    install_error_handlers(app)

    # Domain exception handlers (for business logic errors)
    install_exception_handlers(app)

    # Routers
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(models.router)
    app.include_router(completions.router)
    app.include_router(embeddings.router)
    app.include_router(anthropic_proxy.router)
    app.include_router(qdrant_proxy.router)
    app.include_router(compat.router)
    app.include_router(admin.router)
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    app.include_router(internal.router)
    app.include_router(playground.router)

    return app


app = create_app()
