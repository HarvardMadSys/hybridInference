"""A member signs in through single sign-on only, over HTTP and against the real store.

The organization owns the account of a member, so its identity provider
decides who gets in: the event that makes an account a member takes its
password, and no route gives it one again. A platform administrator is the
exception, because that account must get in when the provider is down.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from serving.config.settings import get_settings
from tests.fixtures.sso_app import (
    EMAIL,
    PASSWORD,
    answer,
    existing_user,
    location,
    new_browser,
    start,
)
from tests.integration import test_organizations_postgres as shared
from tests.integration.test_organizations_postgres import (
    _make_organization,
    _member,
    _password_session,
    _platform_admin,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# The fixtures of the organization tests, under the names pytest finds them by.
organization_routes = shared.organization_routes
clean_organizations = shared.clean_organizations
members_sign_straight_in = shared.members_sign_straight_in

WRONG_PASSWORD = "Invalid email or password"


async def _reset_link(store, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    await store.create_reset_token(
        token=token, user_id=user_id, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    return token


async def _link_by_hand(app, idp, account_email: str = EMAIL):
    """An account with a password accepts the identity the provider offers for it."""
    browser = new_browser(app)
    waiting = await answer(browser, idp, await start(browser, account_email), email=account_email)
    assert location(waiting)[1].get("sso") == ["link"]
    session = await _password_session(browser, account_email)
    confirmed = await browser.post("/auth/sso/link/confirm", headers=session)
    assert confirmed.status_code == 200, confirmed.text
    return browser, session


async def _login(app, email: str = EMAIL, password: str = PASSWORD):
    return await new_browser(app).post("/auth/login", json={"email": email, "password": password})


async def _password_hash(pool, user_id: str) -> str | None:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT password_hash FROM users WHERE id = $1", user_id)


# -- the join takes the password ---------------------------------------------------


async def test_a_link_that_makes_a_member_takes_the_password_and_the_reset_links(
    app, idp, store, clean_organizations
):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    account_id = await existing_user(store)
    link = await _reset_link(store, account_id)

    await _link_by_hand(app, idp)

    assert await store.get_organization_membership(account_id) is not None
    assert await _password_hash(clean_organizations, account_id) is None
    assert await store.get_reset_token(link) is None
    refused = await _login(app)
    assert refused.status_code == 401
    assert refused.json()["detail"] == WRONG_PASSWORD


async def test_an_attach_takes_the_passwords_of_the_accounts_it_makes_members(
    app, idp, store, clean_organizations
):
    """The account linked its identity when the connection belonged to no organization."""
    account_id = await existing_user(store)
    await _link_by_hand(app, idp)
    assert await _password_hash(clean_organizations, account_id) is not None
    link = await _reset_link(store, account_id)
    root, headers, _ = await _platform_admin(app, store)

    made = await _make_organization(root, headers)

    assert made["members_added"] == 1
    assert await _password_hash(clean_organizations, account_id) is None
    assert await store.get_reset_token(link) is None
    assert (await _login(app)).status_code == 401


async def test_a_link_through_a_connection_of_no_organization_keeps_the_password(
    app, idp, store, clean_organizations
):
    account_id = await existing_user(store)

    await _link_by_hand(app, idp)

    assert await store.get_organization_membership(account_id) is None
    assert await _password_hash(clean_organizations, account_id) is not None
    assert (await _login(app)).status_code == 200


@pytest.mark.parametrize("by", ["role", "admin_emails"])
async def test_a_platform_administrator_keeps_its_password_as_a_member(
    app, idp, store, clean_organizations, monkeypatch, by
):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    account_id = await existing_user(store)
    if by == "role":
        await store.update_user_fields(account_id, role="admin")
    else:
        monkeypatch.setattr(get_settings(), "admin_emails", EMAIL.upper())

    browser, session = await _link_by_hand(app, idp)

    assert await store.get_organization_membership(account_id) is not None
    assert await _password_hash(clean_organizations, account_id) is not None
    assert (await _login(app)).status_code == 200
    profile = await browser.get("/user/me", headers=session)
    assert profile.json()["sso_only"] is False


# -- no password signs a member in -------------------------------------------------


async def test_a_password_that_stayed_on_a_member_does_not_sign_in(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    """As for an account that was a platform administrator when it joined, and is none now."""
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    _, _, member_id = await _member(app, idp, store)
    async with clean_organizations.acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = (SELECT password_hash FROM users WHERE email = $2) "
            "WHERE id = $1",
            member_id,
            "root@elsewhere.org",
        )

    refused = await _login(app)

    assert refused.status_code == 401
    assert refused.json()["detail"] == WRONG_PASSWORD
    async with clean_organizations.acquire() as conn:
        reason = await conn.fetchval(
            "SELECT failure_reason FROM login_events WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
            member_id,
        )
    assert reason == "sso_only"


# -- and no route gives it one -----------------------------------------------------


async def test_a_member_is_told_on_its_profile_and_sets_no_password(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    alice, alice_headers, alice_id = await _member(app, idp, store)

    profile = await alice.get("/user/me", headers=alice_headers)
    changed = await alice.post(
        "/user/change-password",
        json={"old_password": "", "new_password": "A-new-passw0rd-of-mine"},
        headers=alice_headers,
    )

    assert profile.json()["sso_only"] is True
    assert profile.json()["has_password"] is False
    assert changed.status_code == 403, changed.text
    assert "single sign-on" in changed.json()["detail"]
    assert await _password_hash(clean_organizations, alice_id) is None


async def test_a_reset_link_is_not_sent_to_a_member_and_an_older_one_sets_nothing(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    _, _, alice_id = await _member(app, idp, store)

    asked = await new_browser(app).post("/auth/forgot-password", json={"email": EMAIL})

    assert asked.status_code == 200
    assert "If an account exists" in asked.json()["message"]
    async with clean_organizations.acquire() as conn:
        links = await conn.fetchval(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = $1", alice_id
        )
    assert links == 0

    # A link from somewhere else (a write that raced the join) sets nothing.
    link = await _reset_link(store, alice_id)
    reset = await new_browser(app).post(
        "/auth/reset-password", json={"token": link, "new_password": "A-new-passw0rd-of-mine"}
    )
    assert reset.status_code == 403, reset.text
    assert await _password_hash(clean_organizations, alice_id) is None
    spent = await store.redeem_reset_token(link, password_hash="not-a-real-hash")
    assert spent.outcome == "sso_only"
    assert await _password_hash(clean_organizations, alice_id) is None


async def test_an_account_in_no_organization_is_not_sso_only(app, store):
    await existing_user(store)
    browser = new_browser(app)
    session = await _password_session(browser, EMAIL)

    profile = await browser.get("/user/me", headers=session)

    assert profile.json()["sso_only"] is False
