"""Unit tests for RequestLogMiddleware log-level routing."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, Response
from httpx import ASGITransport, AsyncClient

from serving.observability.alert_rules import _is_failed_request
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.utils import context as req_ctx

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

    # ``async def`` on purpose: it mirrors the real completions handler and keeps
    # the contextvar in the request's own context. A sync endpoint runs in a
    # threadpool with a *copied* context, so nothing it writes to req_ctx would
    # reach this middleware.
    @app.get("/v1/chat/completions-upstream-401")
    async def upstream_unauthorized():
        # What the completions error path does once it has resolved the failing
        # upstream: publish the attribution durably into req_ctx (the
        # ``req_ctx.push`` scope around the adapter call is already unwound), so
        # the middleware can tell a relayed upstream 401 from one the gateway
        # issued itself.
        req_ctx.update({"provider": "diffusiongemma"})
        return Response(status_code=401)

    return app


@pytest.fixture
def app_with_id_and_log_middleware():
    """App with the real middleware pair, in the order ``app.py`` installs them.

    ``add_middleware`` prepends, so ``RequestIdMiddleware`` added last is the
    outermost and seeds the request context before ``RequestLogMiddleware`` runs.
    Needed to exercise anything about state carried *between* requests.
    """
    app = FastAPI()

    @app.get("/v1/chat/completions-upstream-500")
    async def upstream_error():
        req_ctx.publish_upstream_provider("diffusiongemma")
        return Response(status_code=500)

    @app.get("/user/me")
    async def gateway_unauthorized():
        return Response(status_code=401)

    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


async def _get(app, path: str, headers: dict[str, str] | None = None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(path, headers=headers)


def _window_item(record: logging.LogRecord) -> dict:
    """Project a request-log record exactly as ``FailedRequestRateRule`` does.

    Lets a middleware test assert the downstream alerting verdict from the real
    record instead of a hand-built dict, so the two stay in step.
    """
    return {
        "status": int(record.status_code),
        "provider": getattr(record, "provider", None),
        "path": getattr(record, "path", None),
        "client_error_kind": getattr(record, "client_error_kind", None),
    }


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
                    "origin": "https://gateway.example.com",
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
        assert record.origin == "https://gateway.example.com"

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

    @pytest.mark.asyncio
    async def test_upstream_attributed_401_logs_at_info(self, app_with_middleware, caplog):
        """A relayed upstream 401 is an outage, not token-refresh churn.

        Regression for the hour-long ``diffusiongemma`` outage: a local proxy
        rejected the gateway's own key on every request, and the propagated 401
        was filed in the auth-challenge bucket and logged at DEBUG — below the
        default INFO threshold, so the request log showed nothing at all.
        """
        with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, "/v1/chat/completions-upstream-401")

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records, "Expected an INFO http_request log for an upstream 401"
        record = records[-1]
        assert record.levelno == logging.INFO
        assert record.status_code == 401
        # The provider label is what makes the outage attributable in the log.
        assert record.provider == "diffusiongemma"

    @pytest.mark.asyncio
    async def test_gateway_401_without_provider_stays_demoted(self, app_with_middleware, caplog):
        """The demotion still applies to the gateway's own challenges only."""
        with caplog.at_level(logging.DEBUG, logger="serving.servers.middleware.request_log"):
            await _get(app_with_middleware, "/user/me")

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert records
        record = records[-1]
        assert record.levelno == logging.DEBUG
        assert record.status_code == 401
        assert record.provider is None

    @pytest.mark.asyncio
    async def test_gateway_401_after_upstream_failure_is_not_an_upstream_failure(
        self, app_with_id_and_log_middleware, caplog
    ):
        """The upstream label must not survive into the next request in the task.

        ``publish_upstream_provider`` writes durably (the ``req_ctx.push`` scope
        is unwound by the time the error handler runs), and an ASGI server or test
        transport drives sequential scopes from one task. Without a per-request
        reset, the next gateway-issued 401 inherits the previous request's
        provider — so ordinary client-auth churn is logged as an upstream failure
        and counted by ``FailedRequestRateRule``, which is both a false alarm and
        a way to keep the rule permanently breached.
        """
        with caplog.at_level(logging.DEBUG, logger="serving.servers.middleware.request_log"):
            await _get(app_with_id_and_log_middleware, "/v1/chat/completions-upstream-500")
            await _get(app_with_id_and_log_middleware, "/user/me")

        records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert len(records) == 2

        # The upstream failure itself is still attributed and still loud.
        upstream = records[0]
        assert upstream.status_code == 500
        assert upstream.provider == "diffusiongemma"

        # The gateway's own challenge that follows it is not.
        challenge = records[1]
        assert challenge.status_code == 401
        assert challenge.provider is None, "stale upstream label leaked into the next request"
        assert challenge.levelno == logging.DEBUG

        # ...and the failed-request rule agrees, reading the same record fields.
        assert _is_failed_request(_window_item(upstream)) is True
        assert _is_failed_request(_window_item(challenge)) is False
