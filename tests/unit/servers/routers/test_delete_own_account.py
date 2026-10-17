"""Self-service account deletion: confirmation, proof of ownership, sign-out."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException, Request, Response

from serving.schemas_auth import DeleteAccountRequest, PhoneCodeRequest, PhoneVerifyRequest
from serving.servers.routers import auth_routes, phone_auth_routes, reauth
from serving.utils import password as password_utils
from tests.fixtures import browser_sessions

PHONE = "+8613800138000"
PASSWORD = "Correct-Horse-1"
PASSWORD_HASH = password_utils.hash_password(PASSWORD)


def request(authorization=None, cookie=None):
    headers = [(b"user-agent", b"delete-account-test")]
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/auth/delete-account",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


def issued_cookie(response):
    header = response.headers["set-cookie"]
    return header.split("refresh_token=", 1)[1].split(";", 1)[0]


def account(**overrides):
    row = {
        "id": "owner",
        "email": "owner@example.org",
        "login_name": None,
        "phone": None,
        "phone_verified": False,
        "email_verified": True,
        "password_hash": PASSWORD_HASH,
        "user_name": "Owner",
        "role": "free",
        "status": "active",
        "created_at": datetime.now(UTC),
        "last_login_at": None,
    }
    row.update(overrides)
    return row


def phone_account(**overrides):
    return account(
        **{"email": None, "password_hash": None, "phone": PHONE, "phone_verified": True} | overrides
    )


class Store:
    def __init__(self, row):
        self.row = row
        self.get_user_by_id = AsyncMock(return_value=row)
        self.get_user_by_phone = AsyncMock(return_value=row)
        # The account columns are in place (no upgrade pending).
        self.require_phone_accounts = AsyncMock()
        self.consume_phone_code = AsyncMock(return_value="ok")
        self.reserve_phone_code = AsyncMock(return_value=0)
        self.activate_phone_code = AsyncMock(return_value=True)
        self.invalidate_phone_code = AsyncMock()
        self.create_phone_session = AsyncMock(return_value=True)
        self.record_phone_login_event = AsyncMock()
        # No pre-browser-session cookies here: a retired value names nothing.
        self.get_session_by_token_hash = AsyncMock(return_value=None)
        self.browser_sessions = browser_sessions.install(self)
        self.delete_user = AsyncMock(side_effect=self._delete_user)

    async def _delete_user(self, user_id, **_kwargs):
        # The real store takes the account off every browser in the same
        # transaction that marks it deleted.
        fake = self.browser_sessions
        for key in [key for key in fake.members if key[1] == user_id]:
            del fake.members[key]
        for browser in fake.browsers.values():
            if browser["active_user_id"] == user_id:
                browser["active_user_id"] = None


@pytest.fixture
def setup(monkeypatch):
    from serving.config.settings import get_settings

    secret = "test-secret-for-delete-account-route-long-enough"
    monkeypatch.setattr(get_settings(), "jwt_secret_key", secret)
    monkeypatch.setattr(phone_auth_routes.settings, "jwt_secret_key", secret)
    monkeypatch.setattr(phone_auth_routes.settings, "signup_require_email_verification", False)
    monkeypatch.setattr(reauth, "check_and_record_login", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(reauth, "is_sms_enabled", lambda: True)
    monkeypatch.setattr(auth_routes, "is_email_enabled", lambda: True)
    monkeypatch.setattr(phone_auth_routes, "is_sms_enabled", lambda: True)
    monkeypatch.setattr(phone_auth_routes, "verify_turnstile_token", AsyncMock(return_value=True))
    import serving.config.runtime_settings as runtime

    monkeypatch.setattr(
        runtime,
        "get_runtime_settings_instance",
        lambda: (_ for _ in ()).throw(RuntimeError("not initialized")),
    )


async def delete(store, body, *, req=None, response=None, tasks=None):
    return await auth_routes.delete_own_account(
        req or request(),
        response or Response(),
        body,
        tasks or BackgroundTasks(),
        {"user_id": store.row["id"]},
        store,
    )


# -- confirmation ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", ["owner@example.org", "  Owner@Example.ORG "])
async def test_password_account_is_deleted_with_its_identifier_and_password(setup, confirm):
    store = Store(account())
    tasks = BackgroundTasks()
    result = await delete(
        store, DeleteAccountRequest(confirm=confirm, password=PASSWORD), tasks=tasks
    )
    assert result.message == "Your account has been deleted."
    store.delete_user.assert_awaited_once()
    assert store.delete_user.call_args.args == ("owner",)
    assert store.delete_user.call_args.kwargs["self_service"] is True
    assert store.delete_user.call_args.kwargs["email"] == "owner@example.org"
    [task] = tasks.tasks
    assert task.func is auth_routes.send_account_deleted_email
    assert task.args == ("owner@example.org",)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", ["", "delete my account", "other@example.org"])
async def test_wrong_confirmation_deletes_nothing(setup, confirm):
    store = Store(account())
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm=confirm, password=PASSWORD))
    assert exc.value.status_code == 400
    store.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_name_account_confirms_with_its_login_name(setup):
    store = Store(account(email=None, login_name="owner-name"))
    with pytest.raises(HTTPException):
        await delete(store, DeleteAccountRequest(confirm="owner@example.org", password=PASSWORD))
    await delete(store, DeleteAccountRequest(confirm="owner-name", password=PASSWORD))
    store.delete_user.assert_awaited_once()


# -- proof of ownership ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [None, "", "Wrong-Horse-1"])
async def test_wrong_or_missing_password_deletes_nothing(setup, password):
    store = Store(account())
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm="owner@example.org", password=password))
    assert exc.value.status_code == 400
    store.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_attempts_share_the_login_rate_limit(setup, monkeypatch):
    limiter = AsyncMock(return_value=(False, "email"))
    monkeypatch.setattr(reauth, "check_and_record_login", limiter)
    store = Store(account())
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm="owner@example.org", password=PASSWORD))
    assert exc.value.status_code == 429
    assert limiter.call_args.args[0] == "owner@example.org"
    store.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_administrator_cannot_delete_itself(setup):
    store = Store(account(role="admin"))
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm="owner@example.org", password=PASSWORD))
    assert exc.value.status_code == 403
    store.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_account_confirms_with_a_delete_account_code(setup):
    store = Store(phone_account())
    tasks = BackgroundTasks()
    await delete(store, DeleteAccountRequest(confirm=PHONE, phone_code="123456"), tasks=tasks)
    kwargs = store.consume_phone_code.call_args.kwargs
    assert kwargs["phone"] == PHONE
    assert kwargs["purpose"] == "delete_account"
    assert kwargs["user_id"] == "owner"
    store.delete_user.assert_awaited_once()
    # No email address, so nobody to notify by email.
    assert tasks.tasks == []


@pytest.mark.asyncio
async def test_phone_account_ignores_a_password(setup):
    store = Store(phone_account())
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm=PHONE, password="anything"))
    assert exc.value.status_code == 400
    store.consume_phone_code.assert_not_awaited()
    store.delete_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "status"), [("invalid", 400), ("rate_limited", 429)])
async def test_failed_phone_code_deletes_nothing(setup, result, status):
    store = Store(phone_account())
    store.consume_phone_code.return_value = result
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm=PHONE, phone_code="123456"))
    assert exc.value.status_code == status
    store.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_account_cannot_delete_while_sms_is_off(setup, monkeypatch):
    monkeypatch.setattr(reauth, "is_sms_enabled", lambda: False)
    store = Store(phone_account())
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm=PHONE, phone_code="123456"))
    assert exc.value.status_code == 503
    store.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_with_no_way_to_confirm_is_refused(setup):
    store = Store(phone_account(phone_verified=False))
    with pytest.raises(HTTPException) as exc:
        await delete(store, DeleteAccountRequest(confirm=PHONE, phone_code="123456"))
    assert exc.value.status_code == 400
    store.delete_user.assert_not_awaited()


# -- the browser afterwards --------------------------------------------------


async def signed_in(store):
    """Sign the store's phone account in on a fresh browser; return its cookie."""
    response = Response()
    await phone_auth_routes.phone_login(
        request(), response, PhoneVerifyRequest(phone=PHONE, code="123456"), store
    )
    return issued_cookie(response)


@pytest.mark.asyncio
async def test_last_account_on_the_browser_retires_its_cookie(setup):
    store = Store(phone_account())
    cookie = await signed_in(store)
    response = Response()
    await delete(
        store,
        DeleteAccountRequest(confirm=PHONE, phone_code="123456"),
        req=request(cookie=f"refresh_token={cookie}"),
        response=response,
    )
    [browser] = store.browser_sessions.browsers.values()
    assert browser["revoked"] is True
    assert 'refresh_token=""' in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_other_accounts_on_the_browser_stay_signed_in(setup):
    store = Store(phone_account())
    cookie = await signed_in(store)
    [browser_id] = store.browser_sessions.browsers
    store.browser_sessions.members[(browser_id, "other-user")] = "other-session"
    response = Response()
    await delete(
        store,
        DeleteAccountRequest(confirm=PHONE, phone_code="123456"),
        req=request(cookie=f"refresh_token={cookie}"),
        response=response,
    )
    browser = store.browser_sessions.browsers[browser_id]
    assert browser["revoked"] is False
    assert browser["active_user_id"] is None
    assert list(store.browser_sessions.members) == [(browser_id, "other-user")]
    assert 'refresh_token=""' not in response.headers.get("set-cookie", "")


# -- sending the code --------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_account_code_requires_a_signed_in_account(setup):
    store = Store(phone_account())
    with pytest.raises(HTTPException) as exc:
        await phone_auth_routes.send_phone_code(
            request(), PhoneCodeRequest(phone=PHONE, purpose="delete_account"), store
        )
    assert exc.value.status_code == 401
    store.reserve_phone_code.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "sent"),
    [
        (phone_account(), True),
        (phone_account(id="someone-else"), False),
        (phone_account(phone_verified=False), False),
        (None, False),
    ],
)
async def test_delete_account_code_goes_only_to_the_accounts_own_number(
    setup, monkeypatch, owner, sent
):
    from serving.utils.jwt import create_access_token

    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(phone_auth_routes, "run_in_threadpool", sender)
    store = Store(phone_account())
    store.get_user_by_phone.return_value = owner
    token, _ = create_access_token(user_id="owner", email=None)
    await phone_auth_routes.send_phone_code(
        request(authorization=f"Bearer {token}"),
        PhoneCodeRequest(phone=PHONE, purpose="delete_account"),
        store,
    )
    assert store.reserve_phone_code.call_args.kwargs["user_id"] == "owner"
    assert store.reserve_phone_code.call_args.kwargs["purpose"] == "delete_account"
    assert sender.await_count == (1 if sent else 0)


# -- races ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_that_changed_before_the_delete_ran_is_not_deleted(setup):
    from serving.exceptions import SelfDeleteRefused

    store = Store(phone_account())
    cookie = await signed_in(store)
    store.delete_user.side_effect = SelfDeleteRefused("owner")
    response = Response()
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        await delete(
            store,
            DeleteAccountRequest(confirm=PHONE, phone_code="123456"),
            req=request(cookie=f"refresh_token={cookie}"),
            response=response,
            tasks=tasks,
        )
    assert exc.value.status_code == 409
    [browser] = store.browser_sessions.browsers.values()
    assert browser["revoked"] is False
    assert "set-cookie" not in response.headers
    assert tasks.tasks == []


@pytest.mark.asyncio
async def test_cookie_rotated_by_another_tab_is_left_alone(setup):
    store = Store(phone_account())
    cookie = await signed_in(store)
    [browser_id] = store.browser_sessions.browsers
    store.browser_sessions.members[(browser_id, "other-user")] = "other-session"
    # Another tab refreshes first: the cookie this request carries is retired,
    # and the browser now holds a value this response must not overwrite.
    assert await store.browser_sessions.rotate_browser_session(
        browser_id,
        current_secret_hash=auth_routes.hash_refresh_token(cookie),
        new_secret_hash=auth_routes.hash_refresh_token("rotated-by-another-tab"),
        expires_at=None,
    )
    response = Response()
    await delete(
        store,
        DeleteAccountRequest(confirm=PHONE, phone_code="123456"),
        req=request(cookie=f"refresh_token={cookie}"),
        response=response,
    )
    store.delete_user.assert_awaited_once()
    assert "set-cookie" not in response.headers
    browser = store.browser_sessions.browsers[browser_id]
    assert browser["revoked"] is False
    assert list(store.browser_sessions.members) == [(browser_id, "other-user")]


@pytest.mark.asyncio
async def test_sign_in_racing_the_last_sign_out_starts_a_fresh_browser(setup):
    from serving.auth.browser_sessions import complete_sign_in, resolve_browser_session

    store = Store(phone_account())
    cookie = await signed_in(store)
    [browser_id] = store.browser_sessions.browsers
    # Another tab's sign-in resolved the browser while it still held an account...
    browser = await resolve_browser_session(store, request(cookie=f"refresh_token={cookie}"))
    # ...and the last account signed out before the sign-in attached to it.
    assert await store.browser_sessions.detach_browser_session_account(browser_id, "owner") == 0
    assert store.browser_sessions.browsers[browser_id]["revoked"] is True

    response = Response()
    await complete_sign_in(
        store, response, browser, user_id="newcomer", auth_session_id="newcomer-session"
    )

    [fresh_id] = [bid for bid in store.browser_sessions.browsers if bid != browser_id]
    assert store.browser_sessions.browsers[fresh_id]["active_user_id"] == "newcomer"
    assert (fresh_id, "newcomer") in store.browser_sessions.members
    assert issued_cookie(response)
