"""The self-service routes report a lost claim as a controlled 409.

The store refuses every write to an account under a hard-delete claim. A
self-service writer cannot normally reach that guard — a claimed account is
soft-deleted, and every self-service route requires an ``active`` account —
but the write and the claim can race, and the guard must not escape as an
unhandled error.

These tests drive the real routes over the real store and force the race, so
the response the user sees is the one the code produces.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["tests.servers.conftest_auth"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _claim_the_account(store, user_id: str) -> None:
    await store.delete_user(user_id, admin_id="ops", reason="test")
    await store.begin_hard_delete_user(user_id)


async def test_the_profile_route_reports_a_claim_conflict_as_409(
    auth_app_client, auth_headers, auth_backend, test_user, monkeypatch
):
    """A claim that lands between the session check and the write is a 409."""
    store = auth_backend[0]

    original = store.update_user_fields

    async def _claim_then_write(user_id, **fields):
        # The claim lands after the route's own checks.
        await _claim_the_account(store, user_id)
        return await original(user_id, **fields)

    monkeypatch.setattr(store, "update_user_fields", _claim_then_write)

    response = await auth_app_client.patch(
        "/user/profile",
        headers=auth_headers,
        json={"user_name": "renamed"},
    )

    assert response.status_code == 409, response.text
    assert "permanently deleted" in response.text


async def test_the_password_route_reports_a_claim_conflict_as_409(
    auth_app_client, auth_headers, auth_backend, test_user, monkeypatch
):
    """The password route reports the conflict the same way."""
    store = auth_backend[0]

    original = store.update_user_fields

    async def _claim_then_write(user_id, **fields):
        await _claim_the_account(store, user_id)
        return await original(user_id, **fields)

    monkeypatch.setattr(store, "update_user_fields", _claim_then_write)

    response = await auth_app_client.post(
        "/user/change-password",
        headers=auth_headers,
        json={"old_password": test_user["password"], "new_password": "N3wStr0ngPassw0rd!"},
    )

    assert response.status_code == 409, response.text
    assert "permanently deleted" in response.text


async def test_the_profile_route_still_works_for_an_active_account(
    auth_app_client, auth_headers, auth_backend, test_user
):
    """The ordinary case is unchanged."""
    store = auth_backend[0]
    uid = test_user["id"]

    response = await auth_app_client.patch(
        "/user/profile",
        headers=auth_headers,
        json={"user_name": "renamed"},
    )

    assert response.status_code == 200, response.text
    row = await store.get_user_by_id(uid)
    assert row["user_name"] == "renamed"


async def test_the_profile_route_reports_an_avatar_claim_conflict_as_409(
    auth_app_client, auth_headers, auth_backend, test_user, monkeypatch
):
    """The avatar write is refused the same way, and reported as a 409.

    ``set_user_avatar`` does not go through ``update_user_fields``, so it
    needs its own guard and its own conflict mapping: the request is
    authenticated before the claim exists, and the claim lands between the
    route's check and the write.
    """
    store = auth_backend[0]

    original = store.set_user_avatar

    async def _claim_then_write(user_id, avatar_data_url):
        await _claim_the_account(store, user_id)
        return await original(user_id, avatar_data_url)

    monkeypatch.setattr(store, "set_user_avatar", _claim_then_write)

    response = await auth_app_client.patch(
        "/user/profile",
        headers=auth_headers,
        json={"avatar_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="},
    )

    assert response.status_code == 409, response.text
    assert "permanently deleted" in response.text


async def test_the_profile_route_still_sets_an_avatar_for_an_active_account(
    auth_app_client, auth_headers, auth_backend, test_user
):
    """The ordinary case is unchanged."""
    store = auth_backend[0]
    uid = test_user["id"]

    response = await auth_app_client.patch(
        "/user/profile",
        headers=auth_headers,
        json={"avatar_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="},
    )

    assert response.status_code == 200, response.text
    assert await store.get_user_avatar(uid) == "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
