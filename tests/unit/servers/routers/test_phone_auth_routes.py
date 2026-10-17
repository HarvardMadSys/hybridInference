"""Phone route policy, identity compatibility, and external-send failure boundaries."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException, Request, Response
from pydantic import ValidationError

from serving.exceptions import AccountSchemaPending
from serving.schemas_auth import (
    BindEmailRequest,
    PhoneCodeRequest,
    PhoneSignupRequest,
    PhoneVerifyRequest,
)
from serving.servers.deps import get_current_user
from serving.servers.routers import auth_routes, phone_auth_routes as routes, reauth, user_routes
from serving.setup_state import init_setup_state
from serving.utils.jwt import create_access_token, verify_access_token
from serving.utils.phone_auth import hash_phone_code
from tests.fixtures import browser_sessions

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


def issued_cookie(response):
    """The session cookie value a response set."""
    header = response.headers["set-cookie"]
    return header.split("refresh_token=", 1)[1].split(";", 1)[0]


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
        self.require_phone_accounts = AsyncMock()
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
        self.bind_user_email = AsyncMock(return_value=True)
        self.get_user_avatar = AsyncMock(return_value=None)
        self.get_user_by_email = AsyncMock(return_value=None)
        self.delete_user_verification_tokens = AsyncMock()
        self.create_verification_token = AsyncMock()
        self.get_verification_token = AsyncMock()
        self.mark_verification_used = AsyncMock()
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
        self.browser_sessions = browser_sessions.install(self)


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


@pytest.fixture
async def setup_pending():
    """First-run setup is pending: no account exists and the setup code is stored."""
    setup_store = MagicMock()
    setup_store.get_or_create_setup_code = AsyncMock(return_value="ABCDEFGHJKLM")
    await init_setup_state(setup_store)


NOT_SET_UP = (503, "This deployment has not been set up yet.")


@pytest.mark.asyncio
async def test_signup_code_waits_for_first_run_setup(enabled, setup_pending, monkeypatch):
    # Any account completes setup, so a stranger's phone account would lock the
    # operator out of /auth/setup/admin. Refused before the captcha, the quota
    # reservation and the SMS, so the refusal costs nothing.
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "run_in_threadpool", sender)
    store = Store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), store
        )
    assert (exc.value.status_code, exc.value.detail) == NOT_SET_UP
    routes.verify_turnstile_token.assert_not_awaited()
    store.get_user_by_phone.assert_not_called()
    store.reserve_phone_code.assert_not_called()
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_phone_signup_waits_for_first_run_setup(enabled, setup_pending):
    store = Store()
    with pytest.raises(HTTPException) as exc:
        await routes.phone_signup(
            request(),
            PhoneSignupRequest(phone=PHONE, code="123456", user_name="Stranger", accepted_tos=True),
            BackgroundTasks(),
            store,
            None,
        )
    assert (exc.value.status_code, exc.value.detail) == NOT_SET_UP
    routes.check_and_record_signup.assert_not_awaited()
    store.consume_phone_code.assert_not_called()
    store.create_phone_user.assert_not_called()


# While an upgrade's users migration waits for its lock (the nightly pg_dump),
# the phone columns do not exist yet: every phone route answers 503 after its
# own gates and before any code is reserved, sent or consumed.
UPGRADE_PENDING = (503, AccountSchemaPending.MESSAGE)


def upgrade_pending_store(row=None):
    store = Store(row)
    store.require_phone_accounts = AsyncMock(side_effect=AccountSchemaPending())
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["signup", "login"])
async def test_codes_wait_for_the_database_upgrade(enabled, monkeypatch, purpose):
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "run_in_threadpool", sender)
    store = upgrade_pending_store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose=purpose), store
        )
    assert (exc.value.status_code, exc.value.detail) == UPGRADE_PENDING
    routes.verify_turnstile_token.assert_not_awaited()
    store.get_user_by_phone.assert_not_called()
    store.reserve_phone_code.assert_not_called()
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_phone_signup_waits_for_the_database_upgrade(enabled):
    store = upgrade_pending_store()
    with pytest.raises(HTTPException) as exc:
        await routes.phone_signup(
            request(),
            PhoneSignupRequest(phone=PHONE, code="123456", user_name="Tester", accepted_tos=True),
            BackgroundTasks(),
            store,
            None,
        )
    assert (exc.value.status_code, exc.value.detail) == UPGRADE_PENDING
    routes.check_and_record_signup.assert_not_awaited()
    store.consume_phone_code.assert_not_called()
    store.create_phone_user.assert_not_called()


@pytest.mark.asyncio
async def test_phone_login_waits_for_the_database_upgrade(enabled):
    store = upgrade_pending_store(user())
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await routes.phone_login(
            request(), response, PhoneVerifyRequest(phone=PHONE, code="123456"), store
        )
    assert (exc.value.status_code, exc.value.detail) == UPGRADE_PENDING
    store.get_user_by_phone.assert_not_called()
    store.consume_phone_code.assert_not_called()
    store.create_phone_session.assert_not_called()
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_binding_waits_for_the_database_upgrade(enabled):
    store = upgrade_pending_store(user(email="original@example.com", email_verified=True))
    with pytest.raises(HTTPException) as exc:
        await routes.bind_phone(
            request(),
            PhoneVerifyRequest(phone=PHONE, code="123456"),
            {"user_id": "original-user"},
            store,
            None,
        )
    assert (exc.value.status_code, exc.value.detail) == UPGRADE_PENDING
    store.consume_phone_code.assert_not_called()
    store.bind_user_phone.assert_not_called()


@pytest.mark.asyncio
async def test_signup_codes_report_pending_setup_before_the_upgrade(
    enabled, setup_pending, monkeypatch
):
    """A deployment that is not set up says so first, as for every registration route."""
    store = upgrade_pending_store()
    with pytest.raises(HTTPException) as exc:
        await routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="signup"), store
        )
    assert (exc.value.status_code, exc.value.detail) == NOT_SET_UP
    store.require_phone_accounts.assert_not_called()


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
    refreshed = await auth_routes.refresh(
        request(cookie=f"refresh_token={issued_cookie(response)}"), Response(), store
    )
    assert verify_access_token(refreshed.access_token)["email"] == ""


@pytest.mark.asyncio
async def test_phone_login_starts_a_browser_session(enabled):
    store = Store(user())
    response = Response()
    await routes.phone_login(
        request(), response, PhoneVerifyRequest(phone=PHONE, code="123456"), store
    )
    [browser] = store.browser_sessions.browsers.values()
    assert browser["active_user_id"] == "existing-user"
    assert browser["secret_hash"] == auth_routes.hash_refresh_token(issued_cookie(response))


@pytest.mark.asyncio
async def test_phone_login_joins_the_browser_it_signs_in_on(enabled):
    store = Store(user())
    first = Response()
    await routes.phone_login(
        request(), first, PhoneVerifyRequest(phone=PHONE, code="123456"), store
    )
    [browser_id] = store.browser_sessions.browsers
    store.browser_sessions.members[(browser_id, "other-user")] = "other-session"

    again = Response()
    await routes.phone_login(
        request(cookie=f"refresh_token={issued_cookie(first)}"),
        again,
        PhoneVerifyRequest(phone=PHONE, code="123456"),
        store,
    )

    # The same browser, still holding the other account, with a new cookie.
    assert list(store.browser_sessions.browsers) == [browser_id]
    assert (browser_id, "other-user") in store.browser_sessions.members
    assert issued_cookie(again) != issued_cookie(first)


@pytest.mark.asyncio
async def test_phone_login_is_refused_when_the_browser_is_full(enabled):
    store = Store(user())
    first = Response()
    await routes.phone_login(
        request(), first, PhoneVerifyRequest(phone=PHONE, code="123456"), store
    )
    [browser_id] = store.browser_sessions.browsers
    del store.browser_sessions.members[(browser_id, "existing-user")]
    for index in range(5):
        store.browser_sessions.members[(browser_id, f"user-{index}")] = f"session-{index}"
    store.create_phone_session.reset_mock()

    with pytest.raises(HTTPException) as refused:
        await routes.phone_login(
            request(cookie=f"refresh_token={issued_cookie(first)}"),
            Response(),
            PhoneVerifyRequest(phone=PHONE, code="123456"),
            store,
        )
    assert refused.value.status_code == 409
    store.create_phone_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_phone_login_leaves_the_browser_alone(enabled):
    store = Store(user(status="pending_approval"))
    with pytest.raises(HTTPException):
        await routes.phone_login(
            request(cookie="refresh_token=previous-refresh"),
            Response(),
            PhoneVerifyRequest(phone=PHONE, code="123456"),
            store,
        )
    assert store.browser_sessions.browsers == {}


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


@pytest.mark.asyncio
async def test_bind_email_issues_pending_verification_and_completes_on_verify(enabled, monkeypatch):
    monkeypatch.setattr(auth_routes, "is_email_enabled", lambda: True)
    monkeypatch.setattr(reauth, "is_sms_enabled", lambda: True)
    store = Store(user())
    tasks = BackgroundTasks()
    res = await auth_routes.bind_email(
        request(),
        BindEmailRequest(email="NewUser@tencent.com", phone_code="123456"),
        tasks,
        {"user_id": "existing-user"},
        store,
        None,
    )
    assert res.email == "newuser@tencent.com"
    assert res.email_verified is False
    store.delete_user_verification_tokens.assert_awaited_once_with("existing-user")
    token_kwargs = store.create_verification_token.call_args.kwargs
    assert token_kwargs["user_id"] == "existing-user"
    assert token_kwargs["pending_email"] == "newuser@tencent.com"
    store.bind_user_email.assert_not_called()

    store.get_verification_token.return_value = {
        "token": token_kwargs["token"],
        "user_id": "existing-user",
        "created_at": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
        "used_at": None,
        "pending_email": "newuser@tencent.com",
    }
    verified = await auth_routes.verify_email(token_kwargs["token"], BackgroundTasks(), store)
    assert verified.email_verified is True
    store.bind_user_email.assert_awaited_once_with("existing-user", "newuser@tencent.com")
    store.mark_verification_used.assert_awaited_once_with(token_kwargs["token"])
