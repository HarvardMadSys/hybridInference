"""Unit tests for admin authentication helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from serving.servers.auth import log_admin_action, verify_admin_token


@pytest.fixture
def mock_request() -> Request:
    request = MagicMock(spec=Request)
    client = MagicMock()
    client.host = "127.0.0.1"
    request.client = client
    return request  # type: ignore[return-value]


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

    from serving.servers.routers.admin._common import _serialize_for_audit

    result = _serialize_for_audit(data)

    assert float(result["quota"]) == pytest.approx(123.45)
    assert result["expires"] == now.isoformat()
    assert float(result["nested"]["values"][0]) == pytest.approx(1.1)
    assert result["nested"]["values"][1]["ts"] == now.isoformat()
    assert result["plain"] == "ok"


@pytest.mark.asyncio
async def test_log_admin_action_writes_entry(monkeypatch):
    """log_admin_action delegates to store.log_admin_action when available."""
    store = MagicMock()
    store.log_admin_action = AsyncMock()

    await log_admin_action(
        store,
        admin_ip="10.0.0.1",
        action="create_key",
        target_user_id="user123",
        details={"quota": 100},
    )

    store.log_admin_action.assert_awaited_once_with(
        admin_ip="10.0.0.1",
        action="create_key",
        target_user_id="user123",
        details={"quota": 100},
        success=True,
    )


@pytest.mark.asyncio
async def test_log_admin_action_no_store(monkeypatch):
    # Should not raise when passed None
    await log_admin_action(None, admin_ip="0.0.0.0", action="noop")
