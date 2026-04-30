"""Integration tests for /admin/broadcast-email/* endpoints.

These mirror the pattern used by test_admin_api.py: a FastAPI app is built
with the admin router mounted, the DB pool is mocked at the connection level,
and admin auth is satisfied via the ADMIN_TOKEN env var.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


class _AcquireContext:
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


class _TransactionContext:
    """Mocks `async with conn.transaction(): ...` as a no-op."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None


@pytest.fixture
def mocked_db_logger():
    logger = MagicMock()
    connection = AsyncMock()
    connection.fetch = AsyncMock()
    connection.fetchrow = AsyncMock()
    connection.execute = AsyncMock()
    connection.executemany = AsyncMock()
    connection.transaction = MagicMock(return_value=_TransactionContext())
    pool = MagicMock()
    pool.acquire.side_effect = lambda: _AcquireContext(connection)
    logger.pool = pool
    return logger, connection


@pytest.fixture
async def admin_client(monkeypatch, mocked_db_logger):
    logger, connection = mocked_db_logger
    app = FastAPI(title="Broadcast Email Test")

    services = AppServices(
        router=MagicMock(),
        db_logger=logger,
        rate_limiter=None,
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    mock_log_action = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.log_admin_action", mock_log_action)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")

    try:
        yield client, connection, mock_log_action
    finally:
        await client.aclose()


def _auth_header() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin"}


# ── Auth ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_requires_admin_token(admin_client):
    client, _conn, _log = admin_client
    resp = await client.post(
        "/admin/broadcast-email/preview",
        json={"target_roles": ["free"], "target_statuses": ["active"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_requires_admin_token(admin_client):
    client, _conn, _log = admin_client
    resp = await client.post(
        "/admin/broadcast-email",
        json={
            "subject": "x",
            "body_html": "<p>x</p>",
            "body_text": "x",
            "target_roles": ["free"],
            "target_statuses": ["active"],
        },
    )
    assert resp.status_code == 401


# ── Validation ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_rejects_empty_target_filters(admin_client):
    """Empty target_roles or target_statuses must be 422 (would match nothing)."""
    client, _conn, _log = admin_client

    resp = await client.post(
        "/admin/broadcast-email/preview",
        headers=_auth_header(),
        json={"target_roles": [], "target_statuses": ["active"]},
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/admin/broadcast-email/preview",
        headers=_auth_header(),
        json={"target_roles": ["free"], "target_statuses": []},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_preview_unknown_template_returns_422(admin_client):
    """ValueError from render_broadcast_template should map to 422."""
    client, _conn, _log = admin_client

    resp = await client.post(
        "/admin/broadcast-email/preview",
        headers=_auth_header(),
        json={
            "template_key": "does-not-exist",
            "target_roles": ["free"],
            "target_statuses": ["active"],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_empty_subject_returns_422(admin_client):
    """Custom content with empty subject/body_html must be rejected."""
    client, _conn, _log = admin_client

    resp = await client.post(
        "/admin/broadcast-email",
        headers=_auth_header(),
        json={
            "subject": "",
            "body_html": "",
            "body_text": "",
            "target_roles": ["free"],
            "target_statuses": ["active"],
        },
    )
    assert resp.status_code == 422


# ── Happy paths ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_returns_count_and_rendered(admin_client):
    client, conn, _log = admin_client
    conn.fetchrow.return_value = {"cnt": 7}

    resp = await client.post(
        "/admin/broadcast-email/preview",
        headers=_auth_header(),
        json={
            "subject": "Hello",
            "body_html": "<p>Hi</p>",
            "body_text": "Hi",
            "target_roles": ["free"],
            "target_statuses": ["active"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["recipient_count"] == 7
    assert body["rendered_subject"] == "Hello"
    assert body["rendered_body_html"] == "<p>Hi</p>"


@pytest.mark.asyncio
async def test_create_inserts_broadcast_and_snapshots_recipients(admin_client, monkeypatch):
    """Create endpoint must INSERT the broadcast, snapshot recipients in the
    same transaction, and call schedule_broadcast()."""
    client, conn, log_action = admin_client
    # Recipients query returns 2 users.
    conn.fetch.return_value = [
        {"id": "u1", "email": "a@example.com"},
        {"id": "u2", "email": "b@example.com"},
    ]
    conn.fetchrow.return_value = None  # not used in the new flow

    sched_calls: list[tuple] = []

    def _fake_schedule(broadcast_id, run_at):
        sched_calls.append((broadcast_id, run_at))

    monkeypatch.setattr("serving.servers.routers.admin.schedule_broadcast", _fake_schedule)

    resp = await client.post(
        "/admin/broadcast-email",
        headers=_auth_header(),
        json={
            "subject": "Hi all",
            "body_html": "<p>Hello</p>",
            "body_text": "Hello",
            "target_roles": ["free"],
            "target_statuses": ["active"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["recipient_count"] == 2
    assert body["status"] == "scheduled"

    # Recipients were snapshotted via executemany.
    conn.executemany.assert_awaited()
    rows = conn.executemany.await_args.args[1]
    assert {r[1] for r in rows} == {"u1", "u2"}
    assert {r[2] for r in rows} == {"a@example.com", "b@example.com"}

    # schedule_broadcast() was invoked with the new broadcast id.
    assert len(sched_calls) == 1
    assert sched_calls[0][0] == body["id"]

    log_action.assert_awaited()


@pytest.mark.asyncio
async def test_create_marks_failed_when_scheduler_raises(admin_client, monkeypatch):
    """If schedule_broadcast() raises, the broadcast row must be flipped to
    'failed' and the response must be 503 (not stuck in 'scheduled')."""
    client, conn, _log = admin_client
    conn.fetch.return_value = []  # zero recipients is fine for this test

    def _boom(_bid, _run_at):
        raise RuntimeError("Scheduler not started")

    monkeypatch.setattr("serving.servers.routers.admin.schedule_broadcast", _boom)

    resp = await client.post(
        "/admin/broadcast-email",
        headers=_auth_header(),
        json={
            "subject": "Hi",
            "body_html": "<p>Hi</p>",
            "body_text": "Hi",
            "target_roles": ["free"],
            "target_statuses": ["active"],
        },
    )
    assert resp.status_code == 503

    failed_marks = [
        c for c in conn.execute.await_args_list if "status = 'failed'" in str(c.args[0])
    ]
    assert failed_marks, "broadcast row should be flipped to 'failed' on scheduler error"


@pytest.mark.asyncio
async def test_list_clamps_limit_and_offset(admin_client):
    client, conn, _log = admin_client
    conn.fetchrow.return_value = {"cnt": 0}
    conn.fetch.return_value = []

    resp = await client.get(
        "/admin/broadcast-email?limit=99999&offset=-50",
        headers=_auth_header(),
    )
    assert resp.status_code == 200

    # The data fetch (not the COUNT) must use the clamped values.
    fetch_calls = [c for c in conn.fetch.await_args_list if "ORDER BY created_at DESC" in c.args[0]]
    assert fetch_calls, "expected the broadcast list query"
    args = fetch_calls[0].args
    assert args[1] == 200, f"limit should be clamped to 200, got {args[1]}"
    assert args[2] == 0, f"offset should be clamped to >=0, got {args[2]}"


@pytest.mark.asyncio
async def test_cancel_returns_409_when_not_scheduled(admin_client, monkeypatch):
    client, conn, _log = admin_client
    conn.fetchrow.return_value = {"status": "sent"}

    monkeypatch.setattr("serving.servers.routers.admin.cancel_broadcast_job", lambda _bid: None)

    resp = await client.delete(
        "/admin/broadcast-email/some-id",
        headers=_auth_header(),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_marks_cancelled_and_calls_scheduler(admin_client, monkeypatch):
    client, conn, log_action = admin_client
    conn.fetchrow.return_value = {"status": "scheduled"}

    cancel_calls: list[str] = []
    monkeypatch.setattr(
        "serving.servers.routers.admin.cancel_broadcast_job",
        lambda bid: cancel_calls.append(bid),
    )

    resp = await client.delete(
        "/admin/broadcast-email/some-id",
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    assert cancel_calls == ["some-id"]

    update_calls = [
        c for c in conn.execute.await_args_list if "status = 'cancelled'" in str(c.args[0])
    ]
    assert update_calls
    log_action.assert_awaited()


@pytest.mark.asyncio
async def test_detail_returns_404_when_unknown(admin_client):
    client, conn, _log = admin_client
    conn.fetchrow.return_value = None

    resp = await client.get(
        "/admin/broadcast-email/missing-id",
        headers=_auth_header(),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_detail_returns_broadcast_with_recipients(admin_client):
    client, conn, _log = admin_client
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn.fetchrow.side_effect = [
        {
            "id": "bc1",
            "subject": "Hello",
            "status": "sent",
            "recipient_count": 2,
            "scheduled_at": None,
            "sent_at": created_at,
            "created_by": "admin@example.com",
            "created_at": created_at,
        },
        {"cnt": 2},
    ]
    conn.fetch.return_value = [
        {
            "user_id": "u1",
            "email": "a@example.com",
            "status": "sent",
            "error": None,
            "sent_at": created_at,
        },
        {
            "user_id": "u2",
            "email": "b@example.com",
            "status": "failed",
            "error": "RuntimeError: nope",
            "sent_at": None,
        },
    ]

    resp = await client.get(
        "/admin/broadcast-email/bc1",
        headers=_auth_header(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["broadcast"]["id"] == "bc1"
    assert body["total_recipients"] == 2
    assert len(body["recipients"]) == 2
    failed = next(r for r in body["recipients"] if r["user_id"] == "u2")
    assert failed["error"] == "RuntimeError: nope"
