"""A saved API key is shown in full only to its owner's password, never in the listing.

``GET /user/api-keys/all`` used to decrypt every key for any console bearer
token: a token lifted from a browser, good for minutes, handed out keys good
until revoked. The listing is masked now; ``POST /user/api-keys/{prefix}/reveal``
shows one key after the password, counted against the sign-in limits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.settings import get_settings, settings
from serving.servers.auth import encrypt_api_key
from serving.servers.deps import get_current_user, get_db_logger, get_operational_store
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import user_routes
from serving.utils import password as password_utils

PASSWORD = "Str0ngPassword"
FULL_KEY = "hyi-testkey01-FULL-SECRET-VALUE"
PREFIX = FULL_KEY[:12]


def _key(prefix: str, status: str = "active", *, saved: str | None = FULL_KEY) -> dict[str, Any]:
    return {
        "id": 1,
        "key_prefix": prefix,
        "name": None,
        "created_at": datetime(2026, 10, 10, tzinfo=UTC),
        "last_used_at": None,
        "status": status,
        "api_key_encrypted": encrypt_api_key(saved) if saved else None,
        "budget_daily_usd": None,
        "budget_monthly_usd": None,
        "allowed_models": None,
        "spent_today_usd": 0,
        "spent_month_usd": 0,
    }


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key_secret", "unit-test-api-key-secret")
    for name, value in (
        ("login_rate_limit_per_15min", 5),
        ("login_account_rate_limit_per_15min", 50),
        ("login_rate_limit_per_hour_per_ip", 20),
    ):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(password_utils, "verify_password", lambda plain, _hash: plain == PASSWORD)


@pytest.fixture
def account() -> dict[str, Any]:
    return {"id": "u-owner", "email": "owner@example.com", "password_hash": "stored"}


@pytest.fixture
def op_store(account) -> MagicMock:
    store = MagicMock()
    store.get_user_by_id = AsyncMock(side_effect=lambda user_id: account)
    store.list_account_keys = AsyncMock(
        return_value=[
            _key(PREFIX),
            _key("hyi-revoked001", "revoked"),
            _key("hyi-legacy0001", saved=None),
        ]
    )
    return store


@pytest.fixture
def audit(monkeypatch) -> AsyncMock:
    logged = AsyncMock()
    monkeypatch.setattr(user_routes, "log_admin_action", logged)
    return logged


@pytest.fixture
def app(op_store, audit) -> FastAPI:
    app = FastAPI()
    app.include_router(user_routes.router)
    install_exception_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[get_db_logger] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "u-owner",
        "email": "owner@example.com",
        "role": "free",
    }
    return app


async def _call(app: FastAPI, method: str, path: str, **kwargs: Any):
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def _reveal(app: FastAPI, prefix: str = PREFIX, password: str | None = PASSWORD):
    body = {} if password is None else {"password": password}
    return await _call(app, "POST", f"/user/api-keys/{prefix}/reveal", json=body)


async def test_the_listing_never_carries_a_full_key(app, monkeypatch):
    def refuse(_ciphertext):
        raise AssertionError("the listing decrypted a key")

    monkeypatch.setattr(user_routes, "decrypt_api_key", refuse)

    resp = await _call(app, "GET", "/user/api-keys/all")

    assert resp.status_code == 200
    keys = {item["key_prefix"]: item for item in resp.json()["keys"]}
    assert FULL_KEY not in resp.text
    assert all(item["api_key"] is None for item in keys.values())
    assert {prefix: item["revealable"] for prefix, item in keys.items()} == {
        PREFIX: True,
        "hyi-revoked001": True,
        "hyi-legacy0001": False,
    }
    assert keys[PREFIX]["key_masked"].startswith(PREFIX)


async def test_the_password_reveals_one_saved_key(app, audit):
    resp = await _reveal(app)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key_prefix": PREFIX, "api_key": FULL_KEY}
    assert resp.headers["cache-control"] == "no-store"
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "reveal_key"
    assert audit.await_args.kwargs["details"] == {"actor": "user", "key_prefix": PREFIX}


async def test_a_revoked_key_can_still_be_shown(app):
    resp = await _reveal(app, "hyi-revoked001")

    assert resp.status_code == 200
    assert resp.json()["api_key"] == FULL_KEY


@pytest.mark.parametrize("password", [None, "", "Wrong-Password-1"])
async def test_no_key_without_the_password(app, audit, password):
    resp = await _reveal(app, password=password)

    assert resp.status_code == 400
    assert FULL_KEY not in resp.text
    audit.assert_not_awaited()


async def test_guessing_the_password_here_counts_against_the_sign_in_limits(app):
    for _ in range(5):
        assert (await _reveal(app, password="Wrong-Password-1")).status_code == 400

    limited = await _reveal(app)

    assert limited.status_code == 429
    assert FULL_KEY not in limited.text


@pytest.mark.parametrize("prefix", ["hyi-legacy0001", "hyi-unknown01"])
async def test_a_key_without_a_saved_value_is_not_found(app, prefix, monkeypatch):
    confirm = AsyncMock()
    monkeypatch.setattr(user_routes, "confirm_account_owner", confirm)

    resp = await _reveal(app, prefix)

    assert resp.status_code == 404
    # Nothing to show, so no password attempt is spent.
    confirm.assert_not_awaited()


async def test_an_account_without_a_password_is_told_how_to_get_a_key(app, account, monkeypatch):
    account["password_hash"] = None
    confirm = AsyncMock()
    monkeypatch.setattr(user_routes, "confirm_account_owner", confirm)

    resp = await _reveal(app)

    assert resp.status_code == 400
    assert "regenerate" in resp.json()["detail"]
    confirm.assert_not_awaited()


async def test_a_saved_value_that_cannot_be_read_is_a_conflict(app, op_store):
    unreadable = _key(PREFIX)
    unreadable["api_key_encrypted"] = "not-a-fernet-token"
    op_store.list_account_keys.return_value = [unreadable]

    resp = await _reveal(app)

    assert resp.status_code == 409
