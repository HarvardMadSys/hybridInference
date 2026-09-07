"""Tests for the admin auth-failure blocklist endpoints.

The endpoints exist for one situation: ``servers/auth.py`` consults the
blocklist before it reads the presented key, so a deployment-owned caller whose
credential went stale stays refused for the rest of
``auth_failure_block_duration_sec`` even after the credential is repaired.
These cover seeing that block and ending it.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.settings import settings
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.utils.auth_failure_blocklist import (
    is_ip_blocked,
    record_auth_failure,
    reset_auth_failure_block_state,
)
from serving.utils.logging import JsonFormatter

AUTH = {"Authorization": "Bearer test-admin"}


@pytest.fixture(autouse=True)
def _clean_state():
    reset_auth_failure_block_state()
    yield
    reset_auth_failure_block_state()


@pytest.fixture
def small_limits(monkeypatch):
    """Tiny threshold so a couple of failures block, rather than 200."""
    monkeypatch.setattr(settings, "auth_failure_block_enabled", True)
    monkeypatch.setattr(settings, "auth_failure_block_threshold", 2)
    monkeypatch.setattr(settings, "auth_failure_block_window_sec", 100)
    monkeypatch.setattr(settings, "auth_failure_block_duration_sec", 1000)


@pytest.fixture
async def admin_client(monkeypatch):
    op_store = MagicMock()
    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(),
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    audit = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.auth_blocks.log_admin_action", audit)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, audit


@pytest.mark.asyncio
async def test_list_is_empty_when_nothing_is_blocked(admin_client, small_limits):
    client, _ = admin_client

    response = await client.get("/admin/auth-blocks", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["blocks"] == []
    assert body["enabled"] is True
    # Never claims a deployment-wide view: the blocklist is process memory.
    assert body["process_scoped"] is True


@pytest.mark.asyncio
async def test_list_reports_an_active_block(admin_client, small_limits):
    client, _ = admin_client
    await record_auth_failure("203.0.113.77")
    await record_auth_failure("203.0.113.77")

    response = await client.get("/admin/auth-blocks", headers=AUTH)

    assert response.status_code == 200
    blocks = response.json()["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["ip_bucket"] == "203.0.113.77"
    assert blocks[0]["retry_after_sec"] == 1000
    # Serialized as an aware UTC instant, not a raw epoch float.
    assert blocks[0]["blocked_until"].endswith("Z") or "+00:00" in blocks[0]["blocked_until"]


@pytest.mark.asyncio
async def test_list_reports_the_feature_being_off(admin_client, monkeypatch):
    """``enabled: false`` is surfaced, so an empty list is not read as 'all clear'."""
    client, _ = admin_client
    monkeypatch.setattr(settings, "auth_failure_block_enabled", False)

    response = await client.get("/admin/auth-blocks", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"blocks": [], "enabled": False, "process_scoped": True}


@pytest.mark.asyncio
async def test_clear_lifts_the_block_and_is_audited(admin_client, small_limits):
    client, audit = admin_client
    await record_auth_failure("203.0.113.78")
    await record_auth_failure("203.0.113.78")
    assert (await is_ip_blocked("203.0.113.78"))[0] is True

    response = await client.post(
        "/admin/auth-blocks/clear",
        json={"ip": "203.0.113.78"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {"ip_bucket": "203.0.113.78", "cleared": True}
    assert await is_ip_blocked("203.0.113.78") == (False, 0)

    audit.assert_awaited_once()
    action = audit.await_args.args[2]
    details = audit.await_args.args[4]
    assert action == "auth_blocks.clear"
    assert details == {"ip": "203.0.113.78", "ip_bucket": "203.0.113.78", "cleared": True}


@pytest.mark.asyncio
async def test_clear_of_an_unblocked_source_is_200_not_404(admin_client, small_limits):
    """Nothing to lift is a normal answer, and still audited."""
    client, audit = admin_client

    response = await client.post(
        "/admin/auth-blocks/clear",
        json={"ip": "203.0.113.79"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {"ip_bucket": "203.0.113.79", "cleared": False}
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_clear_accepts_the_bucket_key_from_a_listing(admin_client, small_limits):
    """An IPv6 source is reported as its /64, and that key clears it."""
    client, _ = admin_client
    await record_auth_failure("2001:db8:1234::11")
    await record_auth_failure("2001:db8:1234::22")  # same /64, so same bucket

    listed = (await client.get("/admin/auth-blocks", headers=AUTH)).json()["blocks"]
    assert [b["ip_bucket"] for b in listed] == ["2001:db8:1234::/64"]

    response = await client.post(
        "/admin/auth-blocks/clear",
        json={"ip": "2001:db8:1234::/64"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {"ip_bucket": "2001:db8:1234::/64", "cleared": True}
    assert await is_ip_blocked("2001:db8:1234::11") == (False, 0)


@pytest.mark.asyncio
async def test_audit_failure_does_not_mask_a_successful_clear(admin_client, small_limits, caplog):
    """A store outage must not turn a lifted block into a 500.

    The block lives in process memory and is already lifted by the time the
    audit write runs, so propagating the store error would report failure for
    work that succeeded -- and the retry would answer `cleared: false`, which
    reads as "nothing was blocked". That is the ambiguity, in exactly the store
    outage this endpoint has to survive (Codex's finding).
    """
    client, audit = admin_client
    audit.side_effect = RuntimeError("connection pool exhausted")

    await record_auth_failure("203.0.113.81")
    await record_auth_failure("203.0.113.81")
    assert (await is_ip_blocked("203.0.113.81"))[0] is True

    with caplog.at_level(logging.ERROR, logger="serving.servers.routers.admin.auth_blocks"):
        response = await client.post(
            "/admin/auth-blocks/clear",
            json={"ip": "203.0.113.81"},
            headers=AUTH,
        )

    assert response.status_code == 200
    assert response.json() == {"ip_bucket": "203.0.113.81", "cleared": True}
    # The lift stands, so a retry does not report a confusing `cleared: false`.
    assert await is_ip_blocked("203.0.113.81") == (False, 0)
    audit.assert_awaited_once()

    # With the row lost, this log is the only server-side record of the clear,
    # so it has to carry what happened -- through the formatters, which drop
    # any extra not in ``logging._STRUCTURED_LOG_KEYS``.
    failures = [
        r for r in caplog.records if getattr(r, "event", None) == "auth_block_clear_audit_failed"
    ]
    assert len(failures) == 1, caplog.records
    rendered = json.loads(JsonFormatter().format(failures[0]))
    assert rendered["ip_bucket"] == "203.0.113.81", rendered
    assert rendered["cleared"] is True, rendered


@pytest.mark.asyncio
async def test_endpoints_require_admin_auth(admin_client, small_limits):
    """Neither endpoint is reachable without an admin credential."""
    client, _ = admin_client

    assert (await client.get("/admin/auth-blocks")).status_code == 401
    assert (
        await client.post("/admin/auth-blocks/clear", json={"ip": "203.0.113.80"})
    ).status_code == 401
    assert (
        await client.get("/admin/auth-blocks", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
