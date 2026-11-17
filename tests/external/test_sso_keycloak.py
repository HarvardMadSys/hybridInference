"""Single sign-on against a real Keycloak: the gateway's own HTTP surface, a live provider.

The in-process provider in ``tests/fixtures/oidc_provider.py`` proves the
client's checks; this proves the client and a real OpenID Connect server agree:
discovery, the authorization form, the token exchange, the ID token's
signature and claims, ``prompt=login`` and ``auth_time``.

Needs a Keycloak in development mode and the test database::

    docker run --rm -p 8080:8080 \\
      -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \\
      quay.io/keycloak/keycloak:26.0 start-dev
    eval "$(ops/dev/test-db-local.sh env)"
    SSO_KEYCLOAK_URL=http://localhost:8080 uv run pytest -m external tests/external/test_sso_keycloak.py

Skipped unless ``SSO_KEYCLOAK_URL`` is set. It creates and deletes its own
realm, ``hybridinference-sso-test``.
"""

from __future__ import annotations

import html
import os
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import pytest

from serving.auth import oidc
from serving.config import sso as sso_config
from tests.fixtures.sso_app import location, new_browser

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]

KEYCLOAK = os.environ.get("SSO_KEYCLOAK_URL", "").rstrip("/")
pytestmark = [
    pytest.mark.external,
    pytest.mark.dbtest,
    pytest.mark.asyncio,
    pytest.mark.skipif(not KEYCLOAK, reason="SSO_KEYCLOAK_URL is not set"),
]

REALM = "hybridinference-sso-test"
ISSUER = f"{KEYCLOAK}/realms/{REALM}"
CLIENT_ID = "hybridinference"
CLIENT_SECRET = "keycloak-test-client-secret"
PROVIDER_ID = "keycloak"
CALLBACK = f"/auth/sso/{PROVIDER_ID}/callback"
USER_PASSWORD = "Member-password-1"
MEMBER = "alice@example.edu"
UNVERIFIED = "bob@example.edu"


def _admin(client: httpx.Client) -> dict[str, str]:
    token = client.post(
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": os.environ.get("SSO_KEYCLOAK_ADMIN", "admin"),
            "password": os.environ.get("SSO_KEYCLOAK_ADMIN_PASSWORD", "admin"),
        },
    )
    token.raise_for_status()
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


@pytest.fixture(scope="module")
def realm():
    """A realm with one confidential client and two members, removed afterwards."""
    with httpx.Client(timeout=30) as client:
        headers = _admin(client)
        client.delete(f"{KEYCLOAK}/admin/realms/{REALM}", headers=headers)
        users = [
            {
                "username": email.split("@")[0],
                "email": email,
                "emailVerified": verified,
                "enabled": True,
                "firstName": email.split("@")[0].title(),
                "lastName": "Member",
                "credentials": [{"type": "password", "value": USER_PASSWORD, "temporary": False}],
            }
            for email, verified in ((MEMBER, True), (UNVERIFIED, False))
        ]
        created = client.post(
            f"{KEYCLOAK}/admin/realms",
            headers=headers,
            json={
                "realm": REALM,
                "enabled": True,
                "users": users,
                "clients": [
                    {
                        "clientId": CLIENT_ID,
                        "secret": CLIENT_SECRET,
                        "publicClient": False,
                        "standardFlowEnabled": True,
                        "directAccessGrantsEnabled": False,
                        "redirectUris": [f"http://test{CALLBACK}"],
                    }
                ],
            },
        )
        created.raise_for_status()
        yield
        client.delete(f"{KEYCLOAK}/admin/realms/{REALM}", headers=_admin(client))


@pytest.fixture
def idp(monkeypatch):
    """No stand-in: requests go to the real provider."""
    oidc.clear_caches()
    monkeypatch.setattr(oidc, "_transport", None)
    yield None
    oidc.clear_caches()


@pytest.fixture
def keycloak(realm, sso, monkeypatch):
    sso.configure(
        id=PROVIDER_ID,
        display_name="Keycloak",
        issuer=ISSUER,
        client_id=CLIENT_ID,
        auto_create=True,
        require_approval=False,
    )
    monkeypatch.setattr(sso_config, "client_secret", lambda provider: CLIENT_SECRET)
    return sso


async def _sign_in_at_keycloak(authorization_url: str, email: str) -> dict[str, str]:
    """Fill in Keycloak's sign-in form as a browser would; return the callback's query."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as agent:
        page = await agent.get(authorization_url)
        assert page.status_code == 200, f"Keycloak answered {page.status_code}: {page.text[:300]}"
        action = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page.text)
        assert action, "Keycloak's sign-in form was not found"
        # Keycloak marks its session cookies Secure. A browser still sends them
        # to http://localhost; an HTTP library does not, so they are passed on here.
        cookies = "; ".join(f"{name}={value}" for name, value in page.cookies.items())
        submitted = await agent.post(
            urljoin(authorization_url, html.unescape(action.group(1))),
            data={"username": email, "password": USER_PASSWORD, "credentialId": ""},
            headers={"Cookie": cookies},
        )
        assert submitted.status_code == 302, submitted.text[:300]
        target = urlsplit(submitted.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"http://test{CALLBACK}"
    return {name: values[0] for name, values in parse_qs(target.query).items()}


async def _sign_in(browser, email: str = MEMBER) -> httpx.Response:
    started = await browser.post("/auth/sso/discover", json={"email": email})
    assert started.status_code == 200, started.text
    query = await _sign_in_at_keycloak(started.json()["authorization_url"], email)
    return await browser.get(CALLBACK, params=query)


async def _bearer(browser) -> dict[str, str]:
    refreshed = await browser.post("/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    return {"Authorization": f"Bearer {refreshed.json()['access_token']}"}


async def test_a_member_signs_in_and_gets_an_account(browser, store, keycloak):
    done = await _sign_in(browser)

    assert location(done) == ("/login", {"sso": ["complete"], "next": ["/dashboard"]})
    user = await store.get_user_by_email(MEMBER)
    assert user["status"] == "active" and user["password_hash"] is None
    assert user["user_name"] == "Alice Member"
    (identity,) = await store.list_user_identities(user["id"])
    assert identity["issuer"] == ISSUER
    me = await browser.get("/user/me", headers=await _bearer(browser))
    assert me.json()["sso_reauth_provider"] == {"id": PROVIDER_ID, "name": "Keycloak"}


async def test_a_second_sign_in_reaches_the_same_account(app, browser, store, keycloak):
    await _sign_in(browser)
    first = await store.get_user_by_email(MEMBER)

    async with new_browser(app) as elsewhere:
        again = await _sign_in(elsewhere)

    assert location(again)[1]["sso"] == ["complete"]
    assert len(await store.list_user_identities(first["id"])) == 1


async def test_an_email_keycloak_has_not_verified_is_refused(browser, store, keycloak):
    done = await _sign_in(browser, UNVERIFIED)

    assert location(done) == ("/login", {"sso_error": ["email_untrusted"]})
    assert await store.get_user_by_email(UNVERIFIED) is None


async def test_the_deployment_may_vouch_for_the_providers_emails(browser, store, keycloak):
    keycloak.configure(
        id=PROVIDER_ID,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        auto_create=True,
        require_approval=False,
        assume_email_verified=True,
    )

    done = await _sign_in(browser, UNVERIFIED)

    assert location(done)[1]["sso"] == ["complete"]


async def test_a_wrong_client_secret_fails_the_sign_in(browser, store, keycloak, monkeypatch):
    monkeypatch.setattr(sso_config, "client_secret", lambda provider: "not-the-secret")

    done = await _sign_in(browser)

    assert location(done) == ("/login", {"sso_error": ["failed"]})
    assert await store.get_user_by_email(MEMBER) is None


async def test_a_fresh_sign_in_at_keycloak_confirms_deleting_the_account(browser, store, keycloak):
    await _sign_in(browser)
    headers = await _bearer(browser)
    refused = await browser.post("/auth/delete-account", json={"confirm": MEMBER}, headers=headers)
    assert refused.status_code == 400

    started = await browser.post(
        "/auth/sso/reauth/start",
        json={"purpose": "delete_account", "return_to": "/dashboard/settings"},
        headers=headers,
    )
    assert started.status_code == 200, started.text
    url = started.json()["authorization_url"]
    asked: dict[str, Any] = parse_qs(urlsplit(url).query)
    assert asked["prompt"] == ["login"] and asked["max_age"] == ["0"]
    back = await browser.get(CALLBACK, params=await _sign_in_at_keycloak(url, MEMBER))

    # Keycloak reported when the member signed in, and it was just now.
    assert location(back) == ("/dashboard/settings", {"reauth": ["delete_account"]})
    deleted = await browser.post("/auth/delete-account", json={"confirm": MEMBER}, headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert (await store.get_user_by_email(MEMBER))["status"] == "deleted"
