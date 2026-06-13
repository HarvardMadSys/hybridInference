"""Tests for the FastAPI app routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from status_monitor.app import create_app
from status_monitor.config import AppConfig, GatewayConfig, RegistryConfig, Settings


def _config() -> AppConfig:
    # No registry and no overrides => scheduler resolves zero targets and makes
    # no outbound HTTP, keeping the test hermetic.
    return AppConfig(
        settings=Settings(base_path="/status-monitor"),
        gateway=GatewayConfig(e2e_interval=3600),
        registry=RegistryConfig(path=None),
    )


def test_health_and_status_and_dashboard() -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["total"] == 0

        page = client.get("/")
        assert page.status_code == 200
        assert "FreeInference Model Status" in page.text


def test_base_path_routes() -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        assert client.get("/status-monitor/api/health").status_code == 200
        assert client.get("/status-monitor/").status_code == 200
