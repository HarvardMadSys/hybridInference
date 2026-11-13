"""Agent invitation redemption over HTTP, against the real store.

Seeds tokens directly (issuing is the agent service's half, covered there)
and walks the public redemption endpoint: password set here, account and
organization settled, one scoped key issued, browser signed in. The scoped
key then authenticates like any other key — its scope is what the auth
context carries.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import agent_invitations, auth_routes
from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password

pytest_plugins = ["tests.servers.conftest_auth"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PASSWORD = "Invited-password-42"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest_asyncio.fixture
async def invite_client(auth_app_services):
    app = FastAPI(title="Agent Invite Test API")
    app.state.services = auth_app_services
    install_exception_handlers(app)
    app.include_router(auth_routes.router)
    app.include_router(agent_invitations.router)
    app.include_router(agent_invitations.internal_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _seed_token(store, token: str, *, email: str, agent_id: str = "agent-http-1") -> None:
    await store.create_agent_invite_token(
        token_hash=_hash(token),
        agent_account_id=agent_id,
        email=email,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


async def test_redeem_creates_account_signs_in_and_returns_scoped_key(
    invite_client, auth_app_services, clean_auth_tables
):
    store = auth_app_services.operational_store
    await _seed_token(store, "http-token-1", email="invited@anything-domain.io")

    response = await invite_client.post(
        "/v1/agent-invites/redeem",
        json={"token": "http-token-1", "password": PASSWORD, "accepted_tos": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_created"] is True
    assert body["api_key"].startswith("hyi-")
    assert body["access_token"]
    user = await store.get_user_by_email("invited@anything-domain.io")
    assert user is not None and user["status"] == "active" and user["email_verified"] is True

    # The new password signs in through the ordinary login, and the key the
    # redemption issued resolves with the agent scope on its auth context.
    login = await invite_client.post(
        "/auth/login",
        json={"email": "invited@anything-domain.io", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    from serving.servers.auth import hash_api_key

    ctx = await store.get_auth_context_lightweight(hash_api_key(body["api_key"]))
    assert ctx is not None and ctx["credential_scope"] == "agent"


async def test_redeem_twice_answers_gone_the_second_time(
    invite_client, auth_app_services, clean_auth_tables
):
    store = auth_app_services.operational_store
    await _seed_token(
        store, "http-token-2", email="twice@anything-domain.io", agent_id="agent-http-2"
    )
    payload = {"token": "http-token-2", "password": PASSWORD, "accepted_tos": True}

    assert (await invite_client.post("/v1/agent-invites/redeem", json=payload)).status_code == 200
    assert (await invite_client.post("/v1/agent-invites/redeem", json=payload)).status_code == 410


async def test_terms_and_weak_password_leave_the_token_usable(
    invite_client, auth_app_services, clean_auth_tables
):
    store = auth_app_services.operational_store
    await _seed_token(
        store, "http-token-3", email="terms@anything-domain.io", agent_id="agent-http-3"
    )

    no_terms = await invite_client.post(
        "/v1/agent-invites/redeem",
        json={"token": "http-token-3", "password": PASSWORD, "accepted_tos": False},
    )
    assert no_terms.status_code == 400
    weak = await invite_client.post(
        "/v1/agent-invites/redeem",
        json={"token": "http-token-3", "password": "short", "accepted_tos": True},
    )
    assert weak.status_code in (400, 422)
    ok = await invite_client.post(
        "/v1/agent-invites/redeem",
        json={"token": "http-token-3", "password": PASSWORD, "accepted_tos": True},
    )
    assert ok.status_code == 200, ok.text


async def test_unknown_token_is_gone_not_an_oracle(invite_client, clean_auth_tables):
    response = await invite_client.post(
        "/v1/agent-invites/redeem",
        json={"token": "never-minted", "password": PASSWORD, "accepted_tos": True},
    )
    assert response.status_code == 410


async def test_existing_member_sets_a_new_password(
    invite_client, auth_app_services, clean_auth_tables
):
    store = auth_app_services.operational_store
    user_id = generate_ulid()
    await store.create_user(
        user_id=user_id,
        email="member@anything-domain.io",
        password_hash=hash_password("previous-password-9"),
        user_name="Member",
        email_verified=True,
        status="active",
    )
    await _seed_token(
        store, "http-token-4", email="member@anything-domain.io", agent_id="agent-http-4"
    )

    response = await invite_client.post(
        "/v1/agent-invites/redeem",
        json={"token": "http-token-4", "password": PASSWORD, "accepted_tos": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["account_created"] is False
    old_login = await invite_client.post(
        "/auth/login",
        json={"email": "member@anything-domain.io", "password": "previous-password-9"},
    )
    assert old_login.status_code == 401
    new_login = await invite_client.post(
        "/auth/login",
        json={"email": "member@anything-domain.io", "password": PASSWORD},
    )
    assert new_login.status_code == 200
