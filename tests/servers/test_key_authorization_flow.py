"""API key authorization end to end, against PostgreSQL: sign in, approve, exchange, use.

Run with: make test-db-local
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import hash_api_key
from serving.utils.key_authorization import ENV_CALLBACKS
from serving.utils.key_exchange_rate_limit import reset_key_exchange_rate_limit_state
from tests.fixtures.auth_helpers import login_and_get_auth_headers, seed_test_user

pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest

CALLBACK = "https://app.example.org/model-connections/callback"
VERIFIER = "flow-verifier-" + "x" * 50


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


REQUEST = {
    "callback_url": CALLBACK + "?state=st-42",
    "code_challenge": _challenge(VERIFIER),
    "code_challenge_method": "S256",
}


@pytest_asyncio.fixture
async def flow_client(auth_app_services, clean_auth_tables, monkeypatch):
    """The console's three surfaces on one app: sign-in, the user API and key authorization."""
    from serving.servers.middleware.error import install_error_handlers
    from serving.servers.middleware.exception_handler import install_exception_handlers
    from serving.servers.routers import auth_routes, key_authorization, user_routes

    monkeypatch.setenv(ENV_CALLBACKS, CALLBACK)
    reset_key_exchange_rate_limit_state()
    app = FastAPI()
    app.state.services = auth_app_services
    install_error_handlers(app)
    install_exception_handlers(app)
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    app.include_router(key_authorization.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    reset_key_exchange_rate_limit_state()


@pytest_asyncio.fixture
async def signed_in(auth_backend, flow_client):
    """A verified account and its session headers."""
    operational_store, _, _, _ = auth_backend
    user = await seed_test_user(operational_store)
    headers = await login_and_get_auth_headers(
        flow_client, email=user["email"], password=user["password"]
    )
    return user, headers


async def _approve(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post("/v1/auth/keys/code", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    redirect = urlsplit(response.json()["redirect_url"])
    query = parse_qs(redirect.query)
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == CALLBACK
    assert query["state"] == ["st-42"]
    return query["code"][0]


@pytest.mark.asyncio
async def test_an_approved_application_gets_a_working_key_on_the_users_account(
    flow_client: AsyncClient, signed_in, auth_backend, auth_db_logger
):
    user, headers = signed_in
    operational_store, _, _, _ = auth_backend

    check = await flow_client.post("/v1/auth/keys/check", json=REQUEST)
    assert check.status_code == 200
    assert check.json()["app"] == "app.example.org"

    code = await _approve(flow_client, headers)
    exchange = await flow_client.post(
        "/v1/auth/keys",
        json={"code": code, "code_verifier": VERIFIER, "code_challenge_method": "S256"},
    )
    assert exchange.status_code == 200, exchange.text
    key = exchange.json()["key"]
    assert exchange.json()["user_id"] == user["id"]

    # The key authenticates as the account's own.
    context = await operational_store.get_auth_context_by_key_hash(hash_api_key(key))
    assert context is not None
    assert context["user_id"] == user["id"]

    # It is an ordinary key of the account: listed, named for the application,
    # in the Default project, and revealable by its owner.
    listed = (await flow_client.get("/user/api-keys/all", headers=headers)).json()["keys"]
    (row,) = [item for item in listed if item["key_prefix"] == key[:12]]
    assert row["name"] == "app.example.org"
    assert row["status"] == "active"
    assert row["revealable"] is True
    assert row["project_name"] == "Default"
    revealed = await flow_client.post(f"/user/api-keys/{key[:12]}/reveal", headers=headers)
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["api_key"] == key

    async with auth_db_logger.pool.acquire() as conn:
        stored = await conn.fetchrow(
            "SELECT notes, metadata FROM api_keys WHERE key_prefix = $1", key[:12]
        )
        audit = await conn.fetchrow(
            "SELECT details, actor_type, actor_id, target_id FROM admin_audit_log "
            "WHERE target_user_id = $1 AND action = 'create_key' "
            "ORDER BY timestamp DESC LIMIT 1",
            user["id"],
        )
    metadata = stored["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert stored["notes"] == "Created by API key authorization for app.example.org"
    assert metadata == {"created_by": "api_key_authorization", "callback_host": "app.example.org"}
    details = audit["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["actor"] == "api_key_authorization"
    assert details["callback_host"] == "app.example.org"
    assert key not in json.dumps(details)
    assert (audit["actor_type"], audit["actor_id"], audit["target_id"]) == (
        "user",
        user["id"],
        user["id"],
    )

    # The code is spent.
    replay = await flow_client.post("/v1/auth/keys", json={"code": code, "code_verifier": VERIFIER})
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_concurrent_exchanges_of_one_code_create_one_key(
    flow_client: AsyncClient, signed_in, auth_db_logger
):
    user, headers = signed_in
    code = await _approve(flow_client, headers)

    responses = await asyncio.gather(
        *(
            flow_client.post("/v1/auth/keys", json={"code": code, "code_verifier": VERIFIER})
            for _ in range(10)
        )
    )

    assert sorted(response.status_code for response in responses) == [200] + [400] * 9
    async with auth_db_logger.pool.acquire() as conn:
        keys = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = $1 AND name = 'app.example.org'",
            user["id"],
        )
    assert keys == 1


@pytest.mark.asyncio
async def test_an_unverified_account_must_verify_before_approving(
    flow_client: AsyncClient, auth_backend
):
    """Verification off for sign-in still leaves an unproven address unable to hand out a key."""
    operational_store, _, _, _ = auth_backend
    user = await seed_test_user(operational_store, email_verified=False)
    headers = await login_and_get_auth_headers(
        flow_client, email=user["email"], password=user["password"]
    )

    response = await flow_client.post("/v1/auth/keys/code", headers=headers, json=REQUEST)

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "email_not_verified"


@pytest.mark.asyncio
async def test_an_account_at_its_key_ceiling_cannot_approve(
    flow_client: AsyncClient, signed_in, monkeypatch
):
    from serving.servers.routers import user_routes

    _, headers = signed_in
    monkeypatch.setattr(user_routes, "MAX_ACTIVE_API_KEYS_PER_ACCOUNT", 1)
    assert (await flow_client.post("/user/api-keys", headers=headers)).status_code == 201

    response = await flow_client.post("/v1/auth/keys/code", headers=headers, json=REQUEST)

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "key_limit_reached"


@pytest.mark.asyncio
async def test_a_suspension_after_approval_stops_the_exchange(
    flow_client: AsyncClient, signed_in, auth_backend
):
    user, headers = signed_in
    operational_store, _, _, _ = auth_backend
    code = await _approve(flow_client, headers)
    await operational_store.update_user_fields(user["id"], status="suspended")

    response = await flow_client.post(
        "/v1/auth/keys", json={"code": code, "code_verifier": VERIFIER}
    )

    assert response.status_code == 403
    assert response.json()["error"] == "access_denied"
