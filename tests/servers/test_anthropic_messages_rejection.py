"""Test that anthropic /v1/messages emits log_rejection on 404."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import get_log_store, get_router


@pytest.mark.asyncio
async def test_anthropic_unknown_model_logs_rejection(monkeypatch):
    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "serving.servers.routers.anthropic_messages.log_rejection",
        fake_log_rejection,
    )

    from serving.servers.routers import anthropic_messages as mod

    app = FastAPI()
    app.include_router(mod.router)

    # Stub user, router (with no routes), no concurrency limiter.
    user = {"user_id": "u1", "role": "free", "is_admin": False, "authenticated": True}

    fake_router = MagicMock()
    fake_router.routes = {}

    async def _verify():
        return user

    async def _get_router():
        return fake_router

    async def _get_log_store():
        return MagicMock()

    async def _enforce():
        yield

    app.dependency_overrides[verify_api_key] = _verify
    app.dependency_overrides[get_router] = _get_router
    app.dependency_overrides[get_log_store] = _get_log_store
    app.dependency_overrides[enforce_user_concurrency] = _enforce

    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    app.state.services.runtime_settings = MagicMock()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/anthropic/v1/messages",
            json={
                "model": "claude-bogus-9000",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer test"},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 404
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "model_not_found"
    assert log_calls[0]["model_id"] == "claude-bogus-9000"
    assert log_calls[0]["user"]["user_id"] == "u1"
