"""Unit tests for RequestLogMiddleware log-level routing."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.request_log import RequestLogMiddleware

_LOGGER_NAME = "serving.servers.middleware.request_log"


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Ensure the target logger and root logger are in a clean state.

    Other test modules (e.g., server integration tests) may trigger
    ``setup_logging()`` which installs a JsonFormatter and changes the
    root logger level.  This fixture resets both so caplog works
    correctly regardless of test ordering.
    """
    target = logging.getLogger(_LOGGER_NAME)
    root = logging.getLogger()

    saved_root_level = root.level
    saved_root_handlers = root.handlers[:]
    saved_target_level = target.level
    saved_target_propagate = target.propagate

    # Reset root to default state so caplog captures behave predictably
    root.setLevel(logging.WARNING)
    for h in root.handlers:
        h.setFormatter(logging.Formatter())

    yield

    # Restore
    root.setLevel(saved_root_level)
    root.handlers = saved_root_handlers
    target.setLevel(saved_target_level)
    target.propagate = saved_target_propagate


@pytest.fixture
def app_with_middleware():
    """Minimal FastAPI app with only RequestLogMiddleware installed."""
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/health/deep")
    def health_deep():
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics():
        return {}

    @app.get("/v1/chat/completions")
    def completions():
        return {}

    return app


async def _get(app, path: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(path)


class TestRequestLogMiddleware:
    """Verify verbose paths log at DEBUG and normal paths log at INFO."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/health", "/health/deep", "/metrics"])
    async def test_verbose_paths_log_at_debug(self, app_with_middleware, caplog, path):
        with caplog.at_level(logging.DEBUG, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, path)

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records, f"Expected an http_request log record for {path}"
        assert all(r.levelno == logging.DEBUG for r in records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/health", "/health/deep", "/metrics"])
    async def test_verbose_paths_silent_at_info(self, app_with_middleware, caplog, path):
        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, path)

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert not records, f"Expected no http_request log at INFO level for {path}"

    @pytest.mark.asyncio
    async def test_normal_path_logs_at_info(self, app_with_middleware, caplog):
        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, "/v1/chat/completions")

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records, "Expected an http_request log record for /v1/chat/completions"
        assert all(r.levelno == logging.INFO for r in records)
