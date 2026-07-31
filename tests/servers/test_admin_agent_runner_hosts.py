"""Tests for the admin agent runner host pool endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router

ADMIN = {"Authorization": "Bearer test-admin"}


class FakeHostStore:
    """The runner-host surface of AgentJobStore, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def seed(self, host: str, *, active: bool = False, seen_minutes_ago: float = 0.0) -> None:
        seen = datetime.now(timezone.utc) - timedelta(minutes=seen_minutes_ago)
        self.rows[host] = {
            "host": host,
            "is_active": active,
            "last_worker_id": f"w-{host}",
            "first_seen_at": seen,
            "last_seen_at": seen,
        }

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        return sorted(
            (dict(row) for row in self.rows.values()),
            key=lambda row: row["last_seen_at"],
            reverse=True,
        )

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        # Mirrors the store: an unknown host changes nothing at all.
        if host is not None and host not in self.rows:
            return False
        for row in self.rows.values():
            row["is_active"] = False
        if host is not None:
            self.rows[host]["is_active"] = True
        return True

    async def forget_runner_host(self, *, host: str) -> bool:
        return self.rows.pop(host, None) is not None


@pytest.fixture
async def admin_client(monkeypatch):
    store = FakeHostStore()
    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(),
        operational_store=MagicMock(),
        db_logger=MagicMock(),
        log_store=MagicMock(),
        agent_job_store=store,
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr(
        "serving.servers.routers.admin.agent_runner_hosts.log_admin_action",
        AsyncMock(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store


@pytest.mark.asyncio
async def test_pool_is_empty_and_unpinned_before_any_runner_polls(admin_client):
    client, _ = admin_client

    response = await client.get("/admin/agent/runner-hosts", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["hosts"] == []
    # Unpinned, not "pinned to nothing": a deployment that never touches this
    # page must keep claiming exactly as it did before the feature existed.
    assert body["active_host"] is None


@pytest.mark.asyncio
async def test_listing_reports_poll_age_rather_than_liveness(admin_client):
    client, store = admin_client
    store.seed("runner-a", active=True)
    store.seed("runner-b", seen_minutes_ago=45)

    body = (await client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()

    assert body["active_host"] == "runner-a"
    by_host = {row["host"]: row for row in body["hosts"]}
    assert by_host["runner-a"]["active"] is True
    assert by_host["runner-a"]["seconds_since_seen"] < 60
    assert 2600 < by_host["runner-b"]["seconds_since_seen"] < 2800
    assert by_host["runner-b"]["active"] is False


@pytest.mark.asyncio
async def test_switching_host_returns_the_new_pool_state(admin_client):
    client, store = admin_client
    store.seed("runner-a", active=True)
    store.seed("runner-b")

    response = await client.put(
        "/admin/agent/runner-hosts/active", json={"host": "runner-b"}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json()["active_host"] == "runner-b"
    assert store.rows["runner-a"]["is_active"] is False
    assert store.rows["runner-b"]["is_active"] is True


@pytest.mark.asyncio
async def test_unpinning_lets_any_runner_claim_again(admin_client):
    client, store = admin_client
    store.seed("runner-b", active=True)

    response = await client.put(
        "/admin/agent/runner-hosts/active", json={"host": None}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json()["active_host"] is None
    assert store.rows["runner-b"]["is_active"] is False


@pytest.mark.asyncio
async def test_pinning_an_unknown_host_is_refused(admin_client):
    """Regression: a typo here parks the queue with nothing in the logs.

    Pinning to a machine no runner has ever polled from means every job queues
    forever, and the only symptom is silence — so the write is refused rather
    than accepted and discovered later.
    """
    client, store = admin_client
    store.seed("runner-a", active=True)

    response = await client.put(
        "/admin/agent/runner-hosts/active", json={"host": "runner-b-typo"}, headers=ADMIN
    )

    assert response.status_code == 404
    assert "has ever polled" in response.json()["detail"]
    # And the previously active host keeps running jobs.
    assert store.rows["runner-a"]["is_active"] is True


@pytest.mark.asyncio
async def test_forgetting_the_active_host_is_refused(admin_client):
    """Removing it would silently unpin — every other machine starts claiming."""
    client, store = admin_client
    store.seed("runner-b", active=True)

    response = await client.delete("/admin/agent/runner-hosts/runner-b", headers=ADMIN)

    assert response.status_code == 409
    assert "runner-b" in store.rows


@pytest.mark.asyncio
async def test_forgetting_a_stale_host_drops_it(admin_client):
    client, store = admin_client
    store.seed("runner-a", active=True)
    store.seed("old-box", seen_minutes_ago=6000)

    response = await client.delete("/admin/agent/runner-hosts/old-box", headers=ADMIN)

    assert response.status_code == 200
    assert [row["host"] for row in response.json()["hosts"]] == ["runner-a"]


@pytest.mark.asyncio
async def test_forgetting_an_unknown_host_is_a_404(admin_client):
    client, _ = admin_client

    response = await client.delete("/admin/agent/runner-hosts/never-existed", headers=ADMIN)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_pool_is_admin_only(admin_client):
    """The list names internal machines, and the switch redirects all agent work."""
    client, _ = admin_client

    assert (await client.get("/admin/agent/runner-hosts")).status_code in (401, 403)
    assert (
        await client.put("/admin/agent/runner-hosts/active", json={"host": None})
    ).status_code in (401, 403)


@pytest.mark.asyncio
async def test_endpoints_report_unconfigured_deployments_as_503(monkeypatch):
    """A deployment without agent jobs answers 'not configured', not 500."""
    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(),
        operational_store=MagicMock(),
        db_logger=MagicMock(),
        log_store=MagicMock(),
        agent_job_store=None,
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/agent/runner-hosts", headers=ADMIN)

    assert response.status_code == 503
