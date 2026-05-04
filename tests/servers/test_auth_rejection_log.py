"""Tests verifying verify_api_key fires log_rejection at its rejection sites."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key


def _build_app(monkeypatch, *, op_store_user: dict[str, Any] | None) -> tuple[FastAPI, list[dict]]:
    """Wire up a tiny app whose only endpoint depends on verify_api_key.

    Returns the app plus the list that captures log_rejection invocations.
    """
    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "serving.servers.auth.log_rejection",
        fake_log_rejection,
    )

    app = FastAPI()

    async def fake_op_store_dep():
        op = MagicMock()
        op.get_auth_context_by_key_hash = AsyncMock(return_value=op_store_user)
        op.get_user_cost_today = AsyncMock(return_value=0.0)
        op.update_key_last_used = AsyncMock()
        return op

    async def fake_log_store_dep():
        return MagicMock()

    from serving.servers.deps import get_log_store, get_operational_store

    app.dependency_overrides[get_operational_store] = fake_op_store_dep
    app.dependency_overrides[get_log_store] = fake_log_store_dep

    @app.get("/v1/chat/completions")
    async def hit(user: dict = pytest.importorskip("fastapi").Depends(verify_api_key)):
        return {"ok": True}

    # Stub services so the helper can read log_store / runtime_settings.
    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    app.state.services.runtime_settings = MagicMock()
    return app, log_calls


@pytest.mark.asyncio
async def test_missing_api_key_logs_rejection(monkeypatch):
    """No Authorization header -> 401 + log_rejection(error_code='auth_missing')."""
    app, log_calls = _build_app(monkeypatch, op_store_user=None)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/chat/completions")
        await asyncio.sleep(0)
    assert resp.status_code == 401
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "auth_missing"
    assert log_calls[0]["status_code"] == 401
    assert log_calls[0]["user"] is None


@pytest.mark.asyncio
async def test_invalid_api_key_logs_rejection(monkeypatch):
    """Unknown key -> 401 + log_rejection(error_code='auth_invalid')."""
    app, log_calls = _build_app(monkeypatch, op_store_user=None)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-bogus"},
        )
        await asyncio.sleep(0)
    assert resp.status_code == 401
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "auth_invalid"


@pytest.mark.asyncio
async def test_quota_exceeded_logs_rejection(monkeypatch):
    """Authenticated user over quota -> 429 + log_rejection(error_code='quota_exceeded')."""
    user_row = {
        "id": 1,
        "user_id": "u1",
        "user_name": "Test",
        "role": "free",
        "email": None,
        "email_verified": True,
        "quota_daily_cost_usd": 0.001,  # very low
    }
    app, log_calls = _build_app(monkeypatch, op_store_user=user_row)

    # Override get_user_cost_today to push us over the quota.
    async def fake_op_store_dep():
        op = MagicMock()
        op.get_auth_context_by_key_hash = AsyncMock(return_value=user_row)
        op.get_user_cost_today = AsyncMock(return_value=10.0)
        op.update_key_last_used = AsyncMock()
        return op

    from serving.servers.deps import get_operational_store

    app.dependency_overrides[get_operational_store] = fake_op_store_dep

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
        )
        await asyncio.sleep(0)
    assert resp.status_code == 429
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "quota_exceeded"
    assert log_calls[0]["user"]["user_id"] == "u1"
