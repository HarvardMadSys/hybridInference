"""``/auth/setup/*``: creating the first administrator with the setup code.

Mock-backed; the store's own transaction is covered in
``tests/unit/storage/test_first_run_setup_store.py`` and against PostgreSQL in
``tests/integration/storage/test_first_run_setup_postgres.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_db_logger, get_operational_store
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import auth_routes, setup as setup_routes
from serving.setup_state import (
    SETUP_CODE_KEY,
    SETUP_MARKER_KEY,
    init_setup_state,
    is_setup_required,
)
from serving.utils import password as password_utils
from serving.utils.jwt import get_jwt_secret
from serving.utils.login_rate_limit import SETUP_ATTEMPTS_PER_15MIN

SETUP_CODE = "ABCDEFGHJKLM"
STRONG_PASSWORD = "Str0ngPassword"


def _admin_row(user_id: str, login_name: str, user_name: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": None,
        "login_name": login_name,
        "user_name": user_name,
        "role": "admin",
        "status": "active",
        "email_verified": True,
        "created_at": datetime(2026, 10, 10, tzinfo=timezone.utc),
        "last_login_at": None,
        "password_hash": "unused",
        "preferences": {},
    }


@pytest.fixture
def op_store() -> MagicMock:
    store = MagicMock()
    # Setup pending, with SETUP_CODE stored in the database.
    store.get_or_create_setup_code = AsyncMock(return_value=SETUP_CODE)
    created: dict[str, Any] = {}

    async def _create_first_admin(**kwargs: Any) -> bool:
        created.update(kwargs)
        return True

    async def _get_user_by_id(user_id: str) -> dict[str, Any] | None:
        if created.get("user_id") != user_id:
            return None
        return _admin_row(user_id, created["login_name"], created["user_name"])

    store.created = created
    store.create_first_admin = AsyncMock(side_effect=_create_first_admin)
    store.get_user_by_id = AsyncMock(side_effect=_get_user_by_id)
    store.update_user_last_login = AsyncMock()
    store.create_session = AsyncMock()
    return store


@pytest.fixture
def app(op_store: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(setup_routes.router)
    install_exception_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[get_db_logger] = lambda: None
    return app


@pytest.fixture
async def client(app: FastAPI):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def pending(op_store) -> None:
    """Boot with no users and no marker; the stored setup code is SETUP_CODE."""
    await init_setup_state(op_store)
    assert is_setup_required()


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "setup_code": "ABCD-EFGH-JKLM",
        "login_name": "admin",
        "password": STRONG_PASSWORD,
    }
    body.update(overrides)
    return body


class TestStatus:
    async def test_pending(self, client, pending, monkeypatch):
        monkeypatch.setenv("DB_ENABLED", "true")
        resp = await client.get("/auth/setup/status")
        assert resp.status_code == 200
        assert resp.json() == {"setup_required": True, "database_enabled": True}

    async def test_database_free(self, client, monkeypatch):
        monkeypatch.setenv("DB_ENABLED", "false")
        await init_setup_state(None)
        resp = await client.get("/auth/setup/status")
        assert resp.json() == {"setup_required": False, "database_enabled": False}

    async def test_complete(self, client, op_store):
        op_store.get_or_create_setup_code.return_value = None
        await init_setup_state(op_store)
        resp = await client.get("/auth/setup/status")
        assert resp.json()["setup_required"] is False


class TestCreateAdmin:
    async def test_creates_the_admin_and_signs_them_in(self, client, pending, op_store):
        resp = await client.post(
            "/auth/setup/admin",
            json=_body(setup_code="abcd efgh jklm", login_name=" Admin ", display_name=" Ops "),
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["login_name"] == "admin"
        assert body["user"]["email"] is None
        assert body["user"]["user_name"] == "Ops"
        assert body["user"]["role"] == "admin"
        assert body["user"]["is_admin"] is True
        assert "refresh_token=" in resp.headers["set-cookie"]

        claims = jwt.decode(body["access_token"], get_jwt_secret(), algorithms=["HS256"])
        user_id = op_store.created["user_id"]
        assert claims["sub"] == user_id
        assert claims["email"] == ""
        assert claims["role"] == "admin"

        created = op_store.created
        assert created["login_name"] == "admin"
        assert created["user_name"] == "Ops"
        assert created["marker_key"] == SETUP_MARKER_KEY
        assert created["code_key"] == SETUP_CODE_KEY
        assert password_utils.verify_password(STRONG_PASSWORD, created["password_hash"])
        op_store.update_user_last_login.assert_awaited_once_with(user_id)
        assert op_store.create_session.await_args.kwargs["user_id"] == user_id
        assert is_setup_required() is False

    async def test_display_name_defaults_to_login_name(self, client, pending, op_store):
        resp = await client.post("/auth/setup/admin", json=_body(login_name="root"))
        assert resp.status_code == 200
        assert op_store.created["user_name"] == "root"

    async def test_wrong_code_is_403(self, client, pending, op_store):
        resp = await client.post("/auth/setup/admin", json=_body(setup_code="AAAA-AAAA-AAAA"))

        assert resp.status_code == 403
        op_store.create_first_admin.assert_not_awaited()
        assert is_setup_required() is True

    async def test_already_complete_is_409(self, client, op_store):
        op_store.get_or_create_setup_code.return_value = None
        await init_setup_state(op_store)

        resp = await client.post("/auth/setup/admin", json=_body())

        assert resp.status_code == 409
        op_store.create_first_admin.assert_not_awaited()

    async def test_lost_race_is_409(self, client, pending, op_store):
        op_store.create_first_admin = AsyncMock(return_value=False)

        resp = await client.post("/auth/setup/admin", json=_body())

        assert resp.status_code == 409
        assert is_setup_required() is False

    async def test_second_attempt_after_success_is_409(self, client, pending):
        assert (await client.post("/auth/setup/admin", json=_body())).status_code == 200
        resp = await client.post("/auth/setup/admin", json=_body(login_name="other"))
        assert resp.status_code == 409

    async def test_an_ip_that_keeps_failing_gets_429(self, client, pending, op_store):
        for _ in range(SETUP_ATTEMPTS_PER_15MIN):
            resp = await client.post("/auth/setup/admin", json=_body(setup_code="WRONG"))
            assert resp.status_code == 403

        resp = await client.post("/auth/setup/admin", json=_body(setup_code="WRONGAGAIN"))

        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "900"
        op_store.create_first_admin.assert_not_awaited()

    async def test_junk_from_a_shared_address_cannot_lock_out_the_correct_code(
        self, client, pending, op_store
    ):
        """Behind the console's proxy a stranger and the operator share one IP."""
        for _ in range(3 * SETUP_ATTEMPTS_PER_15MIN):
            await client.post("/auth/setup/admin", json=_body(setup_code="JUNK"))

        resp = await client.post("/auth/setup/admin", json=_body())

        assert resp.status_code == 200, resp.text
        op_store.create_first_admin.assert_awaited_once()

    async def test_a_correct_code_is_not_counted(self, client, pending, op_store, monkeypatch):
        recorded: list[str] = []

        async def _record(ip: str) -> bool:
            recorded.append(ip)
            return True

        monkeypatch.setattr(setup_routes, "record_failed_setup_attempt", _record)
        op_store.create_first_admin = AsyncMock(return_value=False)  # lose the race: 409

        assert (await client.post("/auth/setup/admin", json=_body())).status_code == 409
        assert recorded == []

    async def test_no_database_is_503(self, app, client, pending):
        app.dependency_overrides[get_operational_store] = lambda: None
        resp = await client.post("/auth/setup/admin", json=_body())
        assert resp.status_code == 503

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("login_name", "ab"),
            ("login_name", "admin@example.com"),
            ("login_name", "-admin"),
            ("login_name", "the admin"),
            ("login_name", "a" * 33),
            ("password", "short1A"),
            ("password", "alllowercase1"),
            ("password", "NoDigitsHere"),
            ("display_name", "x"),
            ("display_name", "x" * 51),
        ],
    )
    async def test_invalid_input_is_422_on_its_field(self, client, pending, op_store, field, value):
        resp = await client.post("/auth/setup/admin", json=_body(**{field: value}))

        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"][-1] == field
        op_store.create_first_admin.assert_not_awaited()
        assert is_setup_required() is True


class TestSignupWhileSetupPending:
    async def test_signup_is_503(self, client, pending):
        resp = await client.post(
            "/auth/signup",
            json={
                "email": "stranger@example.com",
                "password": STRONG_PASSWORD,
                "user_name": "Stranger",
                "accepted_tos": True,
            },
        )

        assert resp.status_code == 503
        assert resp.json()["detail"] == "This deployment has not been set up yet."
