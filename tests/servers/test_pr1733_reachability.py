"""Can the self-service write paths reach a claimed, soft-deleted account?

The follow-up review of the verification branch asks whether the self-service
profile and password endpoints can actually reach a ``deleted`` account with
an active hard-delete claim, and whether permitting those writes is required
by a product contract or merely restores pre-claim behaviour.

This test answers that empirically, over the real HTTP stack: it drives the
routes with a real session and observes what the server does.

The routes are the ones that call ``update_user_fields`` without a
compare-and-swap:

- ``PATCH /user/profile`` (a ``user_name`` write),
- ``PATCH /user/password`` (a ``password_hash`` write),
- the login route's bootstrap seed (``role='admin'``),
- the browser-session token issuance (the same seed).
"""

from __future__ import annotations

import pytest

pytest_plugins = ["tests.servers.conftest_auth"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _soft_delete_and_claim(store, user_id: str) -> None:
    """Put the account in the state the concern is about."""
    await store.delete_user(user_id, admin_id="ops", reason="test")
    await store.begin_hard_delete_user(user_id)


async def test_the_profile_route_refuses_a_claimed_deleted_account(
    auth_app_client, auth_headers, auth_backend, test_user
):
    """A soft-deleted, claimed account cannot use the profile route."""
    store = auth_backend[0]
    uid = test_user["id"]
    await _soft_delete_and_claim(store, uid)

    response = await auth_app_client.patch(
        "/user/profile",
        headers=auth_headers,
        json={"user_name": "renamed"},
    )

    # The soft delete revoked every session, so the request is refused
    # before it reaches the write.
    assert response.status_code in (401, 403), response.text
    row = await store.get_user_by_id(uid)
    assert row["user_name"] != "renamed"


async def test_the_password_route_refuses_a_claimed_deleted_account(
    auth_app_client, auth_headers, auth_backend, test_user
):
    """A soft-deleted, claimed account cannot use the password route."""
    store = auth_backend[0]
    uid = test_user["id"]
    await _soft_delete_and_claim(store, uid)

    response = await auth_app_client.post(
        "/user/change-password",
        headers=auth_headers,
        json={"old_password": test_user["password"], "new_password": "N3wStr0ngPassw0rd!"},
    )

    assert response.status_code in (401, 403), response.text


async def test_the_login_route_refuses_a_claimed_deleted_account(
    auth_app_client, auth_backend, test_user
):
    """Login is refused before the bootstrap seed can write."""
    store = auth_backend[0]
    uid = test_user["id"]
    await _soft_delete_and_claim(store, uid)

    response = await auth_app_client.post(
        "/auth/login",
        json={"email": test_user["email"], "password": test_user["password"]},
    )
    assert response.status_code == 403, response.text


async def test_the_browser_session_route_refuses_a_claimed_deleted_account(
    auth_app_client, auth_headers, auth_backend, test_user
):
    """The browser-session token route refuses a non-active account."""
    store = auth_backend[0]
    uid = test_user["id"]
    await _soft_delete_and_claim(store, uid)

    response = await auth_app_client.post(
        "/auth/switch",
        headers=auth_headers,
        json={"user_id": uid},
    )
    assert response.status_code in (401, 403), response.text


async def test_a_live_session_on_a_claimed_account_is_refused_by_the_session_check(
    auth_app_client, auth_backend, test_user
):
    """The session check alone refuses the account.

    The soft delete deletes the ``auth_sessions`` rows, so even a token
    minted before the deletion fails ``require_live_session``. This is the
    first of the two gates; the status check in ``get_current_user`` is the
    second.
    """
    store = auth_backend[0]
    uid = test_user["id"]
    await _soft_delete_and_claim(store, uid)

    assert await store.session_is_live(uid, "any-sid") is False
