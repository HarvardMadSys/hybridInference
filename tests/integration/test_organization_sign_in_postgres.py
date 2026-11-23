"""A member signs in through single sign-on only, over HTTP and against the real store.

The organization owns the account of a member, so its identity provider
decides who gets in: the event that makes an account a member takes its
password, and no route gives it one again. A platform administrator is the
exception, because that account must get in when the provider is down.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from serving.auth import reset_password as reset_tool
from serving.config.settings import get_settings
from serving.servers.routers import auth_routes
from serving.utils import email as email_utils
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


async def _set_role(pool, store, user_id: str, role: str) -> None:
    """Give an account a platform role, and drop the copy of its row the store keeps."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET role = $2 WHERE id = $1", user_id, role)
    await store._cache.delete(store._user_key(user_id))


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
    alice, alice_headers, _ = await _member(app, idp, store)
    profile = (await alice.get("/user/me", headers=alice_headers)).json()

    assert refused.status_code == 401
    assert refused.json()["detail"] == WRONG_PASSWORD
    # For the console the account has no password: it confirms where it signs in.
    assert (profile["has_password"], profile["sso_only"]) == (False, True)
    assert profile["sso_reauth_provider"] == {"id": "example-univ", "name": "Example University"}
    async with clean_organizations.acquire() as conn:
        reason = await conn.fetchval(
            "SELECT failure_reason FROM login_events "
            "WHERE user_id = $1 AND outcome = 'failure' ORDER BY id DESC LIMIT 1",
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
    renamed = await alice.patch("/user/profile", json={"user_name": "Alice"}, headers=alice_headers)
    assert renamed.json()["sso_only"] is True
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


async def test_no_write_gives_a_member_a_password(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    """Decided where the password is written, under the lock a join waits for."""
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    _, _, alice_id = await _member(app, idp, store)
    outsider_id = await existing_user(store, "someone@elsewhere.org")

    assert await store.set_user_password(alice_id, "hash-for-a-member") is False
    assert await store.set_user_password(outsider_id, "hash-for-an-outsider") is True
    assert await store.set_user_password("no-such-account", "hash") is False

    assert await _password_hash(clean_organizations, alice_id) is None
    assert await _password_hash(clean_organizations, outsider_id) == "hash-for-an-outsider"
    assert (await store.get_user_by_id(outsider_id))["password_hash"] == "hash-for-an-outsider"


async def test_the_command_line_reset_gives_a_member_no_password(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    _, _, alice_id = await _member(app, idp, store)
    tool = reset_tool.AccountStore(clean_organizations)

    with pytest.raises(reset_tool.ResetPasswordError, match="single sign-on"):
        await reset_tool.reset_password(tool, EMAIL, "A-new-passw0rd-of-mine")
    # The platform administrator of these tests is no member: its reset goes through.
    await reset_tool.reset_password(tool, "root@elsewhere.org", "A-new-passw0rd-of-mine")

    assert await _password_hash(clean_organizations, alice_id) is None
    assert (await _login(app, "root@elsewhere.org", "A-new-passw0rd-of-mine")).status_code == 200


# -- and a member is told where to sign in -----------------------------------------


async def test_a_failed_password_sign_in_names_the_sign_on_of_the_organization(
    app, idp, store, sso, clean_organizations, members_sign_straight_in
):
    """By the domain of the address alone: the same answer whether or not an account exists."""
    # The name is the connection's, which the sign-in pages show; not the organization's.
    sso.providers[0]["display_name"] = "Example Sign-On"
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    _, _, member_id = await _member(app, idp, store)
    await existing_user(store, "carol@example.edu")
    await existing_user(store, "someone@elsewhere.org")
    told = {"detail": WRONG_PASSWORD, "sso_sign_in": {"provider_name": "Example Sign-On"}}

    a_member = await _login(app)
    nobody = await _login(app, "nobody@example.edu")
    a_wrong_password = await _login(app, "carol@example.edu", "not-her-password")
    the_right_password = await _login(app, "carol@example.edu")
    another_domain = await _login(app, "someone@elsewhere.org", "not-the-password")
    # A password that stayed on a member, and is the right one.
    async with clean_organizations.acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = (SELECT password_hash FROM users WHERE email = $2) "
            "WHERE id = $1",
            member_id,
            "carol@example.edu",
        )
    a_member_with_its_old_password = await _login(app)

    for refused in (a_member, nobody, a_wrong_password, a_member_with_its_old_password):
        assert (refused.status_code, refused.json()) == (401, told)
    # An account of the domain that is no member still signs in with its password.
    assert the_right_password.status_code == 200
    assert (another_domain.status_code, another_domain.json()) == (401, {"detail": WRONG_PASSWORD})

    # A connection that is turned off signs nobody in, so the refusal names none.
    sso.providers[0]["enabled"] = False
    turned_off = await _login(app, "nobody@example.edu")
    assert (turned_off.status_code, turned_off.json()) == (401, {"detail": WRONG_PASSWORD})


async def test_a_connection_of_no_organization_adds_nothing_to_the_refusal(app, store, sso):
    refused = await _login(app, "nobody@example.edu")

    assert (refused.status_code, refused.json()) == (401, {"detail": WRONG_PASSWORD})


async def test_a_member_that_asks_for_a_reset_link_is_mailed_how_it_signs_in(
    app, idp, store, clean_organizations, members_sign_straight_in, monkeypatch
):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers, name="Example College")
    _, _, alice_id = await _member(app, idp, store)
    await existing_user(store, "someone@elsewhere.org")
    explained, reset = MagicMock(), MagicMock()
    monkeypatch.setattr(auth_routes, "is_email_enabled", lambda: True)
    monkeypatch.setattr(auth_routes, "send_sso_sign_in_email", explained)
    monkeypatch.setattr(email_utils, "send_password_reset_email", reset)

    for_the_member = await new_browser(app).post("/auth/forgot-password", json={"email": EMAIL})
    for_another = await new_browser(app).post(
        "/auth/forgot-password", json={"email": "someone@elsewhere.org"}
    )
    for_nobody = await new_browser(app).post(
        "/auth/forgot-password", json={"email": "nobody@example.edu"}
    )

    # One answer for every address; the mail differs, and only its owner reads it.
    assert for_the_member.status_code == 200
    assert for_the_member.json() == for_another.json() == for_nobody.json()
    # With the name of the organization, to the member alone: an address of the
    # domain that has no account gets no mail.
    explained.assert_called_once_with(EMAIL, "Example College")
    assert [call.args[0] for call in reset.call_args_list] == ["someone@elsewhere.org"]
    async with clean_organizations.acquire() as conn:
        links = await conn.fetchval(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = $1", alice_id
        )
    assert links == 0


async def test_an_account_is_told_before_a_link_that_it_will_have_no_password(
    app, idp, store, clean_organizations, monkeypatch
):
    root, headers, root_id = await _platform_admin(app, store)
    carol_id = await existing_user(store, "carol@example.edu")
    carol = new_browser(app)
    waiting = await answer(
        carol, idp, await start(carol, "carol@example.edu"), email="carol@example.edu", sub="s-c"
    )
    assert location(waiting)[1].get("sso") == ["link"]
    session = await _password_session(carol, "carol@example.edu")

    # The connection belongs to no organization yet: a link changes no sign-in.
    before = (await carol.get("/auth/sso/link")).json()
    offered_before = (await carol.get("/auth/sso/identities", headers=session)).json()
    await _make_organization(root, headers)
    after = (await carol.get("/auth/sso/link")).json()
    offered = (await carol.get("/auth/sso/identities", headers=session)).json()
    to_the_administrator = (await root.get("/auth/sso/identities", headers=headers)).json()

    assert (before["sso_only_after_link"], offered_before["sso_only_provider"]) == (False, None)
    assert after["sso_only_after_link"] is True
    assert offered["sso_only_provider"] == {"id": "example-univ", "name": "Example University"}
    # An account of another domain is offered nothing.
    assert to_the_administrator["sso_only_provider"] is None

    # A platform administrator keeps its password, so its own page offers no warning. The
    # identity that waits says the same as before: it is read before the owner signs in, and
    # says nothing about the account.
    await _set_role(clean_organizations, store, carol_id, "admin")
    as_an_administrator = (await carol.get("/auth/sso/identities", headers=session)).json()
    still_waiting = (await carol.get("/auth/sso/link")).json()
    assert as_an_administrator["sso_only_provider"] is None
    assert still_waiting == after
    await _set_role(clean_organizations, store, carol_id, "free")
    # The same for an address of ``ADMIN_EMAILS``, whatever its role is now.
    with monkeypatch.context() as patched:
        patched.setattr(get_settings(), "admin_emails", "CAROL@example.edu")
        named = (await carol.get("/auth/sso/identities", headers=session)).json()
    assert named["sso_only_provider"] is None
    assert (await carol.get("/auth/sso/identities", headers=session)).json() == offered

    confirmed = await carol.post("/auth/sso/link/confirm", headers=session)
    assert confirmed.status_code == 200, confirmed.text
    # A member already: there is nothing left to warn about.
    linked = (await carol.get("/auth/sso/identities", headers=session)).json()
    assert linked["sso_only_provider"] is None
    assert await _password_hash(clean_organizations, carol_id) is None
    assert root_id != carol_id
