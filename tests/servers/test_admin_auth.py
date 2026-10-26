"""Unit tests for admin authentication helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.audit import AuditActor, AuditTarget
from serving.servers.auth import log_admin_action


def test_serialize_for_audit_handles_decimal_and_datetime():
    now = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
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
    """The legacy positional form still delegates, with no actor recorded."""
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
        actor=None,
        target=None,
    )


@pytest.mark.asyncio
async def test_log_admin_action_passes_actor_and_target(monkeypatch):
    store = MagicMock()
    store.log_admin_action = AsyncMock()
    actor = AuditActor.admin("u-admin", "198.51.100.7")

    await log_admin_action(
        store, action="models.concurrency.update", actor=actor, target=AuditTarget.model("m1")
    )

    kwargs = store.log_admin_action.await_args.kwargs
    assert kwargs["actor"] is actor
    assert kwargs["target"] == AuditTarget.model("m1")
    assert kwargs["admin_ip"] is None
    assert kwargs["target_user_id"] is None


@pytest.mark.asyncio
async def test_log_admin_action_no_store(monkeypatch):
    # Should not raise when passed None
    await log_admin_action(None, admin_ip="0.0.0.0", action="noop")
