"""An access token stops working when its session ends, not only when it expires.

Signing out, a password change ("other devices have been signed out") and a
password reset end sessions; a token issued for one of them must be refused on
its next request, although it has not expired. Each browser here is a client
with its own cookie jar; the store's sessions are the in-memory fake the routes
write through.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.settings import settings
from serving.servers.deps import get_db_logger, get_operational_store
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import auth_routes, user_routes
from serving.storage.base import ResetRedemption
from serving.utils import password as password_utils
from serving.utils.jwt import create_access_token
from tests.fixtures import browser_sessions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

USER_ID = "u-owner"
EMAIL = "owner@example.com"
PASSWORD = "Str0ngPassword"
NEW_PASSWORD = "N3wStr0ngPassword"
ENDED = "This session has ended. Please login again."


@pytest.fixture
def op_store() -> MagicMock:
    store = MagicMock()
    account = {
        "id": USER_ID,
        "email": EMAIL,
        "login_name": None,
        "user_name": "Owner",
        "role": "free",
        "status": "active",
        "email_verified": True,
        "created_at": datetime(2026, 10, 10, tzinfo=UTC),
        "last_login_at": None,
        "password_hash": "stored",
        "preferences": {},
    }
    store.get_user_by_id = AsyncMock(
        side_effect=lambda user_id: dict(account) if user_id == USER_ID else None
    )
    store.get_user_by_email = AsyncMock(
        side_effect=lambda email: dict(account) if email.lower() == EMAIL else None
    )
    store.get_user_avatar = AsyncMock(return_value=None)
    store.record_login_event = AsyncMock()
    store.update_user_last_login = AsyncMock()
    store.update_user_fields = AsyncMock()
    store.delete_user_reset_tokens = AsyncMock()
    store.create_session = AsyncMock()
    fake = browser_sessions.install(store)

    def sessions_of(user_id: str) -> list[dict[str, Any]]:
        for call in store.create_session.await_args_list:
            fake._session(call.kwargs["session_id"])
        return [row for row in fake.sessions.values() if row["user_id"] == user_id]

    async def revoke_user_sessions(user_id: str, *, except_sid: str | None = None) -> int:
        revoked = [
            row for row in sessions_of(user_id) if not row["revoked"] and row["sid"] != except_sid
        ]
        for row in revoked:
            row["revoked"] = True
        return len(revoked)

    async def delete_user_sessions(user_id: str) -> None:
        # The fake rebuilds a forgotten row from the create_session calls, so a
        # deleted session is kept as an ended one: the same to a liveness check.
        for row in sessions_of(user_id):
            row["revoked"] = True

    async def redeem_reset_token(token: str, *, password_hash: str) -> ResetRedemption:
        # The emailed link is live; spending it ends the account's sessions.
        await delete_user_sessions(USER_ID)
        return ResetRedemption("reset", USER_ID)

    store.revoke_user_sessions = AsyncMock(side_effect=revoke_user_sessions)
    store.delete_user_sessions = AsyncMock(side_effect=delete_user_sessions)
    store.redeem_reset_token = AsyncMock(side_effect=redeem_reset_token)
    return store


@pytest.fixture
def app(op_store: MagicMock, monkeypatch) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    install_exception_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[get_db_logger] = lambda: None
    monkeypatch.setattr(
        password_utils, "verify_password", lambda plain, _hash: plain in (PASSWORD, NEW_PASSWORD)
    )
    monkeypatch.setattr(password_utils, "hash_password", lambda plain: f"hashed:{plain}")
    monkeypatch.setattr(settings, "signup_require_email_verification", False)
    monkeypatch.setattr(user_routes, "is_email_enabled", lambda: False)
    return app


@asynccontextmanager
async def _browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 123))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _sign_in(browser: AsyncClient) -> dict[str, str]:
    resp = await browser.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _me(browser: AsyncClient, token: dict[str, str]):
    return await browser.get("/user/me", headers=token)


async def test_a_token_stops_working_when_its_browser_signs_out(app):
    async with _browser(app) as browser:
        token = await _sign_in(browser)
        assert (await _me(browser, token)).status_code == 200

        assert (await browser.post("/auth/logout", headers=token)).status_code == 200

        refused = await _me(browser, token)
    assert (refused.status_code, refused.json()["detail"]) == (401, ENDED)


async def test_changing_the_password_ends_the_other_devices_tokens_only(app):
    async with _browser(app) as laptop, _browser(app) as phone:
        laptop_token = await _sign_in(laptop)
        phone_token = await _sign_in(phone)

        changed = await laptop.post(
            "/user/change-password",
            json={"old_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=laptop_token,
        )
        assert changed.status_code == 200, changed.text

        assert (await _me(laptop, laptop_token)).status_code == 200
        refused = await _me(phone, phone_token)
    assert (refused.status_code, refused.json()["detail"]) == (401, ENDED)


async def test_a_password_reset_ends_every_session(app, op_store):
    op_store.get_reset_token = AsyncMock(
        return_value={
            "user_id": USER_ID,
            "used_at": None,
            "expires_at": datetime.now(UTC) + timedelta(minutes=30),
        }
    )
    async with _browser(app) as browser:
        token = await _sign_in(browser)

        reset = await browser.post(
            "/auth/reset-password", json={"token": "emailed", "new_password": NEW_PASSWORD}
        )
        assert reset.status_code == 200, reset.text

        refused = await _me(browser, token)
    assert (refused.status_code, refused.json()["detail"]) == (401, ENDED)


async def test_a_token_naming_no_session_is_refused(app):
    token, _jti = create_access_token(user_id=USER_ID, email=EMAIL, session_id="sess_unknown")

    async with _browser(app) as browser:
        refused = await _me(browser, {"Authorization": f"Bearer {token}"})

    assert (refused.status_code, refused.json()["detail"]) == (401, ENDED)


async def test_a_token_without_a_sid_is_refused(app, monkeypatch):
    monkeypatch.setattr("serving.utils.jwt.verify_access_token", lambda _token: {"sub": USER_ID})

    async with _browser(app) as browser:
        refused = await _me(browser, {"Authorization": "Bearer forged-but-signed"})

    assert refused.status_code == 401


async def test_an_admin_token_whose_session_ended_is_refused():
    """The admin dependency checks too, and does not fall back to ADMIN_TOKEN."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from serving.servers.deps import verify_admin_access

    store = MagicMock()
    store.session_is_live = AsyncMock(return_value=False)
    store.get_user_by_id = AsyncMock(return_value={"id": "u-admin", "role": "admin"})
    token, _jti = create_access_token(
        user_id="u-admin", email="admin@example.com", session_id="sess_gone", role="admin"
    )
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})

    with pytest.raises(HTTPException) as exc:
        await verify_admin_access(request, f"Bearer {token}", store)

    assert (exc.value.status_code, exc.value.detail) == (401, ENDED)
    store.session_is_live.assert_awaited_once_with("u-admin", "sess_gone")
    store.get_user_by_id.assert_not_awaited()


async def test_signing_out_ends_the_session_a_stolen_cookie_keeps_refreshing(app, op_store):
    """The reported case: a copied cookie survived its owner's sign-out.

    Whoever copied the cookie refreshes first, which rotates it: the owner's
    copy no longer names the browser session, so signing out used to clear it
    and nothing else, and the copy kept refreshing tokens for the account.
    Signing out now ends the session the owner's token was issued for, which
    is the session the copy refreshes.
    """
    # A cookie naming no browser session is looked up as a legacy one, too.
    op_store.get_session_by_token_hash = AsyncMock(return_value=None)
    async with _browser(app) as owner, _browser(app) as thief:
        token = await _sign_in(owner)
        thief.cookies.update(owner.cookies)
        assert (await thief.post("/auth/refresh")).status_code == 200
        # The owner's cookie is stale now; the owner signs out all the same.
        assert (await owner.post("/auth/refresh")).status_code == 401
        assert (await owner.post("/auth/logout", headers=token)).status_code == 200

        refused = await thief.post("/auth/refresh")
    assert refused.status_code == 401


async def test_signing_out_without_a_cookie_ends_the_tokens_session(app):
    async with _browser(app) as browser, _browser(app) as api_client:
        token = await _sign_in(browser)

        assert (await api_client.post("/auth/logout", headers=token)).status_code == 200

        refused = await _me(browser, token)
        assert (refused.status_code, refused.json()["detail"]) == (401, ENDED)
        assert (await browser.post("/auth/refresh")).status_code == 401
