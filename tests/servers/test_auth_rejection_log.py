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


def _build_blocked_app(
    monkeypatch,
    *,
    lightweight_user: dict[str, Any] | None,
    logging_on: bool,
) -> tuple[FastAPI, list[dict], MagicMock]:
    """App with a POST inference route, for the ip_blocked rejection path.

    POST (not GET) because the point of these tests is the request *body*:
    ``ip_blocked`` is refused in a dependency, before any handler has parsed it.
    Returns the app, the captured log_rejection calls, and the op_store mock so
    a test can assert whether the identity lookup was attempted at all.
    """
    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr("serving.servers.auth.log_rejection", fake_log_rejection)

    op = MagicMock()
    op.get_auth_context_lightweight = AsyncMock(return_value=lightweight_user)

    async def fake_op_store_dep():
        return op

    async def fake_log_store_dep():
        return MagicMock()

    from serving.servers.deps import get_log_store, get_operational_store

    app = FastAPI()
    app.dependency_overrides[get_operational_store] = fake_op_store_dep
    app.dependency_overrides[get_log_store] = fake_log_store_dep

    from fastapi import Depends

    @app.post("/v1/chat/completions")
    async def hit(user: dict = Depends(verify_api_key)):
        return {"ok": True}

    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=logging_on)
    app.state.services.runtime_settings = rs
    return app, log_calls, op


@pytest.fixture
def blocked_localhost(monkeypatch):
    """Yield a coroutine that trips the auth-failure block for a given IP.

    Threshold 1 so a single recorded failure blocks the ASGI client's peer.
    The blocklist is per-process module state, so it is wiped either side.
    """
    from serving.config.settings import settings
    from serving.utils.auth_failure_blocklist import (
        record_auth_failure,
        reset_auth_failure_block_state,
    )

    reset_auth_failure_block_state()
    monkeypatch.setattr(settings, "auth_failure_block_enabled", True)
    monkeypatch.setattr(settings, "auth_failure_block_threshold", 1)
    monkeypatch.setattr(settings, "auth_failure_block_window_sec", 100)
    monkeypatch.setattr(settings, "auth_failure_block_duration_sec", 1000)
    yield record_auth_failure
    reset_auth_failure_block_state()


@pytest.mark.asyncio
async def test_blocked_ip_rejection_logs_prompt_and_user(monkeypatch, blocked_localhost):
    """An ip_blocked row carries the request prompt and the caller's identity.

    Both are what the admin dashboard renders per row, and both were null on
    this path before: the refusal happens in a dependency, so nothing had read
    the body or resolved the presented key.
    """
    user_row = {"user_id": "u1", "role": "pro", "email": None, "email_verified": True}
    app, log_calls, _op = _build_blocked_app(
        monkeypatch, lightweight_user=user_row, logging_on=True
    )
    await blocked_localhost("127.0.0.1")

    messages = [{"role": "user", "content": "who am I"}]
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": messages},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "ip_blocked"
    assert log_calls[0]["prompt"] == messages
    assert log_calls[0]["user"] == {"user_id": "u1", "role": "pro"}


@pytest.mark.asyncio
async def test_blocked_ip_rejection_without_a_valid_key_has_no_user(monkeypatch, blocked_localhost):
    """A scanner with no resolvable key still logs its prompt, with a null user."""
    app, log_calls, _op = _build_blocked_app(monkeypatch, lightweight_user=None, logging_on=True)
    await blocked_localhost("127.0.0.1")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "probe"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert log_calls[0]["user"] is None
    assert log_calls[0]["prompt"] == [{"role": "user", "content": "probe"}]


@pytest.mark.asyncio
async def test_blocked_ip_skips_enrichment_when_rejection_logging_is_off(
    monkeypatch, blocked_localhost
):
    """With the toggle off, the block costs no body read and no identity lookup.

    The shed path stays cheap: enrichment is only worth paying for when the row
    it enriches will actually be written.
    """
    user_row = {"user_id": "u1", "role": "pro"}
    app, log_calls, op = _build_blocked_app(
        monkeypatch, lightweight_user=user_row, logging_on=False
    )
    await blocked_localhost("127.0.0.1")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert log_calls[0]["prompt"] == ""
    assert log_calls[0]["user"] is None
    op.get_auth_context_lightweight.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_ip_identity_lookup_failure_still_returns_429(monkeypatch, blocked_localhost):
    """A broken identity lookup degrades the log row, never the response."""
    app, log_calls, op = _build_blocked_app(monkeypatch, lightweight_user=None, logging_on=True)
    op.get_auth_context_lightweight = AsyncMock(side_effect=RuntimeError("db down"))
    await blocked_localhost("127.0.0.1")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1000"
    assert log_calls[0]["user"] is None
    # The prompt is captured independently, so it survives the failed lookup.
    assert log_calls[0]["prompt"] == [{"role": "user", "content": "hi"}]


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
