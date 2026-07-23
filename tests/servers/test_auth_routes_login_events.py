"""Tests verifying /login writes a login_events row at every outcome path."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_operational_store
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers.auth_routes import router as auth_router


@pytest.fixture
def fake_op_store() -> MagicMock:
    op = MagicMock()
    op.get_user_by_email = AsyncMock(return_value=None)
    op.update_user_last_login = AsyncMock()
    op.update_user_fields = AsyncMock()
    op.create_session = AsyncMock()
    op.record_login_event = AsyncMock()
    return op


@pytest.fixture
def app(fake_op_store, monkeypatch) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    # Register domain-exception handlers so typed exceptions (e.g.
    # AccountSuspendedError) map to their HTTP responses instead of surfacing
    # as unhandled 500s.
    install_exception_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: fake_op_store

    # Patch rate-limit so it doesn't reject in tests by default.
    async def _allow(email, ip):
        return True, None

    monkeypatch.setattr(
        "serving.servers.routers.auth_routes.check_and_record_login",
        _allow,
    )
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    return app


async def _post_login(app, email="x@y.com", password="pw"):
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )


@pytest.mark.asyncio
async def test_login_records_user_not_found(app, fake_op_store):
    fake_op_store.get_user_by_email.return_value = None
    resp = await _post_login(app, email="ghost@example.com")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["email"] == "ghost@example.com"
    assert kw["outcome"] == "failure"
    assert kw["failure_reason"] == "user_not_found"
    assert kw["user_id"] is None


@pytest.mark.asyncio
async def test_login_records_invalid_password(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1",
        "email": "a@b.com",
        "user_name": "A",
        "password_hash": "$2b$12$NOTUSED",
        "role": "free",
        "status": "active",
        "email_verified": True,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "last_login_at": None,
    }
    monkeypatch.setattr(
        "serving.utils.password.verify_password",
        lambda a, b: False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "invalid_password"
    assert kw["user_id"] == "u1"


@pytest.mark.asyncio
async def test_login_records_email_unverified(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1",
        "email": "a@b.com",
        "user_name": "A",
        "password_hash": "$2b$12$NOTUSED",
        "role": "free",
        "status": "active",
        "email_verified": False,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "last_login_at": None,
    }
    monkeypatch.setattr(
        "serving.utils.password.verify_password",
        lambda a, b: True,
    )
    # Force the runtime-settings check to require verification.
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        True,
        raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "email_unverified"


@pytest.mark.asyncio
async def test_login_records_pending_approval(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1",
        "email": "a@b.com",
        "user_name": "A",
        "password_hash": "$2b$12$NOTUSED",
        "role": "free",
        "status": "pending_approval",
        "email_verified": True,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False,
        raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "account_pending_approval"


@pytest.mark.asyncio
async def test_login_records_rejected(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1",
        "email": "a@b.com",
        "user_name": "A",
        "password_hash": "$2b$12$NOTUSED",
        "role": "free",
        "status": "rejected",
        "email_verified": True,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False,
        raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "account_rejected"


@pytest.mark.asyncio
async def test_login_records_inactive(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1",
        "email": "a@b.com",
        "user_name": "A",
        "password_hash": "$2b$12$NOTUSED",
        "role": "free",
        "status": "suspended",
        "email_verified": True,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False,
        raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "account_inactive"


@pytest.mark.asyncio
async def test_login_suspended_takes_precedence_over_unverified(app, fake_op_store, monkeypatch):
    """A suspended account is reported as suspended, not "email not verified".

    Regression: login used to check email verification before account status, so
    a suspended user who was also unverified (``email_verified=False`` while
    verification is required) was rejected with "Email not verified" and shown
    the resend-verification flow instead of being told the account is suspended.
    """
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1",
        "email": "a@b.com",
        "user_name": "A",
        "password_hash": "$2b$12$NOTUSED",
        "role": "free",
        "status": "suspended",
        "email_verified": False,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    # Require verification: the pre-fix ordering would short-circuit into the
    # "email not verified" branch here before ever checking the status.
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        True,
        raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 403
    # The account-status branch must win over the email-verification branch, and
    # it returns the typed ACCOUNT_SUSPENDED code (not an EMAIL_NOT_VERIFIED /
    # unknown error) so the client can render a dedicated "suspended" message.
    data = resp.json()
    assert data["error_code"] == "ACCOUNT_SUSPENDED"
    assert data["status"] == "suspended"
    assert "not verified" not in resp.text.lower()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "account_inactive"


@pytest.mark.asyncio
async def test_login_records_rate_limited(app, fake_op_store, monkeypatch):
    async def _deny(email, ip):
        return False, "ip"

    monkeypatch.setattr(
        "serving.servers.routers.auth_routes.check_and_record_login",
        _deny,
    )
    resp = await _post_login(app, email="x@y.com")
    assert resp.status_code == 429
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["failure_reason"] == "rate_limited"
    assert kw["user_id"] is None


@pytest.mark.asyncio
async def test_login_records_success(app, fake_op_store, monkeypatch):
    fake_op_store.get_user_by_email.return_value = {
        "id": "u1",
        "email": "a@b.com",
        "user_name": "A",
        "password_hash": "$2b$12$NOTUSED",
        "role": "free",
        "status": "active",
        "email_verified": True,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "last_login_at": None,
    }
    monkeypatch.setattr("serving.utils.password.verify_password", lambda a, b: True)
    monkeypatch.setattr(
        "serving.config.settings.settings.signup_require_email_verification",
        False,
        raising=False,
    )
    resp = await _post_login(app, email="a@b.com")
    assert resp.status_code == 200
    fake_op_store.record_login_event.assert_awaited_once()
    kw = fake_op_store.record_login_event.await_args.kwargs
    assert kw["outcome"] == "success"
    assert kw["failure_reason"] is None
    assert kw["user_id"] == "u1"


@pytest.mark.asyncio
async def test_login_succeeds_when_audit_write_fails(app, fake_op_store, monkeypatch):
    """If record_login_event raises, login still returns the right status."""
    fake_op_store.record_login_event = AsyncMock(side_effect=RuntimeError("db down"))
    fake_op_store.get_user_by_email.return_value = None
    resp = await _post_login(app, email="ghost@example.com")
    assert resp.status_code == 401  # not 500
