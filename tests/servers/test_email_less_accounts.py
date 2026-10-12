"""The first-run setup administrator has a login name and no email address.

Sign-in by login name, the empty JWT ``email`` claim, ``/user/me`` and the
admin endpoints that used to assume every account has an email.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import admin as admin_router, auth_routes, user_routes
from serving.servers.routers.auth_routes import hash_refresh_token
from serving.utils import password as password_utils
from serving.utils.jwt import create_access_token, get_jwt_secret

ADMIN_ID = "u-admin"
OTHER_ID = "u-other"
PASSWORD = "Str0ngPassword"
_CREATED = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _row(user_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": user_id,
        "email": f"{user_id}@example.com",
        "login_name": None,
        "user_name": user_id,
        "role": "free",
        "status": "active",
        "email_verified": True,
        "created_at": _CREATED,
        "last_login_at": None,
        "password_hash": "unused",
        "preferences": {},
        "max_concurrent_requests": None,
        "admin_note": None,
        "suspension_message": None,
        "signup_reason": None,
    }
    row.update(overrides)
    return row


@pytest.fixture(scope="module")
def admin_password_hash() -> str:
    return password_utils.hash_password(PASSWORD)


@pytest.fixture
def users(admin_password_hash: str) -> dict[str, dict[str, Any]]:
    return {
        ADMIN_ID: _row(
            ADMIN_ID,
            email=None,
            login_name="admin",
            user_name="Ops",
            role="admin",
            password_hash=admin_password_hash,
        ),
        OTHER_ID: _row(OTHER_ID, status="pending_approval"),
    }


@pytest.fixture
def op_store(users: dict[str, dict[str, Any]]) -> MagicMock:
    store = MagicMock()

    async def _by_id(user_id: str) -> dict[str, Any] | None:
        return users.get(user_id)

    async def _by_login_name(login_name: str) -> dict[str, Any] | None:
        return next((u for u in users.values() if u["login_name"] == login_name), None)

    async def _by_email(email: str) -> dict[str, Any] | None:
        return next((u for u in users.values() if u["email"] == email.lower()), None)

    store.get_user_by_id = AsyncMock(side_effect=_by_id)
    store.get_user_by_login_name = AsyncMock(side_effect=_by_login_name)
    store.get_user_by_email = AsyncMock(side_effect=_by_email)
    store.record_login_event = AsyncMock()
    store.update_user_last_login = AsyncMock()
    store.update_user_fields = AsyncMock()
    store.update_user_preferences = AsyncMock()
    store.create_session = AsyncMock()
    store.rotate_session = AsyncMock()
    store.get_active_key_by_account = AsyncMock(return_value=None)
    store.approve_user = AsyncMock()
    store.revoke_key = AsyncMock()
    store.log_admin_action = AsyncMock()
    store.add_signup_allowed_domain = AsyncMock(
        side_effect=lambda **kw: {
            "domain": kw["domain"],
            "is_wildcard": kw["is_wildcard"],
            "created_at": _CREATED,
            "created_by": kw["created_by"],
            "created_by_email": None,
        }
    )
    store.list_users = AsyncMock(
        return_value=(
            1,
            [{**users[ADMIN_ID], "key_prefix": None, "key_status": None}],
            {"all": 1, "active": 1},
        )
    )
    return store


@pytest.fixture
def db_logger() -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"email": None})
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    logger = MagicMock()
    logger.pool.acquire.return_value = acquire_cm
    logger.conn = conn
    return logger


@pytest.fixture
async def client(op_store: MagicMock, db_logger: MagicMock, monkeypatch):
    log_store = MagicMock()
    log_store.get_user_detail_usage = AsyncMock(return_value={})
    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(),
        db_logger=db_logger,
        operational_store=op_store,
        log_store=log_store,
        routing_manager=None,
    )
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    app.include_router(admin_router.router)
    install_exception_handlers(app)
    monkeypatch.setattr("serving.servers.routers.admin.users.log_admin_action", AsyncMock())
    monkeypatch.setattr(
        "serving.servers.routers.admin.signup_domains.log_admin_action", AsyncMock()
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _admin_headers() -> dict[str, str]:
    token, _jti = create_access_token(
        user_id=ADMIN_ID, email=None, session_id="sess", is_admin=True, role="admin"
    )
    return {"Authorization": f"Bearer {token}"}


class TestJwt:
    def test_email_claim_is_empty_without_an_email(self):
        token, _jti = create_access_token(user_id=ADMIN_ID, email=None, role="admin")
        claims = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        assert claims["email"] == ""
        assert claims["sub"] == ADMIN_ID

    def test_algorithm_and_lifetime_come_from_settings(self, monkeypatch):
        from serving.utils.jwt import get_access_token_expire_minutes, get_jwt_algorithm

        monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "7")
        monkeypatch.setenv("JWT_ALGORITHM", "HS512")
        assert get_access_token_expire_minutes() == 7
        assert get_jwt_algorithm() == "HS512"

    def test_defaults_are_preserved(self, monkeypatch):
        from serving.utils.jwt import get_access_token_expire_minutes, get_jwt_algorithm

        monkeypatch.delenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", raising=False)
        monkeypatch.delenv("JWT_ALGORITHM", raising=False)
        assert get_access_token_expire_minutes() == 15
        assert get_jwt_algorithm() == "HS256"


class TestLogin:
    async def test_login_by_login_name(self, client, op_store):
        resp = await client.post("/auth/login", json={"email": " Admin ", "password": PASSWORD})

        assert resp.status_code == 200, resp.text
        user = resp.json()["user"]
        assert user["email"] is None
        assert user["login_name"] == "admin"
        assert user["is_admin"] is True
        op_store.get_user_by_login_name.assert_awaited_once_with("admin")
        op_store.get_user_by_email.assert_not_awaited()
        assert op_store.record_login_event.await_args.kwargs["email"] == "admin"
        claims = jwt.decode(resp.json()["access_token"], get_jwt_secret(), algorithms=["HS256"])
        assert claims["email"] == ""

    async def test_unknown_login_name_is_401(self, client, op_store):
        resp = await client.post("/auth/login", json={"email": "nobody", "password": PASSWORD})
        assert resp.status_code == 401
        assert op_store.record_login_event.await_args.kwargs["failure_reason"] == "user_not_found"

    async def test_email_login_still_uses_email(self, client, op_store):
        await client.post("/auth/login", json={"email": "Someone@Example.com", "password": "x"})
        op_store.get_user_by_email.assert_awaited_once_with("Someone@example.com")
        op_store.get_user_by_login_name.assert_not_awaited()

    @pytest.mark.parametrize("identifier", ["not a name", "x", "bad@", "@example.com"])
    async def test_malformed_identifier_is_422(self, client, op_store, identifier):
        resp = await client.post("/auth/login", json={"email": identifier, "password": "x"})
        assert resp.status_code == 422
        op_store.get_user_by_login_name.assert_not_awaited()
        op_store.get_user_by_email.assert_not_awaited()

    async def test_login_rate_limit_is_keyed_by_login_name(self, client, monkeypatch):
        seen: list[str] = []

        async def _record(identifier: str, ip: str) -> tuple[bool, str | None]:
            seen.append(identifier)
            return True, None

        monkeypatch.setattr(auth_routes, "check_and_record_login", _record)
        await client.post("/auth/login", json={"email": "ADMIN", "password": PASSWORD})
        assert seen == ["admin"]

    async def test_refresh_works_without_an_email(self, client, op_store, monkeypatch):
        monkeypatch.setattr(auth_routes.settings, "admin_emails", "someone@example.com")
        op_store.get_session_by_token_hash = AsyncMock(
            return_value={
                "id": "s1",
                "user_id": ADMIN_ID,
                "sid": "sess",
                "revoked": False,
                "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
            }
        )

        resp = await client.post("/auth/refresh", cookies={"refresh_token": "tok"})

        assert resp.status_code == 200, resp.text
        op_store.get_session_by_token_hash.assert_awaited_once_with(hash_refresh_token("tok"))
        op_store.update_user_fields.assert_not_awaited()


class TestUserEndpoints:
    async def test_me(self, client):
        resp = await client.get("/user/me", headers=_admin_headers())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["email"] is None
        assert body["login_name"] == "admin"
        assert body["is_admin"] is True

    async def test_profile_update(self, client, op_store):
        resp = await client.patch(
            "/user/profile", headers=_admin_headers(), json={"user_name": "Operator"}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["login_name"] == "admin"
        op_store.update_user_fields.assert_awaited_once_with(ADMIN_ID, user_name="Operator")


class TestAdminEndpoints:
    async def test_list_users(self, client, op_store):
        resp = await client.get("/admin/users?search=adm", headers=_admin_headers())

        assert resp.status_code == 200, resp.text
        user = resp.json()["users"][0]
        assert user["email"] is None
        assert user["login_name"] == "admin"

    async def test_user_detail(self, client):
        resp = await client.get(f"/admin/users/{ADMIN_ID}/detail", headers=_admin_headers())

        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] is None
        assert resp.json()["login_name"] == "admin"

    async def test_cannot_demote_self(self, client, op_store):
        resp = await client.patch(
            f"/admin/users/{ADMIN_ID}", headers=_admin_headers(), json={"role": "free"}
        )

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Cannot demote your own admin role."
        op_store.update_user_fields.assert_not_awaited()

    async def test_can_change_another_users_role(self, client, op_store, users):
        users[OTHER_ID]["status"] = "active"
        resp = await client.patch(
            f"/admin/users/{OTHER_ID}", headers=_admin_headers(), json={"role": "pro"}
        )

        assert resp.status_code == 200, resp.text
        op_store.update_user_fields.assert_awaited_once_with(OTHER_ID, role="pro")

    async def test_approval_is_attributed_to_the_login_name(self, client, op_store):
        resp = await client.post(f"/admin/users/{OTHER_ID}/approve", headers=_admin_headers())

        assert resp.status_code == 200, resp.text
        assert op_store.approve_user.await_args.kwargs["admin_id"] == "admin"

    async def test_signup_domain_records_the_admin_account(self, client, op_store):
        resp = await client.post(
            "/admin/signup-domains", headers=_admin_headers(), json={"domain": "example.org"}
        )

        assert resp.status_code == 201, resp.text
        assert op_store.add_signup_allowed_domain.await_args.kwargs["created_by"] == ADMIN_ID

    async def test_broadcast_test_send_without_an_email(self, client, db_logger, monkeypatch):
        sent = MagicMock()
        monkeypatch.setattr("serving.servers.routers.admin.broadcast.send_email", sent)

        resp = await client.post(
            "/admin/broadcast-email/test",
            headers=_admin_headers(),
            json={
                "subject": "Hi",
                "body_html": "<p>Hi</p>",
                "body_text": "Hi",
                "target_roles": ["free"],
                "target_statuses": ["active"],
            },
        )

        assert resp.status_code == 400
        assert "no email address" in resp.json()["detail"]
        sent.assert_not_called()
        sql, user_id = db_logger.conn.fetchrow.await_args.args
        assert "WHERE id = $1" in sql
        assert user_id == ADMIN_ID


def test_send_email_skips_a_missing_address(monkeypatch):
    from serving.utils import email as email_utils

    smtp = MagicMock()
    monkeypatch.setattr(email_utils.smtplib, "SMTP", smtp)
    monkeypatch.setattr(email_utils, "is_email_enabled", lambda: True)

    assert email_utils.send_email(None, "s", "<p>b</p>") is False
    assert email_utils.send_email("  ", "s", "<p>b</p>") is False
    smtp.assert_not_called()
