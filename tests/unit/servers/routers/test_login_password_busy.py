"""Sign-in refuses at once when the password checks queue is full.

Every sign-in runs Argon2id, for a name nobody holds as well, and the checks
queued without a bound: a flood of guesses from many addresses kept every
sign-in waiting until it timed out. Past the bound an unrecognized browser's
sign-in is answered 503 before the attempt is counted or the name looked up;
a browser the account recognizes is still served.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from serving.schemas_auth import LoginRequest
from serving.servers.routers import auth_routes
from serving.utils import login_rate_limit, password as password_utils
from serving.utils.password import PasswordWorkBusyError

pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [],
            "client": ("198.51.100.7", 4000),
        }
    )


@pytest.fixture
def store() -> MagicMock:
    store = MagicMock()
    store.get_user_by_email = AsyncMock(return_value=None)
    store.get_user_by_login_name = AsyncMock(return_value=None)
    store.record_login_event = AsyncMock()
    return store


@pytest.fixture(autouse=True)
def _fresh_limiter():
    login_rate_limit.reset_login_rate_limit_state()
    yield
    login_rate_limit.reset_login_rate_limit_state()


async def _login(store: Any, **body: str):
    return await auth_routes.login(
        request=_request(),
        response=Response(),
        body=LoginRequest(**body),
        op_store=store,
    )


async def test_a_full_queue_refuses_before_counting_or_looking_up(monkeypatch, store):
    monkeypatch.setattr(password_utils, "HASH_MAX_WAITERS", 0)

    with pytest.raises(PasswordWorkBusyError):
        await _login(store, email="alice@example.com", password="Guess-1234")

    store.get_user_by_email.assert_not_awaited()
    store.record_login_event.assert_not_awaited()
    # The attempt used none of the address's or the account's allowance.
    assert not login_rate_limit._account_address_attempts
    assert not login_rate_limit._ip_attempts
    assert not login_rate_limit._account_attempts


async def test_a_recognized_browser_is_not_refused_for_a_full_queue(monkeypatch, store):
    monkeypatch.setattr(password_utils, "HASH_MAX_WAITERS", 0)
    monkeypatch.setattr(auth_routes, "recognized_device", lambda request, identifier: "n" * 32)
    monkeypatch.setattr(password_utils, "verify_password", lambda plain, hashed: False)

    with pytest.raises(HTTPException) as refused:
        await _login(store, email="alice@example.com", password="Guess-1234")

    # Checked as usual: no such account here, so the ordinary 401.
    assert refused.value.status_code == 401
    store.get_user_by_email.assert_awaited_once()


async def test_the_refusal_is_a_503_with_retry_after():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from serving.servers.middleware.exception_handler import install_exception_handlers

    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/busy")
    async def busy():
        raise PasswordWorkBusyError("full")

    response = TestClient(app).post("/busy")

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(password_utils.PASSWORD_BUSY_RETRY_AFTER_SEC)
    assert "busy" in response.json()["detail"].lower()
