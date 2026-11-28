"""Tests for admin-created service accounts (POST /admin/service-accounts)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from serving.audit import AuditActor
from serving.exceptions import LoginNameTakenError
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin.users import derive_service_login_name
from serving.utils import password as password_utils
from serving.utils.login_rate_limit import reset_login_rate_limit_state

ADMIN_PASSWORD = "correct-admin-password"


@pytest.fixture
def mock_op_store():
    op_store = MagicMock()
    op_store.create_user = AsyncMock()
    op_store.create_service_account = AsyncMock(return_value={"id": 1, "created_at": None})
    op_store.get_user_by_login_name = AsyncMock(return_value=None)
    op_store.get_user_by_id = AsyncMock(
        return_value={"id": "u-admin", "email": "admin@example.com", "password_hash": "h"}
    )
    op_store.update_user_fields = AsyncMock()
    op_store.get_default_project = AsyncMock(return_value={"id": "proj-1", "name": "Default"})
    op_store.create_key = AsyncMock()
    op_store.list_user_identities = AsyncMock(return_value=[])
    return op_store


@pytest.fixture
async def client(mock_op_store, monkeypatch):
    app = FastAPI(title="Service Account Test")
    services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=mock_op_store,
        log_store=MagicMock(),
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin_router.router)
    mock_log_action = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.users.log_admin_action", mock_log_action)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")

    async def console_session(request: Request) -> str:
        request.state.admin_user_id = "u-admin"
        request.state.audit_actor = AuditActor.admin("u-admin", "127.0.0.1")
        return "admin@example.com"

    app.dependency_overrides[verify_admin_access] = console_session
    monkeypatch.setattr(
        password_utils, "verify_password", lambda plain, _hash: plain == ADMIN_PASSWORD
    )
    reset_login_rate_limit_state()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, mock_op_store, mock_log_action
    reset_login_rate_limit_state()


@pytest.mark.asyncio
async def test_create_service_account_success(client):
    http, op_store, log_action = client
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "Nightly Eval Bot", "role": "pro", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["login_name"] == "nightly-eval-bot"
    assert body["role"] == "pro"
    assert body["api_key"].startswith("hyi-")
    assert body["key_prefix"] == body["api_key"][:12]

    # One storage call writes the account, its role and its key together.
    op_store.create_service_account.assert_awaited_once()
    create = op_store.create_service_account.await_args.kwargs
    assert create["user_id"] == body["user_id"]
    assert create["login_name"] == "nightly-eval-bot"
    assert create["user_name"] == "Nightly Eval Bot"
    assert create["role"] == "pro"
    assert create["key_prefix"] == body["key_prefix"]
    assert create["key_hash"] != body["api_key"]
    op_store.create_user.assert_not_awaited()
    op_store.update_user_fields.assert_not_awaited()
    op_store.create_key.assert_not_awaited()
    log_action.assert_awaited()


@pytest.mark.asyncio
async def test_create_service_account_duplicate_login_is_409(client):
    http, op_store, _log_action = client
    op_store.get_user_by_login_name.return_value = {"id": "someone"}
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "Bot", "login_name": "taken", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 409
    op_store.create_service_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_service_account_admin_role_is_refused(client):
    http, op_store, _log_action = client
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "Bot", "role": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 422
    op_store.create_service_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_service_account_wrong_password_creates_nothing(client):
    http, op_store, _log_action = client
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "Bot", "password": "wrong"},
    )
    assert response.status_code in (400, 401, 403)
    op_store.create_service_account.assert_not_awaited()


@pytest.mark.parametrize(
    "login_name",
    ["ops@bot", "has space", "x" * 255, "ab", "-leading", "bad/slash"],
)
@pytest.mark.asyncio
async def test_create_service_account_refuses_an_invalid_login_name(client, login_name):
    """A given login name is held to the rule every login name follows, before any check."""
    http, op_store, _log_action = client
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "Bot", "login_name": login_name, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 422, response.text
    assert "login_name" in response.text
    op_store.create_service_account.assert_not_awaited()
    # Refused before the administrator was asked to confirm anything.
    op_store.get_user_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_service_account_normalizes_a_given_login_name(client):
    http, op_store, _log_action = client
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "Bot", "login_name": "  CI.Runner_1 ", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text
    assert response.json()["login_name"] == "ci.runner_1"
    assert op_store.create_service_account.await_args.kwargs["login_name"] == "ci.runner_1"


@pytest.mark.asyncio
async def test_create_service_account_pads_a_short_derived_login_name(client):
    http, _op_store, _log_action = client
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "CI", "login_name": "", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text
    assert response.json()["login_name"] == "ci-svc"


@pytest.mark.asyncio
async def test_create_service_account_name_without_ascii_needs_a_login_name(client):
    http, op_store, _log_action = client
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "监控机器人", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 422
    assert "enter a login name" in response.json()["detail"]
    op_store.create_service_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_service_account_concurrent_duplicate_is_409(client):
    """The pre-check passed, but another create took the name first: 409, not 500."""
    http, op_store, log_action = client
    op_store.create_service_account.side_effect = LoginNameTakenError("bot")
    response = await http.post(
        "/admin/service-accounts",
        headers={"Authorization": "Bearer [REDACTED]"},
        json={"name": "Bot", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 409
    assert "already in use" in response.json()["detail"]
    log_action.assert_not_awaited()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Nightly Eval Bot", "nightly-eval-bot"),
        ("  --Uptime  Monitor!!  ", "uptime-monitor"),
        ("CI", "ci-svc"),
        ("x", "x-svc"),
        ("ops@example.com", "ops-example-com"),
        ("a" * 31 + " b", "a" * 31),
        ("Ünïcode Bot", "n-code-bot"),
        ("监控", None),
        ("!!!", None),
    ],
)
def test_derive_service_login_name(name, expected):
    assert derive_service_login_name(name) == expected
