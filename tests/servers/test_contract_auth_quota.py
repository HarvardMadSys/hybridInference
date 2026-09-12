"""Contract freeze: public API-key authentication and quota rejection."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving import quota
from serving.servers.auth import _quota_contact, verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers


@pytest.fixture
async def auth_contract_client(monkeypatch: pytest.MonkeyPatch):
    store = AsyncMock()
    store.get_auth_context_by_key_hash.return_value = {
        "id": 7,
        "user_id": "contract-user",
        "user_name": "Contract User",
        "role": "free",
        "email": "contract@example.test",
        "email_verified": True,
        "quota_daily_cost_usd": 10.0,
    }
    store.get_user_cost_today.return_value = 10.0

    monkeypatch.setattr("serving.servers.auth.is_user_auth_enabled", lambda: True)
    monkeypatch.setattr("serving.servers.auth.hash_api_key", lambda _key: "contract-key-hash")
    monkeypatch.setattr("serving.servers.auth.log_rejection", AsyncMock())

    app = FastAPI()
    install_error_handlers(app)
    app.state.services = AppServices(
        router=RouteExecutor(),
        operational_store=store,
        log_store=None,
    )

    @app.get("/contract-auth")
    async def contract_auth(user: dict = Depends(verify_api_key)):
        return user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_missing_api_key_preserves_401_error_envelope(auth_contract_client: AsyncClient):
    resp = await auth_contract_client.get("/contract-auth")

    assert resp.status_code == 401
    assert resp.json() == {
        "error": {
            "code": 401,
            "message": (
                "Missing API key. Use 'Authorization: Bearer hyi-xxx' or 'X-API-Key: hyi-xxx'"
            ),
            "model": None,
            "queue_size": None,
            "retry_after": None,
            "tokens_requested": None,
            "type": "unknown",
        }
    }


@pytest.mark.asyncio
async def test_exhausted_daily_quota_preserves_429_body_and_headers(
    auth_contract_client: AsyncClient,
):
    resp = await auth_contract_client.get(
        "/contract-auth",
        headers={"Authorization": "Bearer hyi-contract"},
    )

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "Daily cost quota exceeded"
    assert body["quota_usd"] == 10.0
    assert body["spent_usd"] == 10.0
    assert body["remaining_usd"] == 0
    assert body["reset_at"]
    assert int(resp.headers["retry-after"]) > 0
    assert resp.headers["x-ratelimit-limit-cost"] == "10.0"
    assert resp.headers["x-ratelimit-remaining-cost"] == "0"
    assert int(resp.headers["x-ratelimit-reset"]) > 0


@pytest.mark.asyncio
async def test_hand_rolled_quota_429_matches_the_shared_builder(
    auth_contract_client: AsyncClient,
):
    """The two quota doors must answer identically -- checked on the busy one.

    ``quota.exceeded_payload`` has exactly one caller: the agent-grant door in
    ``verify_api_key``. The API-key door a few lines below it hand-rolls the
    same body and headers inline, and that is the door that fires ~122k times a
    day. A "both doors agree" test written against ``exceeded_payload`` twice
    would pass while the hand-rolled copy drifted, so this one drives the real
    dependency over HTTP and compares what came back against the builder.

    It also pins what the redaction pass must never take out of this body: the
    caller's own quota, spend, remaining, and reset time. None of that is
    internal -- it is the client's own accounting, and the only way it can pace
    itself instead of hammering a gateway that is already refusing it.
    """
    resp = await auth_contract_client.get(
        "/contract-auth",
        headers={"Authorization": "Bearer hyi-contract"},
    )

    expected_body, expected_headers = quota.exceeded_payload(
        quota_usd=10.0, spent_usd=10.0, contact_email=_quota_contact()
    )
    body = resp.json()

    assert resp.status_code == 429
    assert set(body) == set(expected_body)
    for key, expected in expected_body.items():
        if key == "retry_after":
            # Both sides count seconds to the next UTC midnight from their own
            # "now"; a second of clock drift between them is not a divergence.
            assert abs(body[key] - expected) <= 2
        else:
            assert body[key] == expected, key

    for header, expected in expected_headers.items():
        if header == "Retry-After":
            assert abs(int(resp.headers[header]) - int(expected)) <= 2
        else:
            assert resp.headers[header] == expected, header
