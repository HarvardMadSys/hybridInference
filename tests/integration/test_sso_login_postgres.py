"""Single sign-on over HTTP, against the real store and an in-process provider.

Each test walks the browser's path: start a sign-in, let the provider answer,
follow the callback. The provider signs real ID tokens; the store is Postgres,
so the session the callback issues is one ``/auth/refresh`` really accepts.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from serving.config.settings import get_settings
from serving.utils import sso_rate_limit
from tests.fixtures.oidc_provider import CLIENT_ID, ISSUER, id_token_claims, sign_id_token
from tests.fixtures.sso_app import (
    CALLBACK,
    EMAIL,
    PASSWORD,
    answer as _answer,
    existing_user as _existing_user,
    location as _location,
    new_browser as _browser,
    sign_in as _sign_in,
    start as _start,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _login_events(auth_backend) -> list[dict[str, Any]]:
    _store, _log_store, db_logger, _backend = auth_backend
    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT email, outcome, failure_reason, user_id FROM login_events ORDER BY id"
        )
    return [dict(row) for row in rows]


# -- starting a sign-in -----------------------------------------------------------


async def test_discover_starts_a_sign_in_at_the_provider_for_the_emails_domain(browser, idp):
    response = await browser.post(
        "/auth/sso/discover", json={"email": "Alice@Example.edu", "return_to": "/dashboard/keys"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    url = urlsplit(body["authorization_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    params = parse_qs(url.query)
    assert params["redirect_uri"] == ["http://test/auth/sso/example-univ/callback"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["login_hint"] == ["alice@example.edu"]
    assert params["code_challenge_method"] == ["S256"]
    # The browser is bound to the flow by a cookie only the single sign-on routes see.
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("refresh_token_sso_flow=")
    assert "HttpOnly" in cookie and "Path=/auth/sso" in cookie and "SameSite=lax" in cookie


async def test_an_email_no_provider_answers_for_is_not_offered_single_sign_on(browser, idp, store):
    # Whether the address has an account here makes no difference to the answer.
    await _existing_user(store, "someone@elsewhere.org")

    for email in ("someone@elsewhere.org", "nobody@elsewhere.org"):
        response = await browser.post("/auth/sso/discover", json={"email": email})
        assert response.status_code == 200
        assert response.json() == {"available": False, "authorization_url": None}
        assert "set-cookie" not in response.headers
    assert idp.requests == []


async def test_a_provider_that_cannot_be_reached_is_reported_as_unavailable(browser, idp):
    idp.unreachable = True

    response = await browser.post("/auth/sso/discover", json={"email": EMAIL})

    assert response.status_code == 503


async def test_a_disabled_provider_is_not_offered(browser, sso):
    sso.configure(enabled=False)

    response = await browser.post("/auth/sso/discover", json={"email": EMAIL})

    assert response.json()["available"] is False


async def test_starts_from_one_address_are_limited(browser, monkeypatch):
    monkeypatch.setattr(sso_rate_limit, "STARTS_PER_HOUR", 2)

    statuses = [
        (await browser.post("/auth/sso/discover", json={"email": EMAIL})).status_code
        for _ in range(3)
    ]

    assert statuses == [200, 200, 429]


async def test_the_direct_link_goes_straight_to_the_provider(browser, idp):
    response = await browser.get(
        "/auth/sso/example-univ/start", params={"return_to": "/dashboard/keys"}
    )

    path, params = _location(response)
    assert path == "/realms/main/authorize"
    assert "login_hint" not in params
    assert "refresh_token_sso_flow=" in response.headers["set-cookie"]

    # The flow it started completes like one started from the sign-in page.
    done = await _answer(browser, idp, params)
    assert _location(done) == ("/login", {"sso_error": ["no_account"]})


async def test_the_direct_link_for_an_unknown_provider_returns_to_the_sign_in_page(browser):
    response = await browser.get("/auth/sso/no-such-provider/start")

    assert _location(response) == ("/login", {"sso_error": ["unavailable"]})


# -- a first sign-in --------------------------------------------------------------


async def test_a_first_sign_in_creates_the_account_and_signs_it_in(
    browser, idp, sso, store, auth_backend
):
    sso.configure(auto_create=True, require_approval=False, default_role="pro")

    response = await _answer(
        browser, idp, await _start(browser, return_to="/dashboard/keys"), name="Alice Example"
    )

    assert _location(response) == ("/login", {"sso": ["complete"], "next": ["/dashboard/keys"]})
    user = await store.get_user_by_email(EMAIL)
    assert user["status"] == "active" and user["role"] == "pro"
    assert user["email_verified"] is True
    assert user["password_hash"] is None
    assert user["user_name"] == "Alice Example"
    identity = await store.get_user_identity(ISSUER, "subject-1")
    assert identity["user_id"] == user["id"]
    assert identity["provider_id"] == "example-univ"
    assert identity["last_login_at"] is not None

    # The session the callback issued is a real one: the console's first
    # request after the redirect gets an access token for the new account.
    refreshed = await browser.post("/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    me = await browser.get(
        "/user/me", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL
    assert me.json()["has_password"] is False

    events = await _login_events(auth_backend)
    assert events[-1] == {
        "email": EMAIL,
        "outcome": "success",
        "failure_reason": None,
        "user_id": user["id"],
    }


async def test_a_second_sign_in_uses_the_link_not_the_email(browser, idp, sso, store):
    sso.configure(auto_create=True, require_approval=False)
    await _sign_in(browser, idp)

    # The provider now reports another address, unverified: the identity is the same.
    response = await _sign_in(browser, idp, email="renamed@elsewhere.org", email_verified=False)

    assert _location(response)[1]["sso"] == ["complete"]
    assert await store.get_user_by_email("renamed@elsewhere.org") is None


async def test_a_first_sign_in_waits_for_approval_by_default(
    browser, idp, sso, store, auth_backend
):
    sso.configure(auto_create=True)

    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso_error": ["pending"]})
    user = await store.get_user_by_email(EMAIL)
    assert user["status"] == "pending_approval"
    assert (await store.get_user_identity(ISSUER, "subject-1"))["user_id"] == user["id"]
    assert "refresh_token=" not in response.headers.get("set-cookie", "")
    assert (await _login_events(auth_backend))[-1]["failure_reason"] == "sso_pending"

    # Until an administrator approves it the answer stays the same, and no
    # second account is made.
    again = await _sign_in(browser, idp)
    assert _location(again) == ("/login", {"sso_error": ["pending"]})

    await store.update_user_fields(user["id"], status="active")
    approved = await _sign_in(browser, idp)
    assert _location(approved)[1]["sso"] == ["complete"]


async def test_without_automatic_creation_an_unknown_member_is_refused(browser, idp, store):
    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso_error": ["no_account"]})
    assert await store.get_user_by_email(EMAIL) is None


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"email_verified": False}, id="not verified"),
        pytest.param({"email_verified": None}, id="provider did not say"),
        pytest.param({"email": "alice@elsewhere.org"}, id="another domain"),
    ],
)
async def test_an_email_the_provider_cannot_vouch_for_is_refused(browser, idp, sso, store, claims):
    sso.configure(auto_create=True, require_approval=False)

    response = await _sign_in(browser, idp, **claims)

    assert _location(response) == ("/login", {"sso_error": ["email_untrusted"]})
    assert await store.get_user_identity(ISSUER, "subject-1") is None


async def test_an_address_on_the_administrator_list_is_never_created(
    browser, idp, sso, store, monkeypatch
):
    sso.configure(auto_create=True, require_approval=False)
    monkeypatch.setattr(get_settings(), "admin_emails", EMAIL)

    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso_error": ["no_account"]})
    assert await store.get_user_by_email(EMAIL) is None


# -- an account that already exists ------------------------------------------------


async def test_an_existing_account_is_linked_only_after_its_owner_signs_in(
    browser, idp, store, auth_backend
):
    user_id = await _existing_user(store)

    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso": ["link"]})
    assert "refresh_token_sso_link=" in response.headers["set-cookie"]
    assert "refresh_token=" not in response.headers["set-cookie"]
    assert await store.get_user_identity(ISSUER, "subject-1") is None

    # The sign-in page reads who is waiting from the cookie, not from a URL.
    pending = await browser.get("/auth/sso/link")
    assert pending.json() == {
        "provider_id": "example-univ",
        "provider_name": "Example University",
        "email": EMAIL,
    }

    # Accepting needs the account's owner to be signed in.
    assert (await browser.post("/auth/sso/link/confirm")).status_code == 401
    login = await browser.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    confirmed = await browser.post("/auth/sso/link/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert (await store.get_user_identity(ISSUER, "subject-1"))["user_id"] == user_id

    # Nothing is left waiting, and the next sign-in goes straight through.
    assert (await browser.get("/auth/sso/link")).status_code == 404
    assert (await browser.post("/auth/sso/link/confirm", headers=headers)).status_code == 404
    assert _location(await _sign_in(browser, idp))[1]["sso"] == ["complete"]


async def test_an_identity_cannot_be_accepted_by_an_account_with_another_email(
    app, browser, idp, store
):
    await _existing_user(store)
    await _existing_user(store, "bob@example.edu")
    await _sign_in(browser, idp)

    # On the same browser, someone else signs in to their own account.
    login = await browser.post(
        "/auth/login", json={"email": "bob@example.edu", "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    refused = await browser.post("/auth/sso/link/confirm", headers=headers)

    assert refused.status_code == 409
    assert await store.get_user_identity(ISSUER, "subject-1") is None
    # The identity still waits for its own account.
    assert (await browser.get("/auth/sso/link")).status_code == 200


async def test_a_waiting_identity_belongs_to_the_browser_that_left_it(app, browser, idp, store):
    await _existing_user(store)
    await _sign_in(browser, idp)

    async with _browser(app) as other:
        assert (await other.get("/auth/sso/link")).status_code == 404
        login = await other.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (await other.post("/auth/sso/link/confirm", headers=headers)).status_code == 404

    assert await store.get_user_identity(ISSUER, "subject-1") is None


async def test_declining_drops_the_waiting_identity(browser, idp, store):
    await _existing_user(store)
    await _sign_in(browser, idp)

    declined = await browser.post("/auth/sso/link/cancel")

    assert declined.status_code == 200
    assert (await browser.get("/auth/sso/link")).status_code == 404
    assert await store.get_user_identity(ISSUER, "subject-1") is None


async def test_automatic_linking_signs_in_without_asking(browser, idp, sso, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "signup_require_email_verification", True)
    sso.configure(auto_link=True)
    user_id = await _existing_user(store)

    response = await _sign_in(browser, idp)

    assert _location(response)[1]["sso"] == ["complete"]
    assert (await store.get_user_identity(ISSUER, "subject-1"))["user_id"] == user_id


async def test_automatic_linking_is_off_where_email_verification_is_not_required(
    browser, idp, sso, store
):
    # The test deployment does not require verification, so an account's
    # "verified" flag proves nothing about who registered it.
    sso.configure(auto_link=True)
    await _existing_user(store)

    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso": ["link"]})


@pytest.mark.parametrize(
    ("status", "code"),
    [("suspended", "suspended"), ("deleted", "inactive"), ("pending_approval", "pending")],
)
async def test_an_account_that_is_not_active_is_never_linked_or_replaced(
    browser, idp, sso, store, status, code
):
    sso.configure(auto_create=True, auto_link=True)
    await _existing_user(store, status=status)

    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso_error": [code]})
    assert await store.get_user_identity(ISSUER, "subject-1") is None


async def test_a_suspended_account_cannot_sign_in_through_its_link(browser, idp, sso, store):
    sso.configure(auto_create=True, require_approval=False)
    await _sign_in(browser, idp)
    user = await store.get_user_by_email(EMAIL)
    await store.suspend_user(user["id"])

    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso_error": ["suspended"]})


# -- answers that are refused -------------------------------------------------------


async def test_a_flow_completes_once(browser, idp, sso):
    sso.configure(auto_create=True, require_approval=False)
    params = await _start(browser)
    first = await _answer(browser, idp, params)
    assert _location(first)[1]["sso"] == ["complete"]

    replayed = await _answer(browser, idp, params)

    assert _location(replayed) == ("/login", {"sso_error": ["expired"]})


async def test_a_flow_completes_only_in_the_browser_that_started_it(app, browser, idp, sso, store):
    sso.configure(auto_create=True, require_approval=False)
    params = await _start(browser)

    async with _browser(app) as attacker:
        response = await _answer(attacker, idp, params)

    assert _location(response) == ("/login", {"sso_error": ["expired"]})
    assert await store.get_user_by_email(EMAIL) is None


async def test_a_flow_completes_only_at_the_provider_it_started_at(browser, idp, sso, store):
    sso.providers = [
        sso.provider(auto_create=True, require_approval=False),
        sso.provider(id="other", domains=["other.edu"]),
    ]
    params = await _start(browser)
    idp.id_token = sign_id_token(id_token_claims(params["nonce"][0]))

    response = await browser.get(
        "/auth/sso/other/callback", params={"code": "code-1", "state": params["state"][0]}
    )

    assert _location(response) == ("/login", {"sso_error": ["expired"]})
    assert await store.get_user_by_email(EMAIL) is None


async def test_a_callback_without_a_flow_is_refused(browser, idp):
    for params in ({}, {"code": "code-1"}, {"code": "code-1", "state": "never-issued"}):
        response = await browser.get(CALLBACK, params=params)
        assert _location(response) == ("/login", {"sso_error": ["expired"]})
    assert idp.requests == []


async def test_cancelling_at_the_provider_returns_to_the_sign_in_page(browser, idp):
    params = await _start(browser)

    response = await browser.get(
        CALLBACK,
        params={
            "error": "access_denied",
            "error_description": "the user said no <script>",
            "state": params["state"][0],
        },
    )

    assert _location(response) == ("/login", {"sso_error": ["cancelled"]})
    # Nothing the provider said about the error travels on.
    assert "script" not in response.headers["location"]


async def test_a_token_that_does_not_validate_is_a_failed_sign_in(browser, idp, sso, store):
    sso.configure(auto_create=True, require_approval=False)
    params = await _start(browser)

    response = await _answer(browser, idp, {**params, "nonce": ["another-flow"]})

    assert _location(response) == ("/login", {"sso_error": ["failed"]})
    assert await store.get_user_by_email(EMAIL) is None


async def test_a_return_address_outside_the_console_is_replaced(browser, idp, sso):
    sso.configure(auto_create=True, require_approval=False)

    for return_to in ("https://evil.test/", "//evil.test", "/\\evil.test", "dashboard"):
        params = await _start(browser, return_to=return_to)
        response = await _answer(browser, idp, params)
        assert _location(response)[1]["next"] == ["/dashboard"]


async def test_a_browser_holding_the_maximum_of_accounts_cannot_add_another(
    browser, idp, sso, store, monkeypatch
):
    from serving.auth import browser_sessions

    monkeypatch.setattr(browser_sessions, "MAX_ACCOUNTS_PER_BROWSER", 1)
    await _existing_user(store, "bob@elsewhere.org")
    await browser.post("/auth/login", json={"email": "bob@elsewhere.org", "password": PASSWORD})
    sso.configure(auto_create=True, require_approval=False)

    response = await _sign_in(browser, idp)

    assert _location(response) == ("/login", {"sso_error": ["browser_full"]})
