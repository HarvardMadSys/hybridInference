"""Who an audit row says acted, from where, on what (``serving.audit``).

These run the real admin authentication (``verify_admin_access``), the real
route, the real ``log_admin_action`` helper and the real store writer; only the
connection is recorded (``AuditRowRecorder``), so each assertion is about the
row the request would store. The client address resolver is replaced with one
that reads a test header: which address is trusted is
``serving.utils.request_ip``'s business, and what matters here is that its
answer, and nothing else, becomes the row's source.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.config.model_concurrency import ModelConcurrencyResolver
from serving.config.settings import get_settings
from serving.servers import deps
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router
from serving.utils.jwt import create_access_token
from tests.fixtures.audit import AuditRowRecorder

ADMIN_TOKEN = "unit-admin-token"
MODEL = "public-model"


def _adapter(model_id: str) -> MagicMock:
    adapter = MagicMock()
    adapter.config.id = model_id
    adapter.config.name = model_id
    adapter.config.provider = "test"
    return adapter


def _admin_row(user_id: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": f"{user_id}@example.test",
        "login_name": None,
        "role": "admin",
        "status": "active",
        "email_verified": True,
        "phone": None,
        "phone_verified": False,
    }


def _bearer(user_id: str) -> dict[str, str]:
    token, _jti = create_access_token(user_id, f"{user_id}@example.test", role="admin")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-jwt-secret-at-least-32-characters")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client_ip(monkeypatch):
    """Resolve each request's client address from an ``X-Test-Client`` header."""

    def _resolve(request) -> str:
        return request.headers.get("x-test-client", "unknown")

    monkeypatch.setattr(deps, "get_client_ip", _resolve)


@pytest.fixture
async def concurrency_app(auth_env):
    recorder = AuditRowRecorder()
    op_store = MagicMock()
    admins = {"u-alice": _admin_row("u-alice"), "u-bob": _admin_row("u-bob")}
    op_store.get_user_by_id = AsyncMock(side_effect=lambda user_id: admins.get(user_id))
    op_store.get_model_concurrency_exemption = AsyncMock(return_value=None)
    op_store.set_model_concurrency_exemption = AsyncMock()
    op_store.delete_model_concurrency_exemption = AsyncMock()
    op_store.log_admin_action = recorder.log_admin_action

    router = FixedRouter()
    router.register_route(MODEL, [(_adapter(MODEL), 1.0)], required_role="free")
    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        operational_store=op_store,
        model_concurrency_resolver=ModelConcurrencyResolver(op_store),
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, op_store, recorder, app


async def _set_exempt(client, exempt: bool, headers: dict[str, str]):
    return await client.patch(
        f"/admin/models/{MODEL}/concurrency", json={"exempt": exempt}, headers=headers
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("exempt", [True, False], ids=["enable", "disable"])
async def test_model_exemption_change_records_the_jwt_admin_and_the_model(
    concurrency_app, client_ip, exempt
):
    """The original bug: this row had a model but no actor, and the address as its 'source'."""
    client, _op_store, recorder, _app = concurrency_app

    response = await _set_exempt(
        client, exempt, {**_bearer("u-alice"), "X-Test-Client": "192.0.2.44"}
    )

    assert response.status_code == 200, response.text
    [row] = recorder.rows
    assert row["action"] == "models.concurrency.update"
    assert row["details"]["new_exempt"] is exempt
    # The account's stable id, never its email.
    assert (row["actor_type"], row["actor_id"]) == ("admin", "u-alice")
    assert row["source_ip"] == "192.0.2.44"
    assert row["admin_ip"] == "192.0.2.44"
    assert (row["target_type"], row["target_id"]) == ("model", MODEL)
    # A model is not an affected user: the user filter and erasure never reach it.
    assert row["target_user_id"] is None


@pytest.mark.asyncio
async def test_shared_token_is_recorded_as_itself_without_an_account(concurrency_app, client_ip):
    client, _op_store, recorder, _app = concurrency_app

    response = await _set_exempt(
        client,
        True,
        {"Authorization": f"Bearer {ADMIN_TOKEN}", "X-Test-Client": "192.0.2.45"},
    )

    assert response.status_code == 200, response.text
    [row] = recorder.rows
    assert (row["actor_type"], row["actor_id"]) == ("admin_token", None)
    assert row["source_ip"] == "192.0.2.45"


@pytest.mark.asyncio
async def test_an_unresolved_source_is_null_not_a_stand_in(concurrency_app):
    """The real resolver cannot trust the test transport's loopback peer."""
    client, _op_store, recorder, _app = concurrency_app

    response = await _set_exempt(client, True, _bearer("u-alice"))

    assert response.status_code == 200, response.text
    [row] = recorder.rows
    assert row["source_ip"] is None
    # The legacy column keeps its NOT NULL with the honest word, not an email.
    assert row["admin_ip"] == "unknown"
    assert row["actor_id"] == "u-alice"


@pytest.mark.asyncio
async def test_concurrent_requests_never_see_each_others_actor(concurrency_app, client_ip):
    """Both requests are inside authentication at once; each row names its own admin."""
    client, op_store, recorder, _app = concurrency_app
    admins = {"u-alice": _admin_row("u-alice"), "u-bob": _admin_row("u-bob")}
    arrived: list[str] = []
    both_in = asyncio.Event()

    async def _lookup(user_id: str):
        arrived.append(user_id)
        if len(arrived) == 2:
            both_in.set()
        # Hold each lookup until the other request is authenticating too, so
        # the two interleave rather than run one after the other.
        await asyncio.wait_for(both_in.wait(), timeout=5)
        return admins[user_id]

    op_store.get_user_by_id = AsyncMock(side_effect=_lookup)

    alice, bob = await asyncio.gather(
        _set_exempt(client, True, {**_bearer("u-alice"), "X-Test-Client": "192.0.2.1"}),
        _set_exempt(client, False, {**_bearer("u-bob"), "X-Test-Client": "192.0.2.2"}),
    )

    assert alice.status_code == bob.status_code == 200
    assert sorted(arrived) == ["u-alice", "u-bob"]
    by_actor = {row["actor_id"]: row for row in recorder.rows}
    assert set(by_actor) == {"u-alice", "u-bob"}
    assert by_actor["u-alice"]["source_ip"] == "192.0.2.1"
    assert by_actor["u-alice"]["details"]["new_exempt"] is True
    assert by_actor["u-bob"]["source_ip"] == "192.0.2.2"
    assert by_actor["u-bob"]["details"]["new_exempt"] is False


@pytest.mark.asyncio
async def test_an_audited_route_refuses_rather_than_invent_an_actor(concurrency_app):
    """Bypassing the recording dependency fails closed, before anything changes."""
    client, op_store, recorder, app = concurrency_app
    app.dependency_overrides[verify_admin_access] = lambda: "someone"
    try:
        response = await _set_exempt(client, True, {})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    op_store.set_model_concurrency_exemption.assert_not_awaited()
    assert recorder.rows == []


@pytest.mark.asyncio
async def test_verify_admin_access_still_returns_the_display_identity(concurrency_app, client_ip):
    """The dependency's return contract is unchanged; the actor is a companion."""
    client, op_store, _recorder, _app = concurrency_app

    await _set_exempt(client, True, {**_bearer("u-alice"), "X-Test-Client": "192.0.2.9"})

    # ``updated_by`` keeps receiving the display identity it always did.
    op_store.set_model_concurrency_exemption.assert_awaited_once_with(MODEL, "u-alice@example.test")
