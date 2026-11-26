"""Organizations over HTTP, against the real store.

The platform administrator sets an organization up through the Admin routes;
its members come from single sign-on; an organization administrator sees and
manages them in the organization console. These tests show who becomes a
member and when, who reaches which route, and that an organization is never
shown what its members sent.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request

from serving import organizations as organization_rules, quota
from serving.audit import AuditActor
from serving.servers.auth import generate_api_key, hash_api_key, verify_api_key
from serving.servers.routers import organization
from serving.storage.base import CostIncrement
from tests.fixtures.oidc_provider import ISSUER
from tests.fixtures.sso_app import (
    EMAIL,
    PASSWORD,
    answer,
    existing_user,
    location,
    new_browser,
    start,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

BOB = "bob@example.edu"
SECRET_PROMPT = "the-prompt-nobody-else-reads"
SECRET_RESPONSE = "the-answer-nobody-else-reads"
PLATFORM = AuditActor.admin("u-platform", "192.0.2.10")


@pytest.fixture(autouse=True)
def organization_routes(app):
    """The organization console, beside the routes the single sign-on fixture mounts."""
    app.include_router(organization.router)


@pytest_asyncio.fixture(autouse=True)
async def clean_organizations(auth_backend, clean_auth_tables):
    _store, _log_store, db_logger, _backend = auth_backend

    async def _wipe() -> None:
        async with db_logger.pool.acquire() as conn:
            await conn.execute("DELETE FROM orgs")
            await conn.execute("DELETE FROM spend_counters WHERE scope_id LIKE 'o:%'")
            await conn.execute("DELETE FROM admin_audit_log WHERE organization_id IS NOT NULL")
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'org-itest-%'")
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'org-itest-%'")

    await _wipe()
    yield db_logger.pool
    await _wipe()


@pytest.fixture
def members_sign_straight_in(sso):
    """A provider whose members get an account on their first sign-in."""
    sso.configure(auto_create=True, require_approval=False)
    return sso


# -- helpers -----------------------------------------------------------------------


async def _bearer(browser) -> dict[str, str]:
    refreshed = await browser.post("/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    return {"Authorization": f"Bearer {refreshed.json()['access_token']}"}


async def _password_session(browser, email: str) -> dict[str, str]:
    login = await browser.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _member(app, idp, store, *, email: str = EMAIL, sub: str = "subject-1"):
    """Sign a member in through the provider in a browser of its own."""
    browser = new_browser(app)
    done = await answer(browser, idp, await start(browser, email), sub=sub, email=email, name=email)
    assert location(done)[1].get("sso") == ["complete"], done.headers["location"]
    headers = await _bearer(browser)
    user = await store.get_user_by_email(email)
    return browser, headers, user["id"]


async def _platform_admin(app, store):
    browser = new_browser(app)
    admin_id = await existing_user(store, "root@elsewhere.org")
    await store.update_user_fields(admin_id, role="admin")
    return browser, await _password_session(browser, "root@elsewhere.org"), admin_id


async def _make_organization(browser, headers, **body: Any) -> dict[str, Any]:
    payload = {"name": "Example University", "provider_ids": ["example-univ"], **body}
    created = await browser.post("/admin/organizations", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()


async def _set_role(browser, headers, organization_id: str, user_id: str, role: str = "admin"):
    """Give the role, with the administrator's password, or take it back, with nothing."""
    confirmation = {"password": PASSWORD} if role == "admin" else {}
    response = await browser.put(
        f"/admin/organizations/{organization_id}/members/{user_id}/role",
        json={"role": role, **confirmation},
        headers=headers,
    )
    assert response.status_code == 200, response.text


async def _audit_actions(pool, organization_id: str) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action FROM admin_audit_log WHERE organization_id = $1 ORDER BY id",
            organization_id,
        )
    return [row["action"] for row in rows]


async def _organization_with_an_administrator(app, idp, store):
    """Alice administers the organization; Bob is a member. Returns their sessions."""
    root, root_headers, _ = await _platform_admin(app, store)
    alice, alice_headers, alice_id = await _member(app, idp, store)
    bob, bob_headers, bob_id = await _member(app, idp, store, email=BOB, sub="subject-2")
    made = await _make_organization(root, root_headers)
    organization_id = made["organization"]["id"]
    await _set_role(root, root_headers, organization_id, alice_id)
    return {
        "organization_id": organization_id,
        "root": (root, root_headers),
        "alice": (alice, alice_headers, alice_id),
        "bob": (bob, bob_headers, bob_id),
    }


# -- making an organization --------------------------------------------------------


async def test_making_an_organization_makes_the_accounts_of_its_connection_members(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    root, headers, root_id = await _platform_admin(app, store)
    _alice, _alice_headers, alice_id = await _member(app, idp, store)
    outsider_id = await existing_user(store, "someone@elsewhere.org")

    made = await _make_organization(root, headers, monthly_limit_usd="1000")

    assert made["members_added"] == 1
    body = made["organization"]
    assert body["name"] == "Example University"
    # An organization has no status until a route can change one.
    assert "status" not in body
    assert body["member_count"] == 1
    assert body["monthly_limit_usd"] == 1000.0
    assert body["default_member_monthly_limit_usd"] is None
    assert body["spent_month_usd"] == 0.0
    assert body["administrators"] == []
    assert body["connections"] == [
        {
            "provider_id": "example-univ",
            "available": True,
            "display_name": "Example University",
            "type": "oidc",
            "domains": ["example.edu"],
            "enabled": True,
            "auto_create": True,
            "require_approval": False,
        }
    ]
    assert (await store.get_organization_membership(alice_id))["organization_id"] == body["id"]
    assert await store.get_organization_membership(outsider_id) is None
    assert await store.get_organization_membership(root_id) is None

    listed = (await root.get("/admin/organizations", headers=headers)).json()["organizations"]
    assert [item["id"] for item in listed] == [body["id"]]
    one = await root.get(f"/admin/organizations/{body['id']}", headers=headers)
    assert one.json() == body
    assert await _audit_actions(clean_organizations, body["id"]) == ["organization.create"]


async def test_a_name_a_connection_and_a_provider_are_checked(app, store, sso):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)

    same_name = await root.post(
        "/admin/organizations",
        json={"name": "EXAMPLE UNIVERSITY", "provider_ids": ["example-univ"]},
        headers=headers,
    )
    same_connection = await root.post(
        "/admin/organizations",
        json={"name": "Another", "provider_ids": ["example-univ"]},
        headers=headers,
    )
    unknown = await root.post(
        "/admin/organizations",
        json={"name": "Third", "provider_ids": ["no-such-provider"]},
        headers=headers,
    )
    no_connection = await root.post(
        "/admin/organizations", json={"name": "Fourth", "provider_ids": []}, headers=headers
    )
    # A name of spaces would be stored as no name at all.
    no_name = await root.post(
        "/admin/organizations",
        json={"name": "   ", "provider_ids": ["example-univ"]},
        headers=headers,
    )
    organization_id = (await root.get("/admin/organizations", headers=headers)).json()[
        "organizations"
    ][0]["id"]
    renamed_to_spaces = await root.patch(
        f"/admin/organizations/{organization_id}", json={"name": " \t "}, headers=headers
    )
    padded = await root.patch(
        f"/admin/organizations/{organization_id}",
        json={"name": "  Example University  "},
        headers=headers,
    )

    assert (same_name.status_code, same_connection.status_code) == (409, 409)
    assert unknown.status_code == 404
    assert no_connection.status_code == 422
    assert (no_name.status_code, renamed_to_spaces.status_code) == (422, 422)
    # The spaces around a name are not part of it: this is the name it has.
    assert (padded.status_code, padded.json()["name"]) == (200, "Example University")
    listed = (await root.get("/admin/organizations", headers=headers)).json()["organizations"]
    assert [item["name"] for item in listed] == ["Example University"]


async def test_the_admin_routes_are_the_platform_administrators_alone(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, alice_headers, _ = state["alice"]

    anonymous = await new_browser(app).get("/admin/organizations")
    as_org_admin = await alice.get("/admin/organizations", headers=alice_headers)
    create = await alice.post(
        "/admin/organizations",
        json={"name": "Mine", "provider_ids": ["example-univ"]},
        headers=alice_headers,
    )

    # The role of organization administrator gives no access to /admin.
    assert anonymous.status_code in (401, 403)
    assert as_org_admin.status_code == 403
    assert create.status_code == 403


async def test_the_name_and_the_limit_change_and_a_connection_is_attached_and_detached(
    app, idp, store, sso, clean_organizations, members_sign_straight_in
):
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)
    organization_id = made["organization"]["id"]
    path = f"/admin/organizations/{organization_id}"

    renamed = await root.patch(
        path, json={"name": "Example U", "monthly_limit_usd": "250.5"}, headers=headers
    )
    cleared = await root.patch(path, json={"monthly_limit_usd": None}, headers=headers)
    missing = await root.patch(
        "/admin/organizations/no-such-organization", json={"name": "X"}, headers=headers
    )
    detached = await root.delete(f"{path}/connections/example-univ", headers=headers)
    again = await root.delete(f"{path}/connections/example-univ", headers=headers)
    # An account that signs in while the connection belongs to no organization is no member.
    _alice, _headers, alice_id = await _member(app, idp, store)
    before_attach = await store.get_organization_membership(alice_id)
    attached = await root.put(f"{path}/connections/example-univ", headers=headers)

    assert renamed.status_code == 200
    assert (renamed.json()["name"], renamed.json()["monthly_limit_usd"]) == ("Example U", 250.5)
    assert cleared.json()["monthly_limit_usd"] is None
    assert cleared.json()["name"] == "Example U"
    assert missing.status_code == 404
    assert detached.json()["connections"] == []
    assert again.status_code == 404
    assert before_attach is None
    assert attached.status_code == 200
    assert attached.json()["members_added"] == 1
    assert (await store.get_organization_membership(alice_id))["organization_id"] == (
        organization_id
    )
    assert await _audit_actions(clean_organizations, organization_id) == [
        "organization.create",
        "organization.update",
        "organization.limit.set",
        "organization.limit.set",
        "organization.connection.detach",
        "organization.connection.attach",
    ]


async def test_a_connection_whose_provider_is_gone_is_shown_as_not_available(app, store, sso):
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)

    sso.providers = [sso.provider(id="renamed")]
    shown = await root.get(f"/admin/organizations/{made['organization']['id']}", headers=headers)

    assert shown.json()["connections"] == [
        {
            "provider_id": "example-univ",
            "available": False,
            "display_name": None,
            "type": None,
            "domains": [],
            "enabled": False,
            "auto_create": False,
            "require_approval": False,
        }
    ]


# -- becoming a member -------------------------------------------------------------


async def test_a_first_sign_in_through_a_connection_makes_a_member_and_is_recorded(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)
    organization_id = made["organization"]["id"]

    alice, alice_headers, alice_id = await _member(app, idp, store)

    membership = await store.get_organization_membership(alice_id)
    assert (membership["organization_id"], membership["role"]) == (organization_id, "member")
    me = (await alice.get("/user/me", headers=alice_headers)).json()
    assert me["organization"] == {
        "id": organization_id,
        "name": "Example University",
        "role": "member",
    }
    async with clean_organizations.acquire() as conn:
        joined = await conn.fetchrow(
            "SELECT actor_type, actor_id, target_user_id, details::text AS details "
            "FROM admin_audit_log WHERE organization_id = $1 "
            "AND action = 'organization.member.join'",
            organization_id,
        )
    assert (joined["actor_type"], joined["actor_id"]) == ("user", alice_id)
    assert joined["target_user_id"] == alice_id
    assert '"sso_provider": "example-univ"' in joined["details"]

    # A second sign-in of a member writes no second row.
    await _member(app, idp, store)
    assert (await _audit_actions(clean_organizations, organization_id)).count(
        "organization.member.join"
    ) == 1


async def test_an_account_in_no_organization_has_none_on_its_profile(app, store):
    await existing_user(store, "someone@elsewhere.org")
    browser = new_browser(app)
    headers = await _password_session(browser, "someone@elsewhere.org")

    me = (await browser.get("/user/me", headers=headers)).json()

    assert me["organization"] is None


async def test_guided_linking_makes_an_existing_account_a_member(
    app, idp, store, clean_organizations
):
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)
    account_id = await existing_user(store)  # alice@example.edu, with a password
    browser = new_browser(app)

    waiting = await answer(browser, idp, await start(browser))
    assert location(waiting)[1].get("sso") == ["link"]
    assert await store.get_organization_membership(account_id) is None
    session = await _password_session(browser, EMAIL)
    confirmed = await browser.post("/auth/sso/link/confirm", headers=session)

    assert confirmed.status_code == 200, confirmed.text
    membership = await store.get_organization_membership(account_id)
    assert membership["organization_id"] == made["organization"]["id"]
    assert (await store.get_user_identity(ISSUER, "subject-1"))["user_id"] == account_id
    assert "organization.member.join" in await _audit_actions(
        clean_organizations, made["organization"]["id"]
    )


async def test_a_member_cannot_disconnect_the_identity_of_its_organization(
    app, idp, store, members_sign_straight_in
):
    root, headers, _ = await _platform_admin(app, store)
    await _make_organization(root, headers)
    alice, alice_headers, _ = await _member(app, idp, store)
    identity = (await store.get_user_identity(ISSUER, "subject-1"))["id"]

    refused = await alice.post(
        f"/auth/sso/identities/{identity}/unlink", json={}, headers=alice_headers
    )

    assert refused.status_code == 409
    assert "belongs to your organization" in refused.json()["detail"]
    assert await store.get_user_identity(ISSUER, "subject-1") is not None


# -- who reaches the organization console ---------------------------------------------


async def test_only_an_organization_administrator_reaches_the_console(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, alice_headers, _ = state["alice"]
    bob, bob_headers, bob_id = state["bob"]
    await existing_user(store, "someone@elsewhere.org")
    outsider = new_browser(app)
    outsider_headers = await _password_session(outsider, "someone@elsewhere.org")

    paths = (
        "/user/organization",
        "/user/organization/overview",
        "/user/organization/members",
        f"/user/organization/members/{bob_id}",
        "/user/organization/usage/requests",
        "/user/organization/usage/models",
        "/user/organization/usage/members",
        "/user/organization/audit-log",
    )
    for path in paths:
        assert (await new_browser(app).get(path)).status_code in (401, 403), path
        assert (await bob.get(path, headers=bob_headers)).status_code == 403, path
        assert (await outsider.get(path, headers=outsider_headers)).status_code == 403, path
        # The platform administrator is not a member: the console is not theirs.
        assert (await root.get(path, headers=root_headers)).status_code == 403, path
        assert (await alice.get(path, headers=alice_headers)).status_code == 200, path

    for action in ("remove", "restore"):
        path = f"/user/organization/members/{bob_id}/{action}"
        assert (await bob.post(path, headers=bob_headers)).status_code == 403
        assert (await root.post(path, headers=root_headers)).status_code == 403
    assert (await store.get_user_by_id(bob_id))["status"] == "active"


async def test_an_administrator_reaches_only_the_members_of_its_own_organization(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    _alice, _alice_headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    # Another organization, with an administrator of its own.
    boss_id = await existing_user(store, "boss@other.example.org")
    await store.create_organization(
        organization_id="o-other", name="Other Organization", actor=PLATFORM
    )
    # Signed in before the membership: a member has no password sign-in, and
    # a session from before the join stays.
    boss = new_browser(app)
    boss_headers = await _password_session(boss, "boss@other.example.org")
    async with clean_organizations.acquire() as conn:
        await conn.execute(
            "INSERT INTO org_memberships (user_id, organization_id, role) "
            "VALUES ($1, 'o-other', 'admin')",
            boss_id,
        )

    own = (await boss.get("/user/organization", headers=boss_headers)).json()
    members = (await boss.get("/user/organization/members", headers=boss_headers)).json()
    detail = await boss.get(f"/user/organization/members/{bob_id}", headers=boss_headers)
    remove = await boss.post(f"/user/organization/members/{bob_id}/remove", headers=boss_headers)
    restore = await boss.post(
        f"/user/organization/members/{alice_id}/restore", headers=boss_headers
    )

    assert own["name"] == "Other Organization"
    assert [member["user_id"] for member in members["members"]] == [boss_id]
    # A member of another organization answers as an account that does not exist.
    assert (detail.status_code, remove.status_code, restore.status_code) == (404, 404, 404)
    assert (await store.get_user_by_id(bob_id))["status"] == "active"


async def test_the_role_is_given_only_to_a_member(app, idp, store, members_sign_straight_in):
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)
    organization_id = made["organization"]["id"]
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    alice, alice_headers, alice_id = await _member(app, idp, store)

    stranger = await root.put(
        f"/admin/organizations/{organization_id}/members/{outsider_id}/role",
        json={"role": "admin", "password": PASSWORD},
        headers=headers,
    )
    bad_role = await root.put(
        f"/admin/organizations/{organization_id}/members/{alice_id}/role",
        json={"role": "owner"},
        headers=headers,
    )
    before = await alice.get("/user/organization", headers=alice_headers)
    await _set_role(root, headers, organization_id, alice_id)
    after = await alice.get("/user/organization", headers=alice_headers)
    me = (await alice.get("/user/me", headers=alice_headers)).json()
    await _set_role(root, headers, organization_id, alice_id, "member")
    taken_back = await alice.get("/user/organization", headers=alice_headers)

    assert (stranger.status_code, bad_role.status_code) == (404, 422)
    assert (before.status_code, after.status_code, taken_back.status_code) == (403, 200, 403)
    assert me["organization"]["role"] == "admin"
    assert [admin["user_id"] for admin in after.json()["administrators"]] == [alice_id]


async def test_giving_the_role_asks_the_platform_administrator_to_confirm(
    app, idp, store, clean_organizations, members_sign_straight_in, monkeypatch
):
    """The role stays after the session that gave it: a session alone does not give it."""
    from serving.config.settings import get_settings

    monkeypatch.setenv("ADMIN_TOKEN", "org-itest-admin-token")
    get_settings.cache_clear()
    token = {"Authorization": "Bearer org-itest-admin-token"}
    root, headers, _ = await _platform_admin(app, store)
    made = await _make_organization(root, headers)
    organization_id = made["organization"]["id"]
    _alice, _alice_headers, alice_id = await _member(app, idp, store)
    path = f"/admin/organizations/{organization_id}/members/{alice_id}/role"

    unconfirmed = await root.put(path, json={"role": "admin"}, headers=headers)
    wrong = await root.put(
        path, json={"role": "admin", "password": "not-the-password"}, headers=headers
    )
    # The admin token has no account to confirm with.
    by_token = await root.put(path, json={"role": "admin"}, headers=token)
    before = (await store.get_organization_membership(alice_id))["role"]
    given = await root.put(path, json={"role": "admin", "password": PASSWORD}, headers=headers)
    # An administrator already: nothing to confirm, and nothing to write.
    again = await root.put(path, json={"role": "admin"}, headers=headers)
    # Taking the role back asks for nothing.
    taken = await root.put(path, json={"role": "member"}, headers=token)

    assert (unconfirmed.status_code, wrong.status_code, by_token.status_code) == (400, 400, 403)
    assert before == "member"
    assert (given.status_code, again.status_code, taken.status_code) == (200, 200, 200)
    assert (await store.get_organization_membership(alice_id))["role"] == "member"
    actions = await _audit_actions(clean_organizations, organization_id)
    assert [action for action in actions if action.startswith("organization.admin.")] == [
        "organization.admin.add",
        "organization.admin.remove",
    ]


# -- what an administrator is shown ---------------------------------------------------


async def _seed_usage(
    pool, store, user_id: str, *, requests: int, cost: str, at: datetime | None = None
) -> None:
    """Requests of *user_id* at *at*, by default now, with content an organization is never shown."""
    now = at or datetime.now(UTC)
    key = await store.create_key(
        key_hash=secrets.token_hex(16),
        key_prefix=f"sk-{secrets.token_hex(3)}",
        user_id=user_id,
        account_id=user_id,
        name="laptop",
        api_key_encrypted="encrypted-key-material",
    )
    async with pool.acquire() as conn:
        for index in range(requests):
            await conn.execute(
                "INSERT INTO api_logs (request_id, timestamp, model_id, provider, user_id, "
                "key_id, project_id, status_code, prompt_tokens, completion_tokens, "
                "total_tokens, cost_usd, prompt, response, request_payload) "
                "VALUES ($1, $2, $3, 'test', $4, $5, $6, $7, 100, 50, 150, $8, $9, $10, "
                "$11::jsonb)",
                f"org-itest-{secrets.token_hex(6)}",
                now,
                "model-a" if index % 2 == 0 else "model-b",
                user_id,
                key["id"],
                key.get("project_id"),
                200 if index else 500,
                float(cost) / requests,
                SECRET_PROMPT,
                SECRET_RESPONSE,
                f'{{"messages": "{SECRET_PROMPT}"}}',
            )
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests, last_request_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id,
            now.date().isoformat(),
            float(cost),
            requests,
            now,
        )


async def test_an_administrator_sees_the_usage_of_the_members_and_never_their_content(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    await _seed_usage(clean_organizations, store, alice_id, requests=2, cost="0.50")
    await _seed_usage(clean_organizations, store, bob_id, requests=4, cost="1.50")
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    await _seed_usage(clean_organizations, store, outsider_id, requests=3, cost="9.00")
    try:
        overview = await alice.get("/user/organization/overview?period=7d", headers=headers)
        members = await alice.get(
            "/user/organization/members?sort_by=spend&order=desc", headers=headers
        )
        detail = await alice.get(f"/user/organization/members/{bob_id}", headers=headers)
        requests = await alice.get("/user/organization/usage/requests?limit=4", headers=headers)
        errors = await alice.get("/user/organization/usage/requests?status=errors", headers=headers)
        models = await alice.get("/user/organization/usage/models", headers=headers)
        by_member = await alice.get("/user/organization/usage/members", headers=headers)
        searched = await alice.get("/user/organization/members?search=BOB", headers=headers)
        everything = await alice.get(
            "/user/organization/members", params={"search": "%"}, headers=headers
        )
        asked_at = datetime.now(UTC)
        hourly = await alice.get("/user/organization/overview?period=1d", headers=headers)
    finally:
        async with clean_organizations.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_daily_cost WHERE user_id = ANY($1::text[])",
                [alice_id, bob_id, outsider_id],
            )

    for response in (overview, members, detail, requests, errors, models, by_member, hourly):
        assert response.status_code == 200, response.text
        # Not what a member sent, nor what it got back, nor a key.
        assert SECRET_PROMPT not in response.text
        assert SECRET_RESPONSE not in response.text
        assert "encrypted-key-material" not in response.text
        assert "request_payload" not in response.text

    body = overview.json()
    # The outsider's nine dollars and three requests are in no figure.
    assert (body["cost_usd"], body["requests"], body["total_tokens"]) == (2.0, 6, 900)
    assert (body["members_with_requests"], body["member_count"]) == (2, 2)
    assert (body["bucket"], len(body["series"])) == ("day", 7)
    today = body["series"][-1]
    assert (today["cost_usd"], today["requests"], today["members_with_requests"]) == (2.0, 6, 2)
    assert all(point["requests"] == 0 for point in body["series"][:-1])
    assert body["previous"] == {"cost_usd": 0.0, "requests": 0}
    assert {model["model_id"]: model["requests"] for model in body["models"]} == {
        "model-a": 3,
        "model-b": 3,
    }
    assert sum(model["errors"] for model in body["models"]) == 2
    # One point for each hour of the UTC day that has begun, and none for one to come.
    assert hourly.json()["bucket"] == "hour"
    assert len(hourly.json()["series"]) in {asked_at.hour + 1, datetime.now(UTC).hour + 1}
    assert sum(point["requests"] for point in hourly.json()["series"]) == 6

    listed = members.json()
    assert listed["total"] == 2
    assert [member["user_id"] for member in listed["members"]] == [bob_id, alice_id]
    first = listed["members"][0]
    assert (first["email"], first["role"], first["status"]) == (BOB, "member", "active")
    assert (first["requests"], first["total_tokens"], first["cost_usd"]) == (4, 600, 1.5)
    assert first["spent_month_usd"] == 1.5
    assert (first["monthly_limit_usd"], first["monthly_limit_source"]) == (None, "none")
    assert first["removed"] is False
    assert first["last_request_at"] is not None
    assert listed["members"][1]["role"] == "admin"
    # The organization manages a member; an administrator is the platform's.
    assert [member["protected"] for member in listed["members"]] == [False, True]
    assert [member["user_id"] for member in searched.json()["members"]] == [bob_id]
    # A percent sign is searched for as it is: no member has one.
    assert (everything.status_code, everything.json()["total"]) == (200, 0)
    assert [member["user_id"] for member in by_member.json()["members"]] == [bob_id, alice_id]

    page = requests.json()
    assert len(page["requests"]) == 4
    assert page["next_before"] is not None
    one = page["requests"][0]
    assert one["key_name"] == "laptop"
    assert one["email"] in (EMAIL, BOB)
    assert set(one) == {
        "id",
        "timestamp",
        "user_id",
        "email",
        "user_name",
        "project_name",
        "key_name",
        "model_id",
        "status_code",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
    }
    rest = await alice.get(
        f"/user/organization/usage/requests?limit=4&before={page['next_before']}", headers=headers
    )
    assert len(rest.json()["requests"]) == 2
    assert rest.json()["next_before"] is None
    assert not {r["id"] for r in rest.json()["requests"]} & {r["id"] for r in page["requests"]}
    assert len(errors.json()["requests"]) == 2
    # A value this route did not return is refused, whatever it looks like:
    # digits that are no number, and an id that no row can have.
    for value in (
        "not-a-cursor",
        "²-5",
        "1700000000000000-²",
        "1700000000000000-9223372036854775808",
    ):
        bad_cursor = await alice.get(
            "/user/organization/usage/requests", params={"before": value}, headers=headers
        )
        assert bad_cursor.status_code == 400, value

    shown = detail.json()
    assert shown["member"]["user_id"] == bob_id
    assert len(shown["days"]) == 30
    assert shown["days"][-1]["cost_usd"] == 1.5
    assert sum(model["requests"] for model in shown["models"]) == 4
    assert len(shown["requests"]) == 4
    assert {request["user_id"] for request in shown["requests"]} == {bob_id}


async def test_the_figures_of_a_member_are_those_of_its_whole_account(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    """The organization owns the account: what it used before it joined is in the figures too."""
    root, root_headers, _ = await _platform_admin(app, store)
    alice, headers, alice_id = await _member(app, idp, store)
    _bob, _bob_headers, bob_id = await _member(app, idp, store, email=BOB, sub="subject-2")
    earlier = datetime.now(UTC) - timedelta(days=3)
    # Bob used the gateway before there was an organization.
    await _seed_usage(clean_organizations, store, bob_id, requests=3, cost="6.00", at=earlier)
    made = await _make_organization(root, root_headers)
    await _set_role(root, root_headers, made["organization"]["id"], alice_id)
    await _seed_usage(clean_organizations, store, bob_id, requests=2, cost="1.00")
    try:
        overview = await alice.get("/user/organization/overview?period=30d", headers=headers)
        members = await alice.get("/user/organization/members?period=30d", headers=headers)
        requests = await alice.get("/user/organization/usage/requests?period=30d", headers=headers)
        detail = await alice.get(f"/user/organization/members/{bob_id}?period=30d", headers=headers)
        in_admin = await root.get("/admin/users", params={"search": BOB}, headers=root_headers)
    finally:
        async with clean_organizations.acquire() as conn:
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id = $1", bob_id)

    assert (overview.json()["requests"], overview.json()["cost_usd"]) == (5, 7.0)
    bob = next(m for m in members.json()["members"] if m["user_id"] == bob_id)
    assert (bob["requests"], bob["cost_usd"]) == (5, 7.0)
    assert len(requests.json()["requests"]) == 5
    shown = detail.json()
    assert len(shown["requests"]) == 5
    assert sum(day["cost_usd"] for day in shown["days"]) == 7.0
    # The two consoles show one figure for all time.
    (as_admin_sees,) = in_admin.json()["users"]
    assert bob["spent_alltime_usd"] == float(as_admin_sees["usage_alltime_usd"]) == 7.0


# -- removing and restoring a member ---------------------------------------------------


async def test_a_removed_member_cannot_sign_in_until_the_organization_restores_it(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, _alice_id = state["alice"]
    bob, _bob_headers, bob_id = state["bob"]
    await store.create_key(
        key_hash=secrets.token_hex(16), key_prefix="sk-bob", user_id=bob_id, account_id=bob_id
    )

    removed = await alice.post(f"/user/organization/members/{bob_id}/remove", headers=headers)

    assert removed.status_code == 200, removed.text
    account = await store.get_user_by_id(bob_id)
    assert account["status"] == "suspended"
    assert account["suspension_message"] == (
        "Removed from Example University by its administrator."
    )
    # Its sessions are over, its key is revoked, and single sign-on refuses it.
    assert (await bob.post("/auth/refresh")).status_code in (401, 403)
    async with clean_organizations.acquire() as conn:
        keys = await conn.fetch("SELECT status FROM api_keys WHERE user_id = $1", bob_id)
    assert [row["status"] for row in keys] == ["revoked"]
    again = new_browser(app)
    refused = await answer(
        again, idp, await start(again, BOB), sub="subject-2", email=BOB, name=BOB
    )
    assert location(refused)[1].get("sso_error") == ["suspended"]
    listed = await alice.get("/user/organization/members?status=removed", headers=headers)
    assert [(m["user_id"], m["removed"]) for m in listed.json()["members"]] == [(bob_id, True)]
    # It is shown as removed, so the list of the suspended members does not have it.
    suspended = await alice.get("/user/organization/members?status=suspended", headers=headers)
    assert suspended.json() == {"total": 0, "members": []}
    by_use = await alice.get(
        "/user/organization/members?sort_by=limit_use&order=desc", headers=headers
    )
    assert by_use.status_code == 200, by_use.text
    assert by_use.json()["total"] == 2
    twice = await alice.post(f"/user/organization/members/{bob_id}/remove", headers=headers)
    assert twice.status_code == 409

    restored = await alice.post(f"/user/organization/members/{bob_id}/restore", headers=headers)

    assert restored.status_code == 200, restored.text
    assert (await store.get_user_by_id(bob_id))["status"] == "active"
    _browser, _headers, same_id = await _member(app, idp, store, email=BOB, sub="subject-2")
    assert same_id == bob_id
    async with clean_organizations.acquire() as conn:
        keys = await conn.fetch("SELECT status FROM api_keys WHERE user_id = $1", bob_id)
    # The revoked key stays revoked.
    assert [row["status"] for row in keys] == ["revoked"]
    not_removed = await alice.post(f"/user/organization/members/{bob_id}/restore", headers=headers)
    assert not_removed.status_code == 409
    actions = await _audit_actions(clean_organizations, state["organization_id"])
    assert actions[-2:] == ["organization.member.remove", "organization.member.restore"]


async def test_an_administrator_is_not_removed_here(app, idp, store, members_sign_straight_in):
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    await _set_role(root, root_headers, state["organization_id"], bob_id)

    other_admin = await alice.post(f"/user/organization/members/{bob_id}/remove", headers=headers)
    itself = await alice.post(f"/user/organization/members/{alice_id}/remove", headers=headers)

    assert (other_admin.status_code, itself.status_code) == (409, 409)
    assert (await store.get_user_by_id(bob_id))["status"] == "active"
    assert (await store.get_user_by_id(alice_id))["status"] == "active"


async def test_what_the_platform_suspends_or_reactivates_is_no_longer_the_organizations(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    restore = f"/user/organization/members/{bob_id}/restore"

    # Suspended in Admin: not the organization's to restore.
    suspended = await root.patch(
        f"/admin/users/{bob_id}", json={"status": "suspended"}, headers=root_headers
    )
    assert suspended.status_code == 200, suspended.text
    assert (await alice.post(restore, headers=headers)).status_code == 409

    # Removed by the organization, made active in Admin, suspended there again.
    await root.patch(f"/admin/users/{bob_id}", json={"status": "active"}, headers=root_headers)
    assert (
        await alice.post(f"/user/organization/members/{bob_id}/remove", headers=headers)
    ).status_code == 200
    reactivated = await root.patch(
        f"/admin/users/{bob_id}", json={"status": "active"}, headers=root_headers
    )
    assert reactivated.status_code == 200, reactivated.text
    assert (await store.get_organization_membership(bob_id))["removed_at"] is None
    await root.patch(f"/admin/users/{bob_id}", json={"status": "suspended"}, headers=root_headers)

    assert (await alice.post(restore, headers=headers)).status_code == 409
    assert (await store.get_user_by_id(bob_id))["status"] == "suspended"


async def test_a_reason_the_platform_writes_takes_a_removal_over(
    app, idp, store, members_sign_straight_in
):
    """An account cannot be suspended twice: the platform's reason is how it keeps one."""
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    assert (
        await alice.post(f"/user/organization/members/{bob_id}/remove", headers=headers)
    ).status_code == 200
    # A note for the administrators changes nothing: the removal is still the organization's.
    noted = await root.patch(
        f"/admin/users/{bob_id}", json={"admin_note": "asked about it"}, headers=root_headers
    )
    assert noted.status_code == 200, noted.text
    assert (await store.get_organization_membership(bob_id))["removed_at"] is not None

    written = await root.patch(
        f"/admin/users/{bob_id}",
        json={"suspension_message": "Suspended by the platform: see ticket 42."},
        headers=root_headers,
    )

    assert written.status_code == 200, written.text
    assert (await store.get_organization_membership(bob_id))["removed_at"] is None
    restored = await alice.post(f"/user/organization/members/{bob_id}/restore", headers=headers)
    assert restored.status_code == 409
    account = await store.get_user_by_id(bob_id)
    assert account["status"] == "suspended"
    assert account["suspension_message"] == "Suspended by the platform: see ticket 42."
    # The organization sees the member as suspended, no longer as removed.
    listed = await alice.get("/user/organization/members?status=suspended", headers=headers)
    assert [member["user_id"] for member in listed.json()["members"]] == [bob_id]


# -- the audit log ---------------------------------------------------------------------


async def test_the_audit_log_shows_the_organizations_rows_and_no_platform_account(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    await alice.post(f"/user/organization/members/{bob_id}/remove", headers=headers)
    # A row of another organization, and one of none.
    await store.create_organization(
        organization_id="o-other", name="Other Organization", actor=PLATFORM
    )
    await store.log_admin_action(
        action="update_user", actor=PLATFORM, details={"note": "of no organization"}
    )

    everything = (await alice.get("/user/organization/audit-log", headers=headers)).json()
    membership = (
        await alice.get("/user/organization/audit-log?category=membership", headers=headers)
    ).json()
    searched = (
        await alice.get("/user/organization/audit-log?search=bob@example", headers=headers)
    ).json()
    by_own_administrator = (
        await alice.get(f"/user/organization/audit-log?search={EMAIL}", headers=headers)
    ).json()
    # The platform administrator made two of the rows; its address is root@elsewhere.org.
    by_platform_address = [
        (await alice.get(f"/user/organization/audit-log?search={guess}", headers=headers)).json()
        for guess in ("root@elsewhere.org", "elsewhere", "root@")
    ]
    paged = (
        await alice.get("/user/organization/audit-log?limit=1&offset=1", headers=headers)
    ).json()

    assert everything["total"] == 3
    assert [entry["action"] for entry in everything["entries"]] == [
        "organization.member.remove",
        "organization.admin.add",
        "organization.create",
    ]
    removal, role, creation = everything["entries"]
    assert (removal["category"], removal["actor"]) == ("membership", "member")
    assert (removal["actor_user_id"], removal["actor_email"]) == (alice_id, EMAIL)
    assert (removal["target_user_id"], removal["target_email"]) == (bob_id, BOB)
    # The platform administrator is named as that, without its account.
    for entry in (role, creation):
        assert entry["actor"] == "platform_administrator"
        assert entry["actor_user_id"] is None
        assert entry["actor_email"] is None
    assert creation["category"] == "organization"
    assert creation["details"]["name"] == "Example University"
    assert [entry["action"] for entry in membership["entries"]] == [
        "organization.member.remove",
        "organization.admin.add",
    ]
    assert [entry["action"] for entry in searched["entries"]] == ["organization.member.remove"]
    # A search finds a row by the address of an administrator of the organization...
    assert "organization.member.remove" in [e["action"] for e in by_own_administrator["entries"]]
    # ...and never by the address of a platform administrator, which no row shows:
    # a match would tell, one guess at a time, what that address is.
    assert [found["total"] for found in by_platform_address] == [0, 0, 0]
    assert (paged["total"], len(paged["entries"])) == (3, 1)
    assert paged["entries"][0]["action"] == "organization.admin.add"


async def test_the_platform_administrator_filters_the_audit_log_by_organization(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]

    listed = await root.get(
        f"/admin/audit-log?organization_id={state['organization_id']}", headers=root_headers
    )

    assert listed.status_code == 200, listed.text
    actions = {entry["action"] for entry in listed.json()["entries"]}
    assert actions == {"organization.create", "organization.admin.add"}


# -- limits ----------------------------------------------------------------------------


async def _carol(app, idp, store):
    return await _member(app, idp, store, email="carol@example.edu", sub="subject-3")


async def test_an_administrator_sets_the_limit_of_a_member_and_the_default(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    _carol_browser, _carol_headers, carol_id = await _carol(app, idp, store)
    await root.patch(
        f"/admin/organizations/{state['organization_id']}",
        json={"monthly_limit_usd": "100"},
        headers=root_headers,
    )
    limit = f"/user/organization/members/{bob_id}/monthly-limit"

    own = await alice.put(limit, json={"monthly_limit_usd": "20"}, headers=headers)
    default = await alice.put(
        "/user/organization/default-member-limit", json={"monthly_limit_usd": "5"}, headers=headers
    )

    assert own.status_code == 200, own.text
    assert default.status_code == 200, default.text
    assert default.json()["default_member_monthly_limit_usd"] == 5.0
    assert default.json()["monthly_limit_usd"] == 100.0
    listed = (await alice.get("/user/organization/members", headers=headers)).json()["members"]
    limits = {m["user_id"]: (m["monthly_limit_usd"], m["monthly_limit_source"]) for m in listed}
    assert limits[bob_id] == (20.0, "member")
    assert limits[carol_id] == (5.0, "default")
    # The default is for the members an organization manages: not for its administrator.
    assert limits[alice_id] == (None, "none")
    assert {m["user_id"]: m["own_monthly_limit_usd"] for m in listed} == {
        alice_id: None,
        bob_id: 20.0,
        carol_id: None,
    }
    assert all(m["platform_monthly_limit_usd"] is None for m in listed)
    async with clean_organizations.acquire() as conn:
        stored = await conn.fetch(
            "SELECT user_id, monthly_limit_usd FROM org_memberships "
            "WHERE user_id = ANY($1::text[])",
            [bob_id, carol_id],
        )
        quotas = await conn.fetch(
            "SELECT quota_monthly_cost_usd FROM account_quotas WHERE user_id = ANY($1::text[])",
            [bob_id, carol_id],
        )
    # The limit of a member is on its membership, and the default is copied to no member.
    assert {row["user_id"]: row["monthly_limit_usd"] for row in stored} == {
        bob_id: Decimal("20.0000"),
        carol_id: None,
    }
    # The quota of an account is the platform administrator's: the organization writes none.
    assert all(row["quota_monthly_cost_usd"] is None for row in quotas)

    cleared = await alice.put(limit, json={"monthly_limit_usd": None}, headers=headers)
    assert cleared.status_code == 200
    listed = (await alice.get("/user/organization/members", headers=headers)).json()["members"]
    assert {m["user_id"]: m["monthly_limit_source"] for m in listed}[bob_id] == "default"

    above = await alice.put(limit, json={"monthly_limit_usd": "100.01"}, headers=headers)
    default_above = await alice.put(
        "/user/organization/default-member-limit",
        json={"monthly_limit_usd": "200"},
        headers=headers,
    )
    on_admin = await alice.put(
        f"/user/organization/members/{alice_id}/monthly-limit",
        json={"monthly_limit_usd": "1"},
        headers=headers,
    )
    stranger = await alice.put(
        "/user/organization/members/no-such-account/monthly-limit",
        json={"monthly_limit_usd": "1"},
        headers=headers,
    )
    nothing = await alice.put(limit, json={}, headers=headers)
    negative = await alice.put(limit, json={"monthly_limit_usd": "-1"}, headers=headers)

    assert (above.status_code, default_above.status_code) == (400, 400)
    assert (on_admin.status_code, stranger.status_code) == (409, 404)
    assert (nothing.status_code, negative.status_code) == (400, 422)
    actions = await _audit_actions(clean_organizations, state["organization_id"])
    assert actions[-4:] == [
        "organization.limit.set",
        "organization.member_limit.set",
        "organization.default_member_limit.set",
        "organization.member_limit.set",
    ]


async def test_an_organization_lowers_the_quota_of_a_member_and_never_raises_it(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    """What the platform administrator sets in Admin is not the organization's to loosen."""
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, headers, _alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    key = generate_api_key()
    await store.create_key(
        key_hash=hash_api_key(key), key_prefix=key[:12], user_id=bob_id, account_id=bob_id
    )
    limited = await root.patch(
        f"/admin/users/{bob_id}", json={"quota_monthly_cost_usd": "10"}, headers=root_headers
    )
    assert limited.status_code == 200, limited.text
    limit = f"/user/organization/members/{bob_id}/monthly-limit"

    async def at_the_door():
        row = await store._store.get_auth_context_with_spend(hash_api_key(key))
        return row["quota_monthly_cost_usd"]

    async def shown() -> tuple:
        listed = (await alice.get("/user/organization/members", headers=headers)).json()
        bob = next(m for m in listed["members"] if m["user_id"] == bob_id)
        return (
            bob["monthly_limit_usd"],
            bob["monthly_limit_source"],
            bob["own_monthly_limit_usd"],
            bob["platform_monthly_limit_usd"],
        )

    async def in_admin():
        async with clean_organizations.acquire() as conn:
            return await conn.fetchval(
                "SELECT quota_monthly_cost_usd FROM account_quotas WHERE user_id = $1", bob_id
            )

    assert await shown() == (10.0, "platform", None, 10.0)

    raised = await alice.put(limit, json={"monthly_limit_usd": "80"}, headers=headers)
    assert raised.status_code == 200, raised.text
    # The organization's wish is kept, and the lower limit is still the platform's.
    assert await shown() == (10.0, "platform", 80.0, 10.0)
    assert (await at_the_door(), await in_admin()) == (Decimal("10.0000"), Decimal("10.0000"))

    cleared = await alice.put(limit, json={"monthly_limit_usd": None}, headers=headers)
    assert cleared.status_code == 200, cleared.text
    # Clearing the organization's limit does not clear the platform's.
    assert await shown() == (10.0, "platform", None, 10.0)
    assert (await at_the_door(), await in_admin()) == (Decimal("10.0000"), Decimal("10.0000"))

    lowered = await alice.put(limit, json={"monthly_limit_usd": "5"}, headers=headers)
    assert lowered.status_code == 200, lowered.text
    assert await shown() == (5.0, "member", 5.0, 10.0)
    assert (await at_the_door(), await in_admin()) == (Decimal("5.0000"), Decimal("10.0000"))


async def test_a_member_without_a_limit_of_its_own_is_held_to_the_default_at_the_door(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    key = generate_api_key()
    await store.create_key(
        key_hash=hash_api_key(key), key_prefix=key[:12], user_id=bob_id, account_id=bob_id
    )
    admin_key = generate_api_key()
    await store.create_key(
        key_hash=hash_api_key(admin_key),
        key_prefix=admin_key[:12],
        user_id=state["alice"][2],
        account_id=state["alice"][2],
    )
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    outsider_key = generate_api_key()
    await store.create_key(
        key_hash=hash_api_key(outsider_key),
        key_prefix=outsider_key[:12],
        user_id=outsider_id,
        account_id=outsider_id,
    )

    async def monthly_quota(api_key: str):
        row = await store._store.get_auth_context_with_spend(hash_api_key(api_key))
        return row["quota_monthly_cost_usd"]

    before = await monthly_quota(key)
    await alice.put(
        "/user/organization/default-member-limit", json={"monthly_limit_usd": "5"}, headers=headers
    )
    with_default = await monthly_quota(key)
    await alice.put(
        f"/user/organization/members/{bob_id}/monthly-limit",
        json={"monthly_limit_usd": "20"},
        headers=headers,
    )
    with_own = await monthly_quota(key)

    # No limit, then the organization's default, then the member's own.
    assert (before, with_default, with_own) == (None, Decimal("5.0000"), Decimal("20.0000"))
    # The default does not hold the administrator that set it.
    assert await monthly_quota(admin_key) is None
    # An account in no organization is not given anyone's default.
    assert await monthly_quota(outsider_key) is None


async def test_what_a_member_is_charged_counts_against_the_organization_and_its_limit_refuses(
    app, idp, store, clean_organizations, members_sign_straight_in, monkeypatch
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    organization_id = state["organization_id"]
    await root.patch(
        f"/admin/organizations/{organization_id}",
        json={"monthly_limit_usd": "1"},
        headers=root_headers,
    )
    key = generate_api_key()
    created = await store.create_key(
        key_hash=hash_api_key(key), key_prefix=key[:12], user_id=bob_id, account_id=bob_id
    )
    outsider_id = await existing_user(store, "someone@elsewhere.org")
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    async def charge(user_id: str, cost: str, key_id: int | None = None) -> None:
        await store.increment_user_costs(
            [CostIncrement(user_id=user_id, day=today, api_key_id=key_id, cost_usd=Decimal(cost))]
        )

    async def door():
        return await verify_api_key(
            request=MagicMock(spec=Request),
            authorization=f"Bearer {key}",
            x_api_key=None,
            x_on_behalf_of=None,
            op_store=store._store,
        )

    try:
        await charge(bob_id, "0.60", created["id"])
        # What an account in no organization is charged reaches no organization.
        await charge(outsider_id, "5.00")
        first = (await alice.get("/user/organization", headers=headers)).json()
        admitted = await door()

        await charge(bob_id, "0.60", created["id"])
        second = (await alice.get("/user/organization/overview", headers=headers)).json()
        with pytest.raises(HTTPException) as refused:
            await door()
        await asyncio.sleep(0)  # let the fire-and-forget rejection log run

        row = await store._store.get_auth_context_with_spend(hash_api_key(key))
        by_hash = await store._store.get_auth_context_by_key_hash(hash_api_key(key))
        by_user = (await store.get_quota_context_for_user(bob_id))[0]
        scope = await store.get_organization_spend_scope(bob_id)
        fresh = await organization_rules.check_fields(store, by_hash, native=False)
        async with clean_organizations.acquire() as conn:
            counters = await conn.fetch(
                "SELECT period, cost_usd, requests FROM spend_counters "
                "WHERE scope_id = $1 ORDER BY period",
                f"o:{organization_id}",
            )
    finally:
        async with clean_organizations.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_daily_cost WHERE user_id = ANY($1::text[])",
                [bob_id, outsider_id],
            )
            await conn.execute("DELETE FROM api_key_daily_cost WHERE user_id = $1", bob_id)

    assert first["spent_month_usd"] == 0.6
    assert admitted["user_id"] == bob_id
    assert (second["spent_month_usd"], second["monthly_limit_usd"]) == (1.2, 1.0)
    # One row, for the month: the one period an organization has a limit for.
    assert [(c["period"][:2], c["cost_usd"], c["requests"]) for c in counters] == [
        ("m:", Decimal("1.200000"), 2),
    ]

    body = refused.value.detail
    assert refused.value.status_code == 429
    assert (body["code"], body["scope"], body["window"]) == (
        "budget_exceeded",
        "organization",
        "month",
    )
    assert body["organization"] == "Example University"
    # The member is told which limit, not how much: the amounts are in the
    # overview of the organization's administrator, read above.
    assert not {"quota_usd", "spent_usd", "remaining_usd"} & set(body)
    assert "X-RateLimit-Limit-Cost" not in refused.value.headers

    # Every lookup an identity is built from carries the same organization.
    expected = {
        "organization_id": organization_id,
        "organization_name": "Example University",
        "organization_limit_usd": 1.0,
        "organization_spent_month_usd": 1.2,
    }
    for lookup in (row, by_user, scope):
        assert quota.organization_fields(lookup, native=False) == expected
    # The cached identity lookup carries the organization and its limit, never
    # its spend: the gate reads the counter for an identity that came without.
    assert "organization_spent_month_usd" not in by_hash
    assert by_hash["organization_limit_usd"] == Decimal("1.0000")
    assert fresh == expected
