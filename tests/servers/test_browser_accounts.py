"""Several accounts signed in on one browser, against a real database.

Run with: make test-db
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import jwt
import pytest
import pytest_asyncio

from serving.servers.routers import internal
from serving.servers.routers.auth_routes import hash_refresh_token
from serving.utils.jwt import get_jwt_secret
from tests.fixtures.auth_helpers import seed_test_user

if TYPE_CHECKING:
    from httpx import AsyncClient, Response

pytest_plugins = ["tests.servers.conftest_auth"]

pytestmark = pytest.mark.dbtest


class Browser:
    """One browser: the session cookie it holds, kept current from every response."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.cookie: str | None = None

    async def send(self, method: str, url: str, **kwargs: Any) -> Response:
        # The client's own jar would mix browsers; each request carries only ours.
        self.client.cookies.clear()
        cookies = {"refresh_token": self.cookie} if self.cookie else None
        response = await self.client.request(method, url, cookies=cookies, **kwargs)
        for header in response.headers.get_list("set-cookie"):
            name, _, rest = header.partition("=")
            if name == "refresh_token":
                value = rest.split(";", 1)[0].strip('"')
                self.cookie = value or None
        return response

    async def login(self, user: dict[str, Any]) -> Response:
        return await self.send(
            "POST", "/auth/login", json={"email": user["email"], "password": user["password"]}
        )

    async def accounts(self) -> dict[str, Any]:
        response = await self.send("GET", "/auth/accounts")
        assert response.status_code == 200, response.text
        return response.json()

    async def refreshed_subject(self) -> str | None:
        response = await self.send("POST", "/auth/refresh")
        if response.status_code != 200:
            return None
        return subject(response)


def subject(response: Response) -> str:
    """The account an access token in *response* was issued to."""
    token = response.json()["access_token"]
    return jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])["sub"]


def ids(listing: dict[str, Any]) -> list[str]:
    return [account["id"] for account in listing["accounts"]]


async def another_user(operational_store: Any) -> dict[str, Any]:
    # The factory derives the address from the id's timestamp prefix, which two
    # users made in the same millisecond share.
    return await seed_test_user(operational_store, email=f"other_{uuid4().hex[:12]}@signuptest.dev")


@pytest_asyncio.fixture
async def second_user(auth_backend, test_user):
    operational_store, _, _, _ = auth_backend
    return await another_user(operational_store)


@pytest.fixture
def browser(auth_app_client: AsyncClient) -> Browser:
    return Browser(auth_app_client)


@pytest.mark.asyncio
async def test_a_second_sign_in_keeps_the_first_account(browser, test_user, second_user):
    await browser.login(test_user)
    response = await browser.login(second_user)
    assert response.status_code == 200, response.text

    listing = await browser.accounts()
    assert ids(listing) == [test_user["id"], second_user["id"]]
    assert listing["active_user_id"] == second_user["id"]
    assert all(account["signed_in"] for account in listing["accounts"])
    assert await browser.refreshed_subject() == second_user["id"]


@pytest.mark.asyncio
async def test_switching_needs_no_password_and_moves_the_browser(browser, test_user, second_user):
    await browser.login(test_user)
    await browser.login(second_user)

    response = await browser.send("POST", "/auth/switch", json={"user_id": test_user["id"]})

    assert response.status_code == 200, response.text
    assert subject(response) == test_user["id"]
    assert response.json()["user"]["email"] == test_user["email"].lower()
    assert (await browser.accounts())["active_user_id"] == test_user["id"]
    assert await browser.refreshed_subject() == test_user["id"]


@pytest.mark.asyncio
async def test_a_token_without_activating_leaves_the_browser_where_it_was(
    browser, test_user, second_user
):
    await browser.login(test_user)
    await browser.login(second_user)

    response = await browser.send(
        "POST", "/auth/switch", json={"user_id": test_user["id"], "activate": False}
    )

    assert response.status_code == 200, response.text
    assert subject(response) == test_user["id"]
    assert await browser.refreshed_subject() == second_user["id"]


@pytest.mark.asyncio
async def test_switching_to_an_account_not_signed_in_here_is_refused(
    browser, auth_app_client, test_user, second_user
):
    await browser.login(test_user)
    elsewhere = Browser(auth_app_client)
    await elsewhere.login(second_user)

    response = await browser.send("POST", "/auth/switch", json={"user_id": second_user["id"]})

    assert response.status_code == 401
    assert await browser.refreshed_subject() == test_user["id"]


@pytest.mark.asyncio
async def test_logout_signs_out_only_the_current_account(browser, test_user, second_user):
    await browser.login(test_user)
    signed_in = await browser.login(second_user)
    bearer = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

    response = await browser.send("POST", "/auth/logout", headers=bearer)

    assert response.status_code == 200
    assert browser.cookie is not None
    listing = await browser.accounts()
    assert ids(listing) == [test_user["id"]]
    assert listing["active_user_id"] is None
    # No account is active until one is chosen again, without a password.
    assert await browser.refreshed_subject() is None
    switched = await browser.send("POST", "/auth/switch", json={"user_id": test_user["id"]})
    assert switched.status_code == 200


@pytest.mark.asyncio
async def test_removing_the_last_account_drops_the_cookie(browser, test_user):
    await browser.login(test_user)
    retired = browser.cookie

    response = await browser.send("DELETE", f"/auth/accounts/{test_user['id']}")

    assert response.status_code == 200
    assert response.json()["accounts"] == []
    assert browser.cookie is None
    browser.cookie = retired
    assert await browser.refreshed_subject() is None


@pytest.mark.asyncio
async def test_logout_all_signs_every_account_out(browser, test_user, second_user):
    await browser.login(test_user)
    await browser.login(second_user)
    retired = browser.cookie

    response = await browser.send("POST", "/auth/logout-all")

    assert response.status_code == 200
    assert browser.cookie is None
    browser.cookie = retired
    assert (await browser.accounts())["accounts"] == []
    assert await browser.refreshed_subject() is None


@pytest.mark.asyncio
async def test_a_sixth_account_is_refused(browser, auth_backend, test_user):
    operational_store, _, _, _ = auth_backend
    others = [await another_user(operational_store) for _ in range(5)]
    for user in [test_user, *others[:4]]:
        assert (await browser.login(user)).status_code == 200

    refused = await browser.login(others[4])

    assert refused.status_code == 409
    assert len((await browser.accounts())["accounts"]) == 5
    # Signing in again to one already here is not a sixth.
    assert (await browser.login(test_user)).status_code == 200


@pytest.mark.asyncio
async def test_an_ended_session_is_listed_as_signed_out(
    browser, auth_backend, test_user, second_user
):
    operational_store, _, _, _ = auth_backend
    await browser.login(test_user)
    await browser.login(second_user)

    # A password reset deletes every session the account has.
    await operational_store.delete_user_sessions(test_user["id"])

    listing = await browser.accounts()
    first = next(a for a in listing["accounts"] if a["id"] == test_user["id"])
    assert first["signed_in"] is False
    refused = await browser.send("POST", "/auth/switch", json={"user_id": test_user["id"]})
    assert refused.status_code == 401

    await browser.login(test_user)
    listing = await browser.accounts()
    assert ids(listing) == [test_user["id"], second_user["id"]]
    assert all(account["signed_in"] for account in listing["accounts"])


@pytest.mark.asyncio
async def test_changing_the_password_signs_the_account_out_of_other_browsers(
    auth_app_client, test_user, second_user
):
    here, elsewhere = Browser(auth_app_client), Browser(auth_app_client)
    token = (await here.login(test_user)).json()["access_token"]
    await elsewhere.login(second_user)
    await elsewhere.login(test_user)

    response = await here.send(
        "POST",
        "/user/change-password",
        json={"old_password": test_user["password"], "new_password": "Brand-New-Pass-1"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    # The browser that changed it stays signed in.
    assert await here.refreshed_subject() == test_user["id"]
    # Elsewhere the account is signed out; the other account there is not.
    listing = await elsewhere.accounts()
    signed_in = {account["id"]: account["signed_in"] for account in listing["accounts"]}
    assert signed_in == {test_user["id"]: False, second_user["id"]: True}
    refused = await elsewhere.send("POST", "/auth/switch", json={"user_id": test_user["id"]})
    assert refused.status_code == 401


@pytest.mark.asyncio
async def test_deleting_a_user_takes_it_off_every_browser(
    browser, auth_backend, test_user, second_user
):
    operational_store, _, _, _ = auth_backend
    await browser.login(second_user)
    await browser.login(test_user)

    await operational_store.delete_user(test_user["id"], admin_ip="127.0.0.1", admin_id="admin")

    listing = await browser.accounts()
    assert ids(listing) == [second_user["id"]]
    assert listing["active_user_id"] is None


@pytest.mark.asyncio
async def test_a_cookie_from_before_browser_sessions_is_upgraded_once(
    browser, auth_backend, test_user
):
    operational_store, _, _, _ = auth_backend
    await operational_store.create_session(
        session_id=str(uuid4()),
        user_id=test_user["id"],
        refresh_token_hash=hash_refresh_token("legacy-refresh"),
        jti=str(uuid4()),
        sid=str(uuid4()),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    browser.cookie = "legacy-refresh"

    assert await browser.refreshed_subject() == test_user["id"]
    assert browser.cookie not in (None, "legacy-refresh")
    assert ids(await browser.accounts()) == [test_user["id"]]

    # The old value names nothing now, so it cannot be upgraded a second time.
    replay = Browser(browser.client)
    replay.cookie = "legacy-refresh"
    assert await replay.refreshed_subject() is None


@pytest.mark.asyncio
async def test_pgadmin_admits_a_browser_with_any_live_admin_account(
    browser, auth_app_client, auth_test_app, auth_backend, test_user, second_user
):
    operational_store, _, _, _ = auth_backend
    auth_test_app.include_router(internal.router)
    await operational_store.update_user_fields(test_user["id"], role="admin")

    await browser.login(second_user)
    assert (await browser.send("GET", "/internal/verify-admin")).status_code == 403

    await browser.login(test_user)
    await browser.send("POST", "/auth/switch", json={"user_id": second_user["id"]})
    # The console is showing the non-admin account; the browser still holds an admin.
    assert (await browser.send("GET", "/internal/verify-admin")).status_code == 200

    stranger = Browser(auth_app_client)
    assert (await stranger.send("GET", "/internal/verify-admin")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_it", ["suspend", "revoke"])
async def test_pgadmin_refuses_a_browser_whose_admin_account_is_no_longer_live(
    browser, auth_test_app, auth_backend, test_user, second_user, lose_it
):
    """Having once signed in an admin is not enough: that account must still be live.

    With a member still signed in the browser is known (403); with nobody live
    it is not (401).
    """
    operational_store, _, _, _ = auth_backend
    auth_test_app.include_router(internal.router)
    await operational_store.update_user_fields(test_user["id"], role="admin")
    await browser.login(second_user)
    await browser.login(test_user)
    assert (await browser.send("GET", "/internal/verify-admin")).status_code == 200

    if lose_it == "suspend":
        await operational_store.update_user_fields(test_user["id"], status="suspended")
    else:
        await operational_store.revoke_user_sessions(test_user["id"])
    assert (await browser.send("GET", "/internal/verify-admin")).status_code == 403

    await operational_store.revoke_user_sessions(second_user["id"])
    assert (await browser.send("GET", "/internal/verify-admin")).status_code == 401
