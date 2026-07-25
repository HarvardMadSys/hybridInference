"""Unit tests for RequestLogMiddleware log-level routing."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, Response
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

    @app.get("/user/me")
    def unauthorized():
        return Response(status_code=401)

    return app


async def _get(app, path: str, headers: dict[str, str] | None = None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(path, headers=headers)


class TestRequestLogMiddleware:
    """Verify verbose paths log at DEBUG and normal paths log at INFO."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/health", "/health/deep", "/metrics"])
    async def test_verbose_paths_log_at_debug(self, app_with_middleware, caplog, path):
        """Quiet health and metrics paths are still visible at DEBUG."""
        with caplog.at_level(logging.DEBUG, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, path)

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records, f"Expected an http_request log record for {path}"
        assert all(r.levelno == logging.DEBUG for r in records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/health", "/health/deep", "/metrics"])
    async def test_verbose_paths_silent_at_info(self, app_with_middleware, caplog, path):
        """Quiet health and metrics paths do not emit INFO request logs."""
        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, path)

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert not records, f"Expected no http_request log at INFO level for {path}"

    @pytest.mark.asyncio
    async def test_normal_path_logs_at_info(self, app_with_middleware, caplog):
        """Normal request paths emit INFO request logs."""
        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, "/v1/chat/completions")

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records, "Expected an http_request log record for /v1/chat/completions"
        assert all(r.levelno == logging.INFO for r in records)

    @pytest.mark.asyncio
    async def test_request_log_includes_client_and_peer_ip(
        self, app_with_middleware, caplog, monkeypatch
    ):
        """Request logs carry both trusted client IP and socket peer IP."""
        monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")

        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(
                app_with_middleware,
                "/v1/chat/completions",
                headers={
                    "x-forwarded-for": "203.0.113.8",
                    "x-real-ip": "203.0.113.8",
                    "user-agent": "pytest-client",
                    "origin": "https://freeinference.org",
                },
            )

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records
        record = records[-1]
        assert record.remote_ip == "203.0.113.8"
        assert record.peer_ip
        assert record.ip_source == "x-forwarded-for"
        assert record.x_forwarded_for == "203.0.113.8"
        assert record.x_real_ip == "203.0.113.8"
        assert record.user_agent == "pytest-client"
        assert record.origin == "https://freeinference.org"

    @pytest.mark.asyncio
    async def test_request_log_json_output_carries_cf_connecting_ip(
        self, app_with_middleware, caplog, monkeypatch
    ):
        """The Cloudflare header survives JSON serialization, not just the record.

        Both formatters emit only keys in ``_STRUCTURED_LOG_KEYS``, so a field
        set on the record is still dropped from production logs unless it is
        whitelisted there.
        """
        import json

        from serving.utils.logging import JsonFormatter

        monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
        monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")

        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(
                app_with_middleware,
                "/v1/chat/completions",
                headers={
                    "cf-connecting-ip": "2001:db8:abcd:1234::5",
                    "x-forwarded-for": "1.2.3.4, 2001:db8:abcd:1234::5",
                },
            )

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records
        record = records[-1]
        assert record.remote_ip == "2001:db8:abcd:1234::5"
        assert record.ip_source == "cf-connecting-ip"

        payload = json.loads(JsonFormatter().format(record))
        assert payload["cf_connecting_ip"] == "2001:db8:abcd:1234::5"
        assert payload["remote_ip"] == "2001:db8:abcd:1234::5"
        assert payload["ip_source"] == "cf-connecting-ip"
        # The spoofable header is retained so an attempt stays visible.
        assert payload["x_forwarded_for"] == "1.2.3.4, 2001:db8:abcd:1234::5"

    @pytest.mark.asyncio
    async def test_unauthorized_path_is_silent_at_info(self, app_with_middleware, caplog):
        """401 auth challenges do not emit INFO request logs."""
        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, "/user/me")

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert not records, "Expected no INFO http_request log for 401 auth challenge"

    @pytest.mark.asyncio
    async def test_unauthorized_path_logs_at_debug(self, app_with_middleware, caplog):
        """401 auth challenges remain available at DEBUG."""
        with caplog.at_level(logging.DEBUG, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, "/user/me")

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records, "Expected a DEBUG http_request log for 401 auth challenge"
        assert all(r.levelno == logging.DEBUG for r in records)
