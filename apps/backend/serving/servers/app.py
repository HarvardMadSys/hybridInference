"""FastAPI application factory and configuration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config.settings import settings
from ..utils.logging import attach_quiet_access_filter
from . import bootstrap
from .middleware.error import FallbackErrorMiddleware, install_error_handlers
from .middleware.exception_handler import install_exception_handlers
from .middleware.request_id import RequestIdMiddleware
from .middleware.request_log import RequestLogMiddleware
from .middleware.timeout import TimeoutMiddleware
from .routers import (
    admin,
    agent_grants,
    agent_jobs,
    agent_mcp,
    anthropic_messages,
    auth_routes,
    compat,
    completions,
    embeddings,
    health,
    identity,
    internal,
    models,
    playground,
    qdrant_proxy,
    rag,
    responses,
    site_config,
    site_updates,
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

    import logging as _logging

    _sec_logger = _logging.getLogger(__name__)
    if not settings.jwt_secret_key:
        _sec_logger.critical("jwt_secret_key is empty — tokens will be insecure")
    if not settings.api_key_secret:
        _sec_logger.critical("api_key_secret is empty — API key generation will be insecure")
    if not settings.admin_token:
        _sec_logger.warning("admin_token is empty — admin endpoints will be inaccessible")

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

    # Request ID, timeout, and request log middlewares
    app.add_middleware(TimeoutMiddleware)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(FallbackErrorMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # Error handlers
    install_error_handlers(app)

    # Domain exception handlers (for business logic errors)
    install_exception_handlers(app)

    # Routers
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(completions.router)
    app.include_router(embeddings.router)
    app.include_router(rag.router)
    app.include_router(responses.router)
    app.include_router(anthropic_messages.router)
    app.include_router(agent_jobs.router)
    app.include_router(agent_mcp.router)
    app.include_router(qdrant_proxy.router)
    app.include_router(site_config.router)
    app.include_router(site_updates.router)
    app.include_router(compat.router)
    app.include_router(admin.router)
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    app.include_router(internal.router)
    app.include_router(playground.router)
    app.include_router(identity.router)
    app.include_router(agent_grants.router)

    # Override HTTPException handler to emit Anthropic-format errors on
    # /v1/messages and /anthropic/... paths (must register after install_error_handlers).
    # Register on the Starlette base class, not just fastapi.HTTPException: router
    # 404/405 (unimplemented surface an Anthropic client probes -- GET /v1/messages,
    # /v1/messages/batches, ...) are raised as starlette.exceptions.HTTPException,
    # and Starlette's handler lookup walks the raised type's MRO, so a handler keyed
    # only on the fastapi subclass would never match those. The subclass registration
    # is kept for explicitness (fastapi.HTTPException is a subclass, so the base
    # registration already covers it).
    from fastapi import HTTPException as _HTTPException
    from starlette.exceptions import HTTPException as _StarletteHTTPException

    app.add_exception_handler(
        _StarletteHTTPException,
        anthropic_messages.anthropic_aware_http_exception_handler,
    )
    app.add_exception_handler(
        _HTTPException,
        anthropic_messages.anthropic_aware_http_exception_handler,
    )

    return app


app = create_app()
