"""Phone route policy, identity compatibility, and external-send failure boundaries."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException, Request, Response
from pydantic import ValidationError

from serving.schemas_auth import PhoneCodeRequest, PhoneSignupRequest, PhoneVerifyRequest
from serving.servers.deps import get_current_user
from serving.servers.routers import auth_routes, phone_auth_routes as routes, user_routes
from serving.utils.jwt import create_access_token, verify_access_token
from serving.utils.phone_auth import hash_phone_code

PHONE = "+8613800138000"


def request(authorization=None, cookie=None):
    headers = [(b"user-agent", b"phone-auth-test")]
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/auth/phone/code",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


def user(*, email=None, email_verified=False, status="active", phone_verified=True):
    return {
        "id": "existing-user",
        "email": email,
        "phone": PHONE,
        "phone_verified": phone_verified,
        "email_verified": email_verified,
        "password_hash": None,
        "user_name": "Phone User",
        "role": "free",
        "status": status,
        "created_at": datetime.now(UTC),
        "last_login_at": None,
    }


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(routes, "is_sms_enabled", lambda: True)
    monkeypatch.setattr(routes, "verify_turnstile_token", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "is_public_signup_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "allowlist_is_empty", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "check_and_record_signup", AsyncMock(return_value=(True, None)))
    from serving.config.settings import get_settings

    monkeypatch.setattr(
        get_settings(), "jwt_secret_key", "test-secret-for-phone-route-auth-long-enough"
    )
    monkeypatch.setattr(get_settings(), "signup_require_email_verification", True)
    monkeypatch.setattr(
        routes.settings, "jwt_secret_key", "test-secret-for-phone-route-auth-long-enough"
    )
    monkeypatch.setattr(routes.settings, "signup_require_email_verification", True)
    monkeypatch.setattr(routes.settings, "signup_admin_notify_enabled", False)
    import serving.config.runtime_settings as runtime

    monkeypatch.setattr(
        runtime,
        "get_runtime_settings_instance",
        lambda: (_ for _ in ()).throw(RuntimeError("not initialized")),
    )


class Store:
    def __init__(self, row=None):
        self.row = row
        self.get_user_by_phone = AsyncMock(return_value=row)
        self.get_user_by_id = AsyncMock(return_value=row)
        self.reserve_phone_code = AsyncMock(return_value=0)
        self.activate_phone_code = AsyncMock(return_value=True)
        self.invalidate_phone_code = AsyncMock()
        self.consume_phone_code = AsyncMock(return_value="ok")
        self.create_phone_user = AsyncMock(return_value=True)
        self.create_session = AsyncMock()
        self.create_phone_session = AsyncMock(return_value=True)
        self.update_user_last_login = AsyncMock()
        self.bind_user_phone = AsyncMock(return_value=True)
        self.record_phone_login_event = AsyncMock()
        self.get_session_by_token_hash = AsyncMock(
            return_value={
                "id": "session",
                "user_id": "existing-user",
                "sid": "sid",
                "revoked": False,
                "expires_at": datetime.now(UTC) + timedelta(days=1),
            }
        )
        self.rotate_session = AsyncMock()
        self.revoke_session = AsyncMock()


@pytest.mark.parametrize("phone", ["13800138000", "+8613800138000", " 13800138000 "])
def test_canonical_mainland_phone(phone, monkeypatch):
    monkeypatch.setattr(routes.settings, "sms_default_country_code", "86")
    assert PhoneVerifyRequest(phone=phone, code="000012").phone == PHONE


@pytest.mark.parametrize(
    "phone",
    [
        "12800138000",
        "+016505551234",
        "8613800138000",
        "138 0013 8000",
        "\uff11\uff13\uff18\uff10\uff10\uff11\uff13\uff18\uff10\uff10\uff10",
    ],
)
def test_invalid_phone(phone):
    with pytest.raises(ValidationError):
        PhoneVerifyRequest(phone=phone, code="000012")


def test_hash_is_secret_and_purpose_and_user_bound(enabled):
    assert hash_phone_code(PHONE, "signup", None, "123456") != hash_phone_code(
        PHONE, "login", None, "123456"
    )
    assert hash_phone_code(PHONE, "bind", "u1", "123456") != hash_phone_code(
        PHONE, "bind", "u2", "123456"
    )


@pytest.mark.asyncio
async def test_disabled_phone_feature_never_sends(enabled, monkeypatch):
    monkeypatch.setattr(routes, "is_sms_enabled", lambda: False)
    sender = AsyncMock()
    monkeypatch.setattr(routes, "run_in_threadpool", sender)
    store = Store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), store
        )
    assert exc.value.status_code == 503
    sender.assert_not_called()
    store.reserve_phone_code.assert_not_called()


@pytest.mark.asyncio
async def test_closed_signup_blocks_code_and_submit(enabled, monkeypatch):
    monkeypatch.setattr(routes, "is_public_signup_enabled", AsyncMock(return_value=False))
    store = Store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), store
        )
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await routes.phone_signup(
            request(),
            PhoneSignupRequest(phone=PHONE, code="123456", user_name="Tester", accepted_tos=True),
            BackgroundTasks(),
            store,
            None,
        )
    assert exc.value.status_code == 403
    store.reserve_phone_code.assert_not_called()
    store.consume_phone_code.assert_not_called()


@pytest.mark.asyncio
async def test_failed_captcha_prevents_send(enabled, monkeypatch):
    monkeypatch.setattr(routes, "verify_turnstile_token", AsyncMock(return_value=False))
    store = Store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), store
        )
    assert exc.value.status_code == 400
    store.reserve_phone_code.assert_not_called()


@pytest.mark.asyncio
async def test_send_failure_invalidates_inactive_code_and_preserves_quota(enabled, monkeypatch):
    sender = AsyncMock(return_value=False)
    monkeypatch.setattr(routes, "run_in_threadpool", sender)
    store = Store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), store
        )
    assert exc.value.status_code == 503
    store.reserve_phone_code.assert_awaited_once()
    store.invalidate_phone_code.assert_awaited_once()
    store.activate_phone_code.assert_not_called()
    assert "code_hash" in store.reserve_phone_code.call_args.kwargs
    assert "code" not in store.reserve_phone_code.call_args.kwargs


@pytest.mark.asyncio
async def test_send_success_waits_for_provider_then_activates(enabled, monkeypatch):
    store = Store()

    async def send(*args):
        store.activate_phone_code.assert_not_called()
        return True

    monkeypatch.setattr(routes, "run_in_threadpool", send)
    result = await routes.send_phone_code(
        request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), store
    )
    assert set(result.model_dump()) == {"message", "retry_after"}
    store.activate_phone_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_login_and_existing_signup_do_not_send(enabled, monkeypatch):
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "run_in_threadpool", sender)
    await routes.send_phone_code(request(), PhoneCodeRequest(phone=PHONE, purpose="login"), Store())
    await routes.send_phone_code(
        request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), Store(user())
    )
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_bind_code_requires_authenticated_user(enabled):
    store = Store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="bind"), store
        )
    assert exc.value.status_code == 401
    store.reserve_phone_code.assert_not_called()


@pytest.mark.asyncio
async def test_signup_phone_identity_without_fabricated_email(enabled):
    store = Store()
    result = await routes.phone_signup(
        request(),
        PhoneSignupRequest(phone=PHONE, code="123456", user_name="Tester", accepted_tos=True),
        BackgroundTasks(),
        store,
        None,
    )
    assert result.email is None
    assert result.phone == PHONE
    assert store.consume_phone_code.call_args.kwargs["purpose"] == "signup"
    assert "email" not in store.create_phone_user.call_args.kwargs


@pytest.mark.asyncio
async def test_domain_allowlist_makes_phone_signup_pending(enabled, monkeypatch):
    monkeypatch.setattr(routes, "allowlist_is_empty", AsyncMock(return_value=False))
    store = Store()
    result = await routes.phone_signup(
        request(),
        PhoneSignupRequest(phone=PHONE, code="123456", user_name="Tester", accepted_tos=True),
        BackgroundTasks(),
        store,
        None,
    )
    assert result.requires_approval is True
    assert store.create_phone_user.call_args.kwargs["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_phone_login_and_authenticated_profile_and_refresh(enabled):
    store = Store(user())
    response = Response()
    result = await routes.phone_login(
        request(), response, PhoneVerifyRequest(phone=PHONE, code="123456"), store
    )
    assert verify_access_token(result.access_token)["email"] == ""
    assert result.user.email is None
    assert result.user.email_verified is False
    assert result.user.phone_verified is True
    assert result.user.has_password is False
    assert "httponly" in response.headers["set-cookie"].lower()
    context = await get_current_user("Bearer " + result.access_token, store)
    assert context["user_id"] == "existing-user"
    assert context["phone"] == PHONE
    profile = await user_routes.get_current_user_info(context, store)
    assert profile.has_password is False
    refreshed = await auth_routes.refresh(Response(), "refresh-token", store)
    assert verify_access_token(refreshed.access_token)["email"] == ""


@pytest.mark.asyncio
async def test_phone_login_revokes_the_session_its_cookie_replaces(enabled):
    store = Store(user())
    await routes.phone_login(
        request(cookie="refresh_token=previous-refresh"),
        Response(),
        PhoneVerifyRequest(phone=PHONE, code="123456"),
        store,
    )
    store.get_session_by_token_hash.assert_awaited_once_with(
        auth_routes.hash_refresh_token("previous-refresh")
    )
    store.revoke_session.assert_awaited_once_with("session")


@pytest.mark.asyncio
async def test_phone_login_without_a_previous_cookie_revokes_nothing(enabled):
    store = Store(user())
    await routes.phone_login(
        request(), Response(), PhoneVerifyRequest(phone=PHONE, code="123456"), store
    )
    store.get_session_by_token_hash.assert_not_awaited()
    store.revoke_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_login_survives_a_failed_revoke(enabled):
    store = Store(user())
    store.revoke_session.side_effect = RuntimeError("database unavailable")
    response = Response()
    result = await routes.phone_login(
        request(cookie="refresh_token=previous-refresh"),
        response,
        PhoneVerifyRequest(phone=PHONE, code="123456"),
        store,
    )
    assert result.access_token
    assert "refresh_token=" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_failed_phone_login_keeps_the_previous_session(enabled):
    store = Store(user(status="pending_approval"))
    with pytest.raises(HTTPException):
        await routes.phone_login(
            request(cookie="refresh_token=previous-refresh"),
            Response(),
            PhoneVerifyRequest(phone=PHONE, code="123456"),
            store,
        )
    store.revoke_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending_approval", "rejected", "deleted", "suspended"])
async def test_phone_login_refuses_inactive_status(enabled, status):
    store = Store(user(status=status))
    with pytest.raises(Exception) as exc:
        await routes.phone_login(
            request(), Response(), PhoneVerifyRequest(phone=PHONE, code="123456"), store
        )
    assert getattr(exc.value, "status_code", 403) == 403
    store.create_phone_session.assert_not_called()
    assert store.record_phone_login_event.call_args.kwargs["outcome"] == "failure"


@pytest.mark.asyncio
async def test_phone_login_does_not_bypass_real_unverified_email(enabled):
    store = Store(user(email="admin@example.com", email_verified=False))
    with pytest.raises(HTTPException) as exc:
        await routes.phone_login(
            request(), Response(), PhoneVerifyRequest(phone=PHONE, code="123456"), store
        )
    assert exc.value.status_code == 403
    store.create_phone_session.assert_not_called()


@pytest.mark.asyncio
async def test_phone_only_unverified_identity_refused_even_when_email_gate_disabled(
    enabled, monkeypatch
):
    from serving.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "signup_require_email_verification", False)
    monkeypatch.setattr(routes.settings, "signup_require_email_verification", False)
    store = Store(user(phone_verified=False))
    token, _ = create_access_token(user_id="existing-user", email=None)
    with pytest.raises(HTTPException) as exc:
        await get_current_user("Bearer " + token, store)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bind_scopes_code_to_authenticated_user_and_never_creates_user(enabled):
    store = Store(user(email="original@example.com", email_verified=True))
    await routes.bind_phone(
        request(),
        PhoneVerifyRequest(phone=PHONE, code="123456"),
        {"user_id": "original-user"},
        store,
        None,
    )
    assert store.consume_phone_code.call_args.kwargs["user_id"] == "original-user"
    store.bind_user_phone.assert_awaited_once_with("original-user", PHONE)
    store.create_phone_user.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_or_wrong_otp_never_creates_session(enabled):
    store = Store(user())
    for result, status in [("rate_limited", 429), ("invalid", 400)]:
        store.consume_phone_code.return_value = result
        with pytest.raises(HTTPException) as exc:
            await routes.phone_login(
                request(), Response(), PhoneVerifyRequest(phone=PHONE, code="123456"), store
            )
        assert exc.value.status_code == status
    store.create_phone_session.assert_not_called()


@pytest.mark.asyncio
async def test_deleted_after_code_consumption_cannot_create_session_or_cookie(enabled):
    store = Store(user())
    store.create_phone_session.return_value = False
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await routes.phone_login(
            request(), response, PhoneVerifyRequest(phone=PHONE, code="123456"), store
        )
    assert exc.value.status_code == 403
    assert "set-cookie" not in response.headers
    store.update_user_last_login.assert_not_called()
    assert store.record_phone_login_event.call_args.kwargs["outcome"] == "failure"
