"""Device cookies through the routes: earned by signing in, honoured at the next sign-in.

The test client connects from loopback, which the backend resolves to
``"unknown"`` -- what it sees for every browser behind the console's proxy
without trusted-proxy settings. So unless a test says otherwise, every request
here shares one address: the attacker's and the administrator's alike.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.requests import Request

from serving.config.settings import settings
from serving.servers.deps import get_db_logger, get_operational_store
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import auth_routes, setup as setup_routes
from serving.setup_state import init_setup_state
from serving.utils import device_cookie, password as password_utils
from serving.utils.device_cookie import LIFETIME_SECONDS, device_cookie_name, recognized_device
from tests.fixtures import browser_sessions

ADMIN_ID = "u-admin"
PASSWORD = "Str0ngPassword"
SETUP_CODE = "ABCDEFGHJKLM"


def _account(user_id: str, login_name: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": None,
        "login_name": login_name,
        "user_name": login_name,
        "role": "admin",
        "status": "active",
        "email_verified": True,
        "created_at": datetime(2026, 10, 10, tzinfo=UTC),
        "last_login_at": None,
        "password_hash": "unused",
        "preferences": {},
    }


@pytest.fixture
def op_store() -> MagicMock:
    store = MagicMock()
    users: dict[str, dict[str, Any]] = {}

    async def _create_first_admin(**kwargs: Any) -> bool:
        users[kwargs["user_id"]] = _account(kwargs["user_id"], kwargs["login_name"])
        return True

    async def _by_id(user_id: str) -> dict[str, Any] | None:
        return users.get(user_id)

    async def _by_login_name(login_name: str) -> dict[str, Any] | None:
        return next((row for row in users.values() if row["login_name"] == login_name), None)

    store.users = users
    store.get_or_create_setup_code = AsyncMock(return_value=SETUP_CODE)
    store.create_first_admin = AsyncMock(side_effect=_create_first_admin)
    store.get_user_by_id = AsyncMock(side_effect=_by_id)
    store.get_user_by_login_name = AsyncMock(side_effect=_by_login_name)
    store.get_user_by_email = AsyncMock(return_value=None)
    store.record_login_event = AsyncMock()
    store.update_user_last_login = AsyncMock()
    store.update_user_fields = AsyncMock()
    store.create_session = AsyncMock()
    browser_sessions.install(store)
    return store


@pytest.fixture
def app(op_store: MagicMock, monkeypatch) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(setup_routes.router)
    install_exception_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[get_db_logger] = lambda: None
    monkeypatch.setattr(password_utils, "verify_password", lambda plain, _hash: plain == PASSWORD)
    for name, value in (
        ("login_rate_limit_per_15min", 5),
        ("login_account_rate_limit_per_15min", 50),
        ("login_rate_limit_per_hour_per_ip", 20),
        ("signup_require_email_verification", False),
        ("refresh_token_cookie_name", "refresh_token"),
        ("cookie_secure", True),
        ("cookie_samesite", "lax"),
    ):
        monkeypatch.setattr(settings, name, value)
    return app


async def _send(
    app: FastAPI,
    method: str,
    url: str,
    *,
    client_ip: str = "127.0.0.1",
    cookies: dict[str, str] | None = None,
    **kwargs: Any,
) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(client_ip, 123))
    async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
        return await client.request(method, url, **kwargs)


async def _login(
    app: FastAPI,
    password: str = PASSWORD,
    *,
    login_name: str = "admin",
    device: str | None = None,
    client_ip: str = "127.0.0.1",
) -> Response:
    return await _send(
        app,
        "POST",
        "/auth/login",
        client_ip=client_ip,
        cookies={device_cookie_name(): device} if device else None,
        json={"email": login_name, "password": password},
    )


def _set_cookie(resp: Response, name: str) -> str | None:
    return next((h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{name}=")), None)


def _value(header: str | None) -> str | None:
    return header.split(";", 1)[0].split("=", 1)[1] if header else None


def _device(resp: Response) -> str | None:
    return _value(_set_cookie(resp, device_cookie_name()))


def _recognized(value: str | None, identifier: str) -> bool:
    cookie = f"{device_cookie_name()}={value}".encode()
    request = Request({"type": "http", "headers": [(b"cookie", cookie)]})
    return recognized_device(request, identifier) is not None


def _tampered(value: str) -> str:
    raw = bytearray(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    raw[-1] ^= 1
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()


async def test_sign_in_sets_a_device_cookie_for_the_account(app, op_store):
    op_store.users[ADMIN_ID] = _account(ADMIN_ID, "admin")

    resp = await _login(app)

    assert resp.status_code == 200, resp.text
    header = _set_cookie(resp, "refresh_token_device")
    assert header is not None
    attributes = {part.strip().lower() for part in header.split(";")[1:]}
    assert {"httponly", "secure", "path=/auth", "samesite=lax"} <= attributes
    assert f"max-age={LIFETIME_SECONDS}" in attributes
    assert _recognized(_value(header), "admin")


async def test_setup_sign_in_sets_a_device_cookie(app, op_store):
    await init_setup_state(op_store)

    resp = await _send(
        app,
        "POST",
        "/auth/setup/admin",
        json={"setup_code": SETUP_CODE, "login_name": "admin", "password": PASSWORD},
    )

    assert resp.status_code == 200, resp.text
    assert _recognized(_device(resp), "admin")


async def test_a_recognized_browser_signs_in_while_its_shared_address_is_locked_out(app, op_store):
    op_store.users[ADMIN_ID] = _account(ADMIN_ID, "admin")
    device = _device(await _login(app))  # the administrator, earlier, on this browser
    assert device

    # A stranger behind the same proxy guesses until the address windows are
    # spent, and from ten addresses of their own, the account-wide one.
    for _ in range(25):
        assert (await _login(app, "guess")).status_code in (401, 429)
    for n in range(10):
        for _ in range(5):
            await _login(app, "guess", client_ip=f"1.2.3.{n + 1}")

    # Without the cookie the right password is refused here and anywhere else.
    shared = await _login(app)
    assert (shared.status_code, shared.headers["Retry-After"]) == (429, "3600")
    elsewhere = await _login(app, client_ip="5.6.7.8")
    assert (elsewhere.status_code, elsewhere.headers["Retry-After"]) == (429, "900")

    # With it, the administrator's browser signs in, and its entry is rotated.
    resp = await _login(app, device=device)
    assert resp.status_code == 200, resp.text
    rotated = _device(resp)
    assert rotated and rotated != device

    # The browser's own window still applies: five wrong passwords, then even
    # the right one waits.
    for _ in range(5):
        assert (await _login(app, "guess", device=rotated)).status_code == 401
    blocked = await _login(app, device=rotated)
    assert (blocked.status_code, blocked.headers["Retry-After"]) == (429, "900")


async def test_no_exemption_without_a_valid_entry_for_the_account(app, op_store, monkeypatch):
    op_store.users[ADMIN_ID] = _account(ADMIN_ID, "admin")
    op_store.users["u-ops"] = _account("u-ops", "ops")
    admin_device = _device(await _login(app))
    ops_device = _device(await _login(app, login_name="ops"))
    assert admin_device and ops_device
    for _ in range(25):
        await _login(app, "guess")

    for value in (_tampered(admin_device), ops_device, "not-an-entry"):
        resp = await _login(app, device=value)
        assert (resp.status_code, resp.headers["Retry-After"]) == (429, "3600")

    issued_at = device_cookie._now()
    monkeypatch.setattr(device_cookie, "_now", lambda: issued_at + LIFETIME_SECONDS + 1)
    expired = await _login(app, device=admin_device)
    assert expired.status_code == 429

    monkeypatch.setattr(device_cookie, "_now", lambda: issued_at)
    assert (await _login(app, device=admin_device)).status_code == 200


async def test_a_failed_sign_in_on_a_recognized_browser_is_answered_like_any_other(app, op_store):
    op_store.users[ADMIN_ID] = _account(ADMIN_ID, "admin")
    device = _device(await _login(app))

    plain = await _login(app, "guess", client_ip="5.6.7.8")
    recognized = await _login(app, "guess", device=device)
    nobody = await _login(app, login_name="nobody", device=device)

    assert plain.status_code == recognized.status_code == nobody.status_code == 401
    assert recognized.json() == plain.json() == nobody.json()
    # Nothing is issued, rotated or cleared on a failure.
    assert _set_cookie(recognized, device_cookie_name()) is None
    assert _set_cookie(nobody, device_cookie_name()) is None


async def test_signing_out_keeps_the_device_cookie(app, op_store):
    op_store.users[ADMIN_ID] = _account(ADMIN_ID, "admin")
    signed_in = await _login(app)
    device = _device(signed_in)
    refresh = _value(_set_cookie(signed_in, "refresh_token"))
    assert device and refresh

    resp = await _send(
        app,
        "POST",
        "/auth/logout",
        cookies={"refresh_token": refresh, device_cookie_name(): device},
        headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"},
    )

    assert resp.status_code == 200, resp.text
    assert _set_cookie(resp, "refresh_token") is not None  # the session cookie goes
    assert _set_cookie(resp, device_cookie_name()) is None  # the device cookie stays
    assert _recognized(device, "admin")
