"""What a signed-in account does with single sign-on, over HTTP against the real store.

Confirming a sensitive action at the provider, linking and unlinking from
Settings, and an administrator removing a link. Each write is confirmed by the
account's owner; these tests show which proof each kind of account gives and
that nothing else is accepted.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from serving.utils import login_rate_limit
from tests.fixtures.oidc_provider import ISSUER, id_token_claims, sign_id_token
from tests.fixtures.sso_app import (
    CALLBACK,
    EMAIL,
    PASSWORD,
    answer,
    existing_user,
    location,
    new_browser,
    sign_in,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

SETTINGS = "/dashboard/settings"
NEW_PASSWORD = "A-brand-new-password-7"


async def _password_session(browser, email: str = EMAIL) -> dict[str, str]:
    login = await browser.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _sso_session(browser, idp, **claims: Any) -> dict[str, str]:
    """Sign in through the provider and return the session's bearer header."""
    done = await sign_in(browser, idp, **claims)
    assert location(done)[1].get("sso") == ["complete"], done.headers["location"]
    refreshed = await browser.post("/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    return {"Authorization": f"Bearer {refreshed.json()['access_token']}"}


@pytest.fixture
def members_sign_straight_in(sso):
    """A provider whose members get an account on their first sign-in."""
    sso.configure(auto_create=True, require_approval=False)
    return sso


async def _reauth_start(browser, headers, purpose: str, return_to: str = SETTINGS):
    response = await browser.post(
        "/auth/sso/reauth/start", json={"purpose": purpose, "return_to": return_to}, headers=headers
    )
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)


async def _reauth(browser, idp, headers, purpose: str, **claims: Any):
    """Confirm *purpose* at the provider; returns the redirect back to the console."""
    params = await _reauth_start(browser, headers, purpose)
    claims.setdefault("auth_time", int(time.time()))
    return await answer(browser, idp, params, **claims)


# -- confirming a sensitive action at the provider ---------------------------------


async def test_an_account_without_a_password_confirms_at_its_provider(
    browser, idp, store, members_sign_straight_in
):
    headers = await _sso_session(browser, idp)

    me = (await browser.get("/user/me", headers=headers)).json()
    assert me["has_password"] is False
    assert me["sso_reauth_provider"] == {"id": "example-univ", "name": "Example University"}

    # The session alone does not confirm anything.
    refused = await browser.post("/auth/delete-account", json={"confirm": EMAIL}, headers=headers)
    assert refused.status_code == 400
    assert "organization" in refused.json()["detail"]

    params = await _reauth_start(browser, headers, "delete_account")
    # The provider is asked for a fresh sign-in, with the account's address filled in.
    assert params["prompt"] == ["login"] and params["max_age"] == ["0"]
    assert params["login_hint"] == [EMAIL]
    back = await answer(browser, idp, params, auth_time=int(time.time()))
    assert location(back) == (SETTINGS, {"reauth": ["delete_account"]})

    deleted = await browser.post("/auth/delete-account", json={"confirm": EMAIL}, headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert (await store.get_user_by_email(EMAIL))["status"] == "deleted"


async def test_a_confirmation_is_for_one_action_and_is_spent_by_it(
    browser, idp, store, members_sign_straight_in
):
    headers = await _sso_session(browser, idp)
    await _reauth(browser, idp, headers, "set_password")

    # Confirming one action does not confirm another.
    refused = await browser.post("/auth/delete-account", json={"confirm": EMAIL}, headers=headers)
    assert refused.status_code == 400

    first = await browser.post(
        "/user/change-password", json={"new_password": NEW_PASSWORD}, headers=headers
    )
    assert first.status_code == 200, first.text
    user = await store.get_user_by_email(EMAIL)
    assert user["password_hash"] is not None

    # The account now has a password, so it is no longer confirmed at the provider.
    me = (await browser.get("/user/me", headers=headers)).json()
    assert me["has_password"] is True and me["sso_reauth_provider"] is None


async def test_a_confirmation_belongs_to_the_session_that_made_it(
    app, browser, idp, store, members_sign_straight_in
):
    headers = await _sso_session(browser, idp)
    await _reauth(browser, idp, headers, "delete_account")

    async with new_browser(app) as elsewhere:
        other_session = await _sso_session(elsewhere, idp)
        refused = await elsewhere.post(
            "/auth/delete-account", json={"confirm": EMAIL}, headers=other_session
        )

    assert refused.status_code == 400
    assert (await store.get_user_by_email(EMAIL))["status"] == "active"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"auth_time": int(time.time()) - 3600}, id="signed in an hour ago"),
        pytest.param({"auth_time": None}, id="provider does not say when"),
    ],
)
async def test_only_a_fresh_sign_in_confirms(browser, idp, store, members_sign_straight_in, claims):
    headers = await _sso_session(browser, idp)

    back = await _reauth(browser, idp, headers, "delete_account", **claims)

    assert location(back) == (SETTINGS, {"sso_error": ["failed"]})
    refused = await browser.post("/auth/delete-account", json={"confirm": EMAIL}, headers=headers)
    assert refused.status_code == 400


async def test_another_persons_identity_confirms_nothing(
    app, browser, idp, store, members_sign_straight_in
):
    headers = await _sso_session(browser, idp)
    async with new_browser(app) as elsewhere:
        await _sso_session(elsewhere, idp, sub="subject-2", email="bob@example.edu")

    # Alice starts a confirmation; the provider answers with Bob's identity.
    back = await _reauth(browser, idp, headers, "delete_account", sub="subject-2")

    assert location(back) == (SETTINGS, {"sso_error": ["failed"]})
    refused = await browser.post("/auth/delete-account", json={"confirm": EMAIL}, headers=headers)
    assert refused.status_code == 400


async def test_an_account_with_no_identity_cannot_start_a_confirmation(browser, store):
    await existing_user(store)
    headers = await _password_session(browser)

    response = await browser.post(
        "/auth/sso/reauth/start", json={"purpose": "delete_account"}, headers=headers
    )

    assert response.status_code == 409
    me = (await browser.get("/user/me", headers=headers)).json()
    assert me["sso_reauth_provider"] is None


async def test_a_confirmation_cannot_be_started_without_a_session(browser):
    response = await browser.post("/auth/sso/reauth/start", json={"purpose": "delete_account"})

    assert response.status_code == 401


async def test_the_return_address_of_a_confirmation_stays_inside_the_console(
    browser, idp, members_sign_straight_in
):
    headers = await _sso_session(browser, idp)
    params = await _reauth_start(browser, headers, "set_password", "https://evil.test/")

    back = await answer(browser, idp, params, auth_time=int(time.time()))

    assert location(back) == (SETTINGS, {"reauth": ["set_password"]})


# -- the account chooser -------------------------------------------------------------


async def test_the_chooser_names_the_provider_of_an_account_without_a_password(
    browser, idp, store, members_sign_straight_in
):
    await existing_user(store, "bob@elsewhere.org")
    await _password_session(browser, "bob@elsewhere.org")
    await _sso_session(browser, idp)

    accounts = (await browser.get("/auth/accounts")).json()["accounts"]

    by_email = {account["email"]: account["sso_provider"] for account in accounts}
    assert by_email == {
        "bob@elsewhere.org": None,
        EMAIL: {"id": "example-univ", "name": "Example University"},
    }


# -- linking and unlinking from Settings -------------------------------------------------


async def test_an_account_links_an_identity_with_another_email_from_settings(browser, idp, store):
    user_id = await existing_user(store, "personal@elsewhere.org")
    headers = await _password_session(browser, "personal@elsewhere.org")
    assert (await browser.get("/auth/sso/identities", headers=headers)).json() == {"identities": []}

    started = await browser.post(
        "/auth/sso/identities/link",
        json={"email": EMAIL, "password": PASSWORD, "return_to": f"{SETTINGS}?tab=account"},
        headers=headers,
    )
    assert started.status_code == 200, started.text
    params = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    assert params["login_hint"] == [EMAIL]
    back = await answer(browser, idp, params)

    assert location(back) == (SETTINGS, {"tab": ["account"], "sso_linked": ["1"]})
    assert (await store.get_user_identity(ISSUER, "subject-1"))["user_id"] == user_id
    (identity,) = (await browser.get("/auth/sso/identities", headers=headers)).json()["identities"]
    assert identity["provider_id"] == "example-univ"
    assert identity["provider_name"] == "Example University"
    assert identity["email"] == EMAIL
    assert identity["available"] is True

    # The identity now signs in to that account, whatever its own email.
    async with new_browser(browser._transport.app) as elsewhere:
        await _sso_session(elsewhere, idp)
        me = await elsewhere.post("/auth/refresh")
        assert me.status_code == 200
    assert await store.get_user_by_email(EMAIL) is None


async def test_linking_needs_the_owner_to_confirm(browser, idp, store):
    await existing_user(store, "personal@elsewhere.org")
    headers = await _password_session(browser, "personal@elsewhere.org")

    for password in (None, "not-the-password"):
        refused = await browser.post(
            "/auth/sso/identities/link",
            json={"email": EMAIL, "password": password},
            headers=headers,
        )
        assert refused.status_code == 400
        assert "set-cookie" not in refused.headers
    assert idp.requests == []


async def test_linking_an_email_no_provider_answers_for_starts_nothing(browser, store):
    await existing_user(store)
    headers = await _password_session(browser)

    response = await browser.post(
        "/auth/sso/identities/link",
        json={"email": "someone@elsewhere.org", "password": PASSWORD},
        headers=headers,
    )

    assert response.json() == {"available": False, "authorization_url": None}


async def test_an_identity_that_signs_in_to_another_account_cannot_be_linked(
    app, browser, idp, store, members_sign_straight_in
):
    async with new_browser(app) as elsewhere:
        await _sso_session(elsewhere, idp)
    await existing_user(store, "personal@elsewhere.org")
    headers = await _password_session(browser, "personal@elsewhere.org")

    started = await browser.post(
        "/auth/sso/identities/link", json={"email": EMAIL, "password": PASSWORD}, headers=headers
    )
    back = await answer(browser, idp, parse_qs(urlsplit(started.json()["authorization_url"]).query))

    assert location(back) == (SETTINGS, {"sso_error": ["identity_in_use"]})
    owner = await store.get_user_by_email(EMAIL)
    assert (await store.get_user_identity(ISSUER, "subject-1"))["user_id"] == owner["id"]


async def test_an_account_unlinks_its_identity_with_its_password(browser, idp, store):
    user_id = await existing_user(store)
    headers = await _password_session(browser)
    started = await browser.post(
        "/auth/sso/identities/link", json={"email": EMAIL, "password": PASSWORD}, headers=headers
    )
    await answer(browser, idp, parse_qs(urlsplit(started.json()["authorization_url"]).query))
    (identity,) = await store.list_user_identities(user_id)
    unlink = f"/auth/sso/identities/{identity['id']}/unlink"

    assert (await browser.post(unlink, json={}, headers=headers)).status_code == 400
    wrong = await browser.post(unlink, json={"password": "not-the-password"}, headers=headers)
    assert wrong.status_code == 400
    assert await store.list_user_identities(user_id) != []

    done = await browser.post(unlink, json={"password": PASSWORD}, headers=headers)

    assert done.status_code == 200, done.text
    assert await store.list_user_identities(user_id) == []
    # Unlinking again finds nothing. Each confirmation above counted against the
    # sign-in limits, as a password guess anywhere does; start the count afresh.
    login_rate_limit.reset_login_rate_limit_state()
    again = await browser.post(unlink, json={"password": PASSWORD}, headers=headers)
    assert again.status_code == 404


async def test_an_account_cannot_unlink_another_accounts_identity(
    app, browser, idp, store, members_sign_straight_in
):
    async with new_browser(app) as elsewhere:
        await _sso_session(elsewhere, idp)
    theirs = (await store.get_user_identity(ISSUER, "subject-1"))["id"]
    await existing_user(store, "personal@elsewhere.org")
    headers = await _password_session(browser, "personal@elsewhere.org")

    response = await browser.post(
        f"/auth/sso/identities/{theirs}/unlink", json={"password": PASSWORD}, headers=headers
    )

    assert response.status_code == 404
    assert await store.get_user_identity(ISSUER, "subject-1") is not None


async def test_the_only_way_in_cannot_be_unlinked(browser, idp, store, members_sign_straight_in):
    headers = await _sso_session(browser, idp)
    identity = await store.get_user_identity(ISSUER, "subject-1")
    unlink = f"/auth/sso/identities/{identity['id']}/unlink"

    # Not without confirming at the provider...
    assert (await browser.post(unlink, json={}, headers=headers)).status_code == 400
    # ...and not after it either, because nothing else signs in to this account.
    await _reauth(browser, idp, headers, "unlink_sso")
    refused = await browser.post(unlink, json={}, headers=headers)

    assert refused.status_code == 409
    assert "Set a password" in refused.json()["detail"]
    assert await store.get_user_identity(ISSUER, "subject-1") is not None

    # With a password set, the same account may unlink, confirming with the password.
    await _reauth(browser, idp, headers, "set_password")
    await browser.post(
        "/user/change-password", json={"new_password": NEW_PASSWORD}, headers=headers
    )
    done = await browser.post(unlink, json={"password": NEW_PASSWORD}, headers=headers)
    assert done.status_code == 200, done.text
    assert await store.get_user_identity(ISSUER, "subject-1") is None


async def test_an_account_whose_only_proof_is_its_phone_sets_a_password_first(
    browser, store, auth_backend
):
    user_id = await existing_user(store)
    headers = await _password_session(browser)
    _store, _log_store, db_logger, _backend = auth_backend
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET phone = '+8613800138000', phone_verified = TRUE WHERE id = $1",
            user_id,
        )
    await store.update_user_fields(user_id, password_hash=None)

    link = await browser.post("/auth/sso/identities/link", json={"email": EMAIL}, headers=headers)
    unlink = await browser.post("/auth/sso/identities/anything/unlink", json={}, headers=headers)

    for response in (link, unlink):
        assert response.status_code == 400
        assert "Set a password" in response.json()["detail"]


async def test_a_provider_that_is_no_longer_offered_is_marked_in_the_list(
    browser, idp, sso, store, members_sign_straight_in
):
    headers = await _sso_session(browser, idp)

    sso.configure(enabled=False)
    (identity,) = (await browser.get("/auth/sso/identities", headers=headers)).json()["identities"]

    assert identity["available"] is False
    assert identity["provider_name"] == "example-univ"


# -- an administrator removes a link ---------------------------------------------------


async def _admin_session(browser, store) -> dict[str, str]:
    admin_id = await existing_user(store, "admin@elsewhere.org")
    await store.update_user_fields(admin_id, role="admin")
    return await _password_session(browser, "admin@elsewhere.org")


async def test_an_administrator_sees_and_unlinks_a_users_identity(
    app, browser, idp, store, auth_backend, members_sign_straight_in
):
    async with new_browser(app) as member:
        member_headers = await _sso_session(member, idp)
        user = await store.get_user_by_email(EMAIL)
        admin = await _admin_session(browser, store)

        detail = await browser.get(f"/admin/users/{user['id']}/detail", headers=admin)
        assert detail.status_code == 200, detail.text
        (identity,) = detail.json()["sso_identities"]
        assert identity["provider_name"] == "Example University"
        assert identity["email"] == EMAIL
        unlink = f"/admin/users/{user['id']}/sso-identities/{identity['id']}/unlink"

        # The administrator's own password, as for the other changes to how an
        # account signs in.
        assert (await browser.post(unlink, json={}, headers=admin)).status_code == 400
        wrong = await browser.post(unlink, json={"password": "not-it"}, headers=admin)
        assert wrong.status_code == 400
        assert await store.get_user_identity(ISSUER, "subject-1") is not None

        done = await browser.post(unlink, json={"password": PASSWORD}, headers=admin)
        assert done.status_code == 200, done.text
        assert await store.get_user_identity(ISSUER, "subject-1") is None

        # Only the link goes: the member's session is untouched.
        assert (await member.get("/user/me", headers=member_headers)).status_code == 200
        gone = await browser.post(unlink, json={"password": PASSWORD}, headers=admin)
        assert gone.status_code == 404

    _store, _log_store, db_logger, _backend = auth_backend
    async with db_logger.pool.acquire() as conn:
        actions = await conn.fetch(
            "SELECT action FROM admin_audit_log WHERE target_user_id = $1 ORDER BY id", user["id"]
        )
    assert [row["action"] for row in actions][-1] == "unlink_sso_identity"


async def test_only_an_administrator_may_unlink_for_someone_else(
    browser, idp, store, members_sign_straight_in
):
    headers = await _sso_session(browser, idp)
    user = await store.get_user_by_email(EMAIL)
    identity = await store.get_user_identity(ISSUER, "subject-1")

    response = await browser.post(
        f"/admin/users/{user['id']}/sso-identities/{identity['id']}/unlink",
        json={"password": PASSWORD},
        headers=headers,
    )

    assert response.status_code in (401, 403)
    assert await store.get_user_identity(ISSUER, "subject-1") is not None


# -- the callback keeps flows apart -----------------------------------------------------


async def test_a_sign_in_flow_never_links_or_confirms(
    browser, idp, store, members_sign_straight_in
):
    # A flow started as a sign-in has no account behind it, so its answer can
    # only ever sign in: the same answer to a link flow would link.
    headers = await _sso_session(browser, idp)
    params = await _reauth_start(browser, headers, "delete_account")
    idp.id_token = sign_id_token(id_token_claims(params["nonce"][0], auth_time=int(time.time())))

    # Replaying the state on another provider's callback address is refused.
    other = await browser.get(
        CALLBACK.replace("example-univ", "other"),
        params={"code": "code-1", "state": params["state"][0]},
    )

    assert location(other) == ("/login", {"sso_error": ["expired"]})
    refused = await browser.post("/auth/delete-account", json={"confirm": EMAIL}, headers=headers)
    assert refused.status_code == 400
