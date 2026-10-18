"""Changing an account's sign-in email or giving it a first password needs its owner."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException, Request

from serving.schemas_auth import BindEmailRequest, ChangePasswordRequest, PhoneCodeRequest
from serving.servers.routers import auth_routes, phone_auth_routes, reauth, user_routes
from serving.utils import password as password_utils

PHONE = "+8613800138000"
PASSWORD = "Correct-Horse-1"
PASSWORD_HASH = password_utils.hash_password(PASSWORD)


def request(authorization=None):
    headers = [(b"user-agent", b"owner-change-test")]
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


def account(**overrides):
    row = {
        "id": "owner",
        "email": "owner@e2e-mail.org",
        "login_name": None,
        "phone": None,
        "phone_verified": False,
        "email_verified": True,
        "password_hash": PASSWORD_HASH,
        "user_name": "Owner",
        "role": "free",
        "status": "active",
        "created_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def phone_account(**overrides):
    return account(
        **{"email": None, "password_hash": None, "phone": PHONE, "phone_verified": True} | overrides
    )


class Store:
    def __init__(self, row, *, taken_by=None):
        self.row = row
        self.get_user_by_id = AsyncMock(return_value=row)
        self.get_user_by_phone = AsyncMock(return_value=row)
        # The account columns are in place (no upgrade pending).
        self.require_phone_accounts = AsyncMock()
        self.get_user_by_email = AsyncMock(return_value=taken_by)
        self.consume_phone_code = AsyncMock(return_value="ok")
        self.reserve_phone_code = AsyncMock(return_value=0)
        self.activate_phone_code = AsyncMock(return_value=True)
        self.invalidate_phone_code = AsyncMock()
        self.delete_user_verification_tokens = AsyncMock()
        self.create_verification_token = AsyncMock()
        self.get_verification_token = AsyncMock()
        self.mark_verification_used = AsyncMock()
        self.mark_user_email_verified = AsyncMock()
        self.bind_user_email = AsyncMock(return_value=True)
        self.update_user_fields = AsyncMock()
        self.revoke_user_sessions = AsyncMock(return_value=0)


@pytest.fixture
def setup(monkeypatch):
    from serving.config.settings import get_settings

    secret = "test-secret-for-owner-change-routes-long-enough"
    monkeypatch.setattr(get_settings(), "jwt_secret_key", secret)
    monkeypatch.setattr(phone_auth_routes.settings, "jwt_secret_key", secret)
    monkeypatch.setattr(phone_auth_routes.settings, "signup_require_email_verification", False)
    monkeypatch.setattr(reauth, "check_and_record_login", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(reauth, "is_sms_enabled", lambda: True)
    monkeypatch.setattr(auth_routes, "is_email_enabled", lambda: True)
    monkeypatch.setattr(auth_routes, "log_admin_action", AsyncMock())
    monkeypatch.setattr(phone_auth_routes, "is_sms_enabled", lambda: True)
    monkeypatch.setattr(phone_auth_routes, "verify_turnstile_token", AsyncMock(return_value=True))
    import serving.config.runtime_settings as runtime

    monkeypatch.setattr(
        runtime,
        "get_runtime_settings_instance",
        lambda: (_ for _ in ()).throw(RuntimeError("not initialized")),
    )


async def bind(store, body, tasks=None):
    return await auth_routes.bind_email(
        request(), body, tasks or BackgroundTasks(), {"user_id": store.row["id"]}, store, None
    )


# -- binding or changing the email --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [None, "", "Wrong-Horse-1"])
async def test_changing_email_without_the_password_changes_nothing(setup, password):
    store = Store(account())
    with pytest.raises(HTTPException) as exc:
        await bind(store, BindEmailRequest(email="new@e2e-mail.org", password=password))
    assert exc.value.status_code == 400
    store.create_verification_token.assert_not_awaited()
    store.bind_user_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_changing_email_with_the_password_sends_a_link_to_the_new_address(setup):
    store = Store(account())
    tasks = BackgroundTasks()
    result = await bind(
        store, BindEmailRequest(email="New@E2e-mail.org", password=PASSWORD), tasks=tasks
    )
    assert result.email_verified is False
    assert store.create_verification_token.call_args.kwargs["pending_email"] == "new@e2e-mail.org"
    [task] = tasks.tasks
    assert task.func is auth_routes.send_verification_email
    assert task.args[0] == "new@e2e-mail.org"
    store.bind_user_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_account_binds_email_with_a_bind_email_code(setup):
    store = Store(phone_account())
    with pytest.raises(HTTPException) as exc:
        await bind(store, BindEmailRequest(email="new@e2e-mail.org"))
    assert exc.value.status_code == 400
    store.consume_phone_code.assert_not_awaited()

    await bind(store, BindEmailRequest(email="new@e2e-mail.org", phone_code="123456"))
    kwargs = store.consume_phone_code.call_args.kwargs
    assert (kwargs["phone"], kwargs["purpose"], kwargs["user_id"]) == (
        PHONE,
        "bind_email",
        "owner",
    )
    store.create_verification_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_unconfirmed_session_cannot_learn_whether_an_address_is_taken(setup):
    store = Store(account(), taken_by={"id": "someone-else"})
    with pytest.raises(HTTPException) as exc:
        await bind(store, BindEmailRequest(email="taken@e2e-mail.org", password="Wrong-Horse-1"))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await bind(store, BindEmailRequest(email="taken@e2e-mail.org", password=PASSWORD))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_an_address_is_never_bound_unproven_even_with_signup_verification_off(
    setup, monkeypatch
):
    # Signup verification is off in this fixture. Binding still waits for the
    # link, so an ADMIN_EMAILS address nobody owns cannot be claimed.
    monkeypatch.setattr(auth_routes, "is_email_enabled", lambda: False)
    store = Store(account())
    with pytest.raises(HTTPException) as exc:
        await bind(store, BindEmailRequest(email="admin@e2e-mail.org", password=PASSWORD))
    assert exc.value.status_code == 503
    store.bind_user_email.assert_not_awaited()


# -- following the link -------------------------------------------------------


def token_row(**overrides):
    row = {
        "token": "link",
        "user_id": "owner",
        "created_at": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "used_at": None,
        "pending_email": "new@e2e-mail.org",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_following_the_link_binds_and_tells_the_replaced_address(setup):
    store = Store(account())
    store.get_verification_token.return_value = token_row()
    tasks = BackgroundTasks()
    result = await auth_routes.verify_email("link", tasks, store)
    assert result.email_verified is True
    store.bind_user_email.assert_awaited_once_with("owner", "new@e2e-mail.org")
    store.mark_verification_used.assert_awaited_once_with("link")
    [task] = tasks.tasks
    assert task.func is auth_routes.send_email_changed_email
    assert task.args == ("owner@e2e-mail.org", "new@e2e-mail.org")


@pytest.mark.asyncio
async def test_a_used_link_never_binds_again(setup):
    # The account moved on to another address after following this link.
    store = Store(account(email="later@e2e-mail.org"))
    store.get_verification_token.return_value = token_row(used_at=datetime.now(UTC))
    with pytest.raises(HTTPException) as exc:
        await auth_routes.verify_email("link", BackgroundTasks(), store)
    assert exc.value.status_code == 400
    store.bind_user_email.assert_not_awaited()


# -- a first password ---------------------------------------------------------


async def change(store, body, tasks=None):
    return await user_routes.change_password(
        request(), body, tasks or BackgroundTasks(), {"user_id": "owner", "sid": "here"}, store
    )


@pytest.mark.asyncio
async def test_phone_account_needs_a_set_password_code_for_a_first_password(setup):
    store = Store(phone_account())
    with pytest.raises(HTTPException) as exc:
        await change(store, ChangePasswordRequest(new_password="Brand-New-Pass-1"))
    assert exc.value.status_code == 400
    store.update_user_fields.assert_not_awaited()

    await change(store, ChangePasswordRequest(new_password="Brand-New-Pass-1", phone_code="123456"))
    assert store.consume_phone_code.call_args.kwargs["purpose"] == "set_password"
    store.update_user_fields.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_code_sets_no_password(setup):
    store = Store(phone_account())
    store.consume_phone_code.return_value = "invalid"
    with pytest.raises(HTTPException) as exc:
        await change(
            store, ChangePasswordRequest(new_password="Brand-New-Pass-1", phone_code="123456")
        )
    assert exc.value.status_code == 400
    store.update_user_fields.assert_not_awaited()


# -- changing the password ----------------------------------------------------


@pytest.mark.asyncio
async def test_guessing_the_current_password_counts_against_the_login_limit(setup, monkeypatch):
    limiter = AsyncMock(return_value=(False, "email"))
    monkeypatch.setattr(reauth, "check_and_record_login", limiter)
    store = Store(account())
    with pytest.raises(HTTPException) as exc:
        await change(
            store, ChangePasswordRequest(old_password=PASSWORD, new_password="Brand-New-Pass-1")
        )
    assert exc.value.status_code == 429
    assert limiter.await_args.args[0] == "owner@e2e-mail.org"
    store.update_user_fields.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_password", ["", "Wrong-Horse-1"])
async def test_a_wrong_current_password_changes_nothing(setup, old_password):
    store = Store(account())
    with pytest.raises(HTTPException) as exc:
        await change(
            store,
            ChangePasswordRequest(old_password=old_password, new_password="Brand-New-Pass-1"),
        )
    assert exc.value.status_code == 400
    store.update_user_fields.assert_not_awaited()
    store.revoke_user_sessions.assert_not_awaited()


@pytest.mark.asyncio
async def test_changing_the_password_signs_other_sessions_out_and_tells_the_owner(
    setup, monkeypatch
):
    monkeypatch.setattr(user_routes, "is_email_enabled", lambda: True)
    store = Store(account())
    tasks = BackgroundTasks()
    result = await change(
        store,
        ChangePasswordRequest(old_password=PASSWORD, new_password="Brand-New-Pass-1"),
        tasks=tasks,
    )
    assert "signed out" in result.message
    store.revoke_user_sessions.assert_awaited_once_with("owner", except_sid="here")
    [task] = tasks.tasks
    assert task.func is user_routes.send_password_changed_email
    assert task.args == ("owner@e2e-mail.org",)
    assert task.kwargs == {"first_password": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row", [phone_account(), account(email_verified=False)], ids=["no-email", "unverified"]
)
async def test_no_notice_goes_to_an_address_the_owner_never_proved(setup, monkeypatch, row):
    monkeypatch.setattr(user_routes, "is_email_enabled", lambda: True)
    store = Store(row)
    tasks = BackgroundTasks()
    await change(
        store,
        ChangePasswordRequest(
            old_password=PASSWORD, new_password="Brand-New-Pass-1", phone_code="123456"
        ),
        tasks=tasks,
    )
    store.revoke_user_sessions.assert_awaited_once_with("owner", except_sid="here")
    assert tasks.tasks == []


@pytest.mark.asyncio
async def test_a_first_password_on_an_account_with_an_email_says_set(setup, monkeypatch):
    monkeypatch.setattr(user_routes, "is_email_enabled", lambda: True)
    store = Store(phone_account(email="owner@e2e-mail.org"))
    tasks = BackgroundTasks()
    await change(
        store,
        ChangePasswordRequest(new_password="Brand-New-Pass-1", phone_code="123456"),
        tasks=tasks,
    )
    [task] = tasks.tasks
    assert task.kwargs == {"first_password": True}


# -- sending the codes --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["bind_email", "set_password"])
@pytest.mark.parametrize(
    ("owner", "sent"),
    [(phone_account(), True), (phone_account(id="someone-else"), False)],
)
async def test_owner_codes_go_only_to_the_accounts_own_number(
    setup, monkeypatch, purpose, owner, sent
):
    from serving.utils.jwt import create_access_token

    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(phone_auth_routes, "run_in_threadpool", sender)
    store = Store(phone_account())
    store.get_user_by_phone.return_value = owner
    token, _ = create_access_token(user_id="owner", email=None)
    await phone_auth_routes.send_phone_code(
        request(authorization=f"Bearer {token}"),
        PhoneCodeRequest(phone=PHONE, purpose=purpose),
        store,
    )
    assert store.reserve_phone_code.call_args.kwargs["user_id"] == "owner"
    assert sender.await_count == (1 if sent else 0)
