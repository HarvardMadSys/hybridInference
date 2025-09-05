from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import bootstrap
from .deps import AppServices
from .middleware.error import install_error_handlers
from .routers import health, models, completions, compat, admin


@asynccontextmanager
async def lifespan(app: FastAPI):
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
        description="Unified API server supporting VLLM, DeepSeek, Gemini, and Llama models",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS: be permissive by default to match legacy behavior
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Error handlers
    install_error_handlers(app)

    # Routers
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(completions.router)
    app.include_router(compat.router)
    app.include_router(admin.router)

    return app


app = create_app()
