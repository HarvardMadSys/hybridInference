"""Unit tests for admin authentication helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from serving.config.settings import get_settings
from serving.servers.auth import log_admin_action, verify_admin_token
from serving.servers.routers import admin as admin_router


class _AcquireContext:
    """Async context manager returning the mocked connection."""

    def __init__(self, connection: AsyncMock) -> None:
        self._connection = connection

    async def __aenter__(self) -> AsyncMock:
        return self._connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None


@pytest.fixture
def mock_request() -> Request:
    request = MagicMock(spec=Request)
    client = MagicMock()
    client.host = "127.0.0.1"
    request.client = client
    return request  # type: ignore[return-value]


@pytest.fixture
def db_logger_with_pool():
    logger = MagicMock()
    connection = AsyncMock()
    connection.execute = AsyncMock()
    connection.fetchrow = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value = _AcquireContext(connection)
    logger.pool = pool
    return logger, connection


def _recent_request_row() -> dict[str, Any]:
    """Return a request row containing stored prompt/response content."""
    return {
        "request_id": "req_123",
        "user_id": "user_123",
        "user_name": "Test User",
        "user_email": "user@example.com",
        "user_ip": "203.0.113.10",
        "model_id": "test-model",
        "provider": "test-provider",
        "timestamp": datetime.now(timezone.utc),
        "status_code": 200,
        "latency_ms": 123,
        "ttft_ms": 45,
        "stream": False,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "reasoning_tokens": 0,
        "total_tokens": 30,
        "cost_usd": Decimal("0.01"),
        "prompt": "sensitive prompt",
        "response": "sensitive response",
        "error": None,
    }


@pytest.mark.asyncio
async def test_verify_admin_token_success(monkeypatch, mock_request):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    result = await verify_admin_token(
        request=mock_request,
        authorization="Bearer test-admin",
    )
    assert result == "127.0.0.1"


@pytest.mark.asyncio
async def test_verify_admin_token_missing_header(monkeypatch, mock_request):
    monkeypatch.setenv("ADMIN_TOKEN", "another-token")
    with pytest.raises(HTTPException) as exc:
        await verify_admin_token(request=mock_request, authorization=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_admin_token_invalid_token(monkeypatch, mock_request):
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")
    with pytest.raises(HTTPException) as exc:
        await verify_admin_token(request=mock_request, authorization="Bearer wrong")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_admin_token_missing_env(monkeypatch, mock_request):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        await verify_admin_token(request=mock_request, authorization="Bearer anything")
    assert exc.value.status_code == 500


def test_serialize_for_audit_handles_decimal_and_datetime():
    now = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    data = {
        "quota": Decimal("123.45"),
        "expires": now,
        "nested": {"values": [Decimal("1.1"), {"ts": now}]},
        "plain": "ok",
    }

    result = admin_router._serialize_for_audit(data)

    assert float(result["quota"]) == pytest.approx(123.45)
    assert result["expires"] == now.isoformat()
    assert float(result["nested"]["values"][0]) == pytest.approx(1.1)
    assert result["nested"]["values"][1]["ts"] == now.isoformat()
    assert result["plain"] == "ok"


@pytest.mark.asyncio
async def test_log_admin_action_writes_entry(monkeypatch, db_logger_with_pool):
    logger, connection = db_logger_with_pool
    await log_admin_action(
        logger,
        admin_ip="10.0.0.1",
        action="create_key",
        target_user_id="user123",
        details={"quota": 100},
    )

    connection.execute.assert_awaited()
    call = connection.execute.await_args
    sql = call.args[0]
    assert "INSERT INTO admin_audit_log" in sql
    assert call.args[1] == "10.0.0.1"
    assert call.args[2] == "create_key"
    assert call.args[3] == "user123"
    assert call.args[4] == '{"quota": 100}'
    assert call.args[5] is True


@pytest.mark.asyncio
async def test_log_admin_action_no_pool(monkeypatch):
    logger = MagicMock()
    logger.pool = None
    # Should not raise even if pool missing
    await log_admin_action(logger, admin_ip="0.0.0.0", action="noop")


@pytest.mark.asyncio
async def test_admin_recent_requests_hides_stored_content_by_default(
    monkeypatch, mock_request, db_logger_with_pool
):
    monkeypatch.delenv("ADMIN_SHOW_REQUEST_CONTENT", raising=False)
    get_settings.cache_clear()
    logger, connection = db_logger_with_pool
    connection.fetchrow.return_value = {"total": 1}
    connection.fetch.return_value = [_recent_request_row()]

    try:
        result = await admin_router.admin_list_recent_requests(
            request=mock_request,
            admin_id="admin",
            db_logger=logger,
        )
    finally:
        get_settings.cache_clear()

    assert result.requests[0].prompt is None
    assert result.requests[0].response is None
    assert result.requests[0].content_hidden is True
    query = connection.fetch.await_args.args[0]
    assert "NULL::text AS prompt" in query
    assert "NULL::text AS response" in query


@pytest.mark.asyncio
async def test_admin_recent_requests_can_opt_in_to_content_visibility(
    monkeypatch, mock_request, db_logger_with_pool
):
    monkeypatch.setenv("ADMIN_SHOW_REQUEST_CONTENT", "1")
    get_settings.cache_clear()
    logger, connection = db_logger_with_pool
    connection.fetchrow.return_value = {"total": 1}
    connection.fetch.return_value = [_recent_request_row()]

    try:
        result = await admin_router.admin_list_recent_requests(
            request=mock_request,
            admin_id="admin",
            db_logger=logger,
        )
    finally:
        get_settings.cache_clear()

    assert result.requests[0].prompt == "sensitive prompt"
    assert result.requests[0].response == "sensitive response"
    assert result.requests[0].content_hidden is False
    query = connection.fetch.await_args.args[0]
    assert "l.prompt, l.response" in query
