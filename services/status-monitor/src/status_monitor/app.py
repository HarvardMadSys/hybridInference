"""FastAPI application wiring.

Builds the app, starts the background probe scheduler on startup, and exposes
the dashboard plus JSON status/health endpoints. Routes are served both at the
root and (optionally) under ``settings.base_path`` so the service works whether
or not it sits behind a path-prefixing reverse proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from status_monitor.dashboard import render_dashboard
from status_monitor.scheduler import run_scheduler
from status_monitor.state import StatusStore

if TYPE_CHECKING:
    from status_monitor.config import AppConfig

logger = logging.getLogger(__name__)


def _build_router(config: AppConfig, store: StatusStore) -> APIRouter:
    """Builds the router with dashboard and API routes."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        """Serves the HTML status dashboard."""
        return HTMLResponse(render_dashboard(store.snapshot()))

    @router.get("/api/status")
    async def status() -> JSONResponse:
        """Returns the full status snapshot as JSON."""
        return JSONResponse(store.snapshot())

    @router.get("/api/health")
    async def health() -> JSONResponse:
        """Returns a lightweight liveness/readiness summary."""
        snap = store.snapshot()
        return JSONResponse(
            {
                "status": "ok",
                "total": snap["total"],
                "healthy": snap["healthy"],
                "unhealthy": snap["unhealthy"],
            }
        )

    return router


def create_app(config: AppConfig) -> FastAPI:
    """Creates the FastAPI app for the given configuration.

    Args:
        config: The application configuration.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    store = StatusStore(
        history_size=config.settings.history_size,
        state_path=config.settings.state_path,
    )
    store.load()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202 - FastAPI lifespan signature
        """Starts and stops the background probe scheduler."""
        task = asyncio.create_task(run_scheduler(config, store))

        def _on_done(finished: asyncio.Task) -> None:
            """Surfaces an unexpected scheduler exit instead of failing silently."""
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                logger.error("Probe scheduler exited unexpectedly", exc_info=exc)

        task.add_done_callback(_on_done)
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="FreeInference Status Monitor", lifespan=lifespan)
    app.state.store = store

    router = _build_router(config, store)
    app.include_router(router)
    base_path = config.settings.base_path
    if base_path:
        app.include_router(router, prefix=base_path)

    return app
