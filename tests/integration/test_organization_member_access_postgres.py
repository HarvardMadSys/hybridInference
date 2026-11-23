"""What an organization takes from one member: a model, an API key. Over HTTP, on the real store.

An organization only restricts. Its list of models is its own, beside the one
the platform administrator keeps on the account: a model in either is refused,
and neither party writes the other's list. A key is revoked as its owner
revokes it, and is shown by its name, never by its value.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Request

from serving import organizations as organization_rules
from serving.model_access import disabled_models_of
from serving.servers.auth import generate_api_key, hash_api_key, verify_api_key
from serving.servers.deps import get_router
from serving.servers.routers import organization
from tests.integration import test_organizations_postgres as shared
from tests.integration.test_organizations_postgres import (
    _carol,
    _organization_with_an_administrator,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# The fixtures of the organization tests, under the names pytest finds them by.
organization_routes = shared.organization_routes
clean_organizations = shared.clean_organizations
members_sign_straight_in = shared.members_sign_straight_in

MODELS = ("model-a", "model-b", "model-c")


def _route(model_id: str) -> SimpleNamespace:
    adapter = SimpleNamespace(config=SimpleNamespace(id=model_id))
    return SimpleNamespace(adapters=[(adapter, 1.0)])


@pytest.fixture
def deployment_models(app, monkeypatch):
    """A deployment that serves three models, and lists them all to every account."""
    router = SimpleNamespace(routes={name: _route(name) for name in MODELS}, embedding_routes={})
    app.dependency_overrides[get_router] = lambda: router

    async def listed(**context):
        off = context["user_ctx"]["disabled_models"]
        return SimpleNamespace(
            data=[SimpleNamespace(id=name) for name in MODELS if name not in off]
        )

    monkeypatch.setattr(organization, "_build_model_list_async", listed)
    yield router
    app.dependency_overrides.pop(get_router, None)


async def _key(store, user_id: str, name: str) -> tuple[str, int]:
    key = generate_api_key()
    row = await store.create_key(
        key_hash=hash_api_key(key),
        key_prefix=key[:12],
        user_id=user_id,
        account_id=user_id,
        name=name,
    )
    return key, row["id"]


async def _at_the_door(store, key: str) -> list[str]:
    context = await verify_api_key(
        request=MagicMock(spec=Request),
        authorization=f"Bearer {key}",
        x_api_key=None,
        x_on_behalf_of=None,
        op_store=store._store,
    )
    return context["disabled_models"]


async def _audit(pool, action: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT details FROM admin_audit_log WHERE action = $1 ORDER BY id", action
        )
    return [json.loads(row["details"]) for row in rows]


# -- a model -----------------------------------------------------------------------


async def test_an_organization_turns_a_model_off_for_a_member_and_never_one_on(
    app, idp, store, clean_organizations, members_sign_straight_in, deployment_models, monkeypatch
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    state = await _organization_with_an_administrator(app, idp, store)
    root, root_headers = state["root"]
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    key, _ = await _key(store, bob_id, "laptop")
    models = f"/user/organization/members/{bob_id}/models"
    by_platform = await root.patch(
        f"/admin/users/{bob_id}", json={"disabled_models": ["model-b"]}, headers=root_headers
    )
    assert by_platform.status_code == 200, by_platform.text
    # The cached lookup of the key, which the change below must not leave behind.
    assert disabled_models_of(await store.get_auth_context_by_key_hash(hash_api_key(key))) == [
        "model-b"
    ]

    before = await alice.get(models, headers=headers)
    changed = await alice.put(
        models, json={"disabled_models": ["model-a", "no-such-model"]}, headers=headers
    )

    assert before.json() == {
        "available": ["model-a", "model-c"],
        "disabled_models": [],
        "platform_disabled_models": ["model-b"],
    }
    assert changed.status_code == 200, changed.text
    assert changed.json()["disabled_models"] == ["model-a"]
    assert changed.json()["platform_disabled_models"] == ["model-b"]
    both = ["model-a", "model-b"]
    # Every way the request path reads the account refuses both.
    assert await _at_the_door(store, key) == both
    for lookup in (
        store.get_auth_context_by_key_hash,
        store.get_auth_context_with_spend,
        store.get_auth_context_lightweight,
    ):
        assert disabled_models_of(await lookup(hash_api_key(key))) == both, lookup.__name__
    assert disabled_models_of(await store.get_user_by_id(bob_id)) == both
    assert await organization_rules.disabled_models_of_account(store, bob_id) == both
    # The list of the platform administrator is as it was, in Admin too.
    in_admin = await root.get(f"/admin/users/{bob_id}/detail", headers=root_headers)
    assert in_admin.json()["disabled_models"] == ["model-b"]
    detail = await alice.get(f"/user/organization/members/{bob_id}", headers=headers)
    assert detail.json()["disabled_models"] == ["model-a"]
    assert detail.json()["platform_disabled_models"] == ["model-b"]

    cleared = await alice.put(models, json={"disabled_models": []}, headers=headers)

    assert cleared.status_code == 200, cleared.text
    # Nothing of the organization's is off any more; the platform's model stays off.
    assert await _at_the_door(store, key) == ["model-b"]
    assert disabled_models_of(await store.get_auth_context_by_key_hash(hash_api_key(key))) == [
        "model-b"
    ]
    assert [
        row["disabled_models"]
        for row in await _audit(clean_organizations, "organization.member_models.set")
    ] == [{"old": [], "new": ["model-a"]}, {"old": ["model-a"], "new": []}]


async def test_the_models_of_an_administrator_and_of_a_stranger_are_not_set(
    app, idp, store, members_sign_straight_in, deployment_models
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, alice_id = state["alice"]
    stranger_id = await shared.existing_user(store, "someone@elsewhere.org")
    body = {"disabled_models": ["model-a"]}

    own = await alice.put(
        f"/user/organization/members/{alice_id}/models", json=body, headers=headers
    )
    other = await alice.put(
        f"/user/organization/members/{stranger_id}/models", json=body, headers=headers
    )
    read = await alice.get(f"/user/organization/members/{stranger_id}/models", headers=headers)

    assert own.status_code == 409
    assert (other.status_code, read.status_code) == (404, 404)
    assert disabled_models_of(await store.get_user_by_id(alice_id)) == []


# -- a key -------------------------------------------------------------------------


async def test_an_organization_sees_the_keys_of_a_member_by_name_and_revokes_one(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    _carol_browser, _carol_headers, carol_id = await _carol(app, idp, store)
    laptop, laptop_id = await _key(store, bob_id, "laptop")
    server, server_id = await _key(store, bob_id, "server")
    _, carols_id = await _key(store, carol_id, "carol's")
    _, alices_id = await _key(store, alice_id, "alice's")
    # Both keys are in the cache of the lookups before one is revoked.
    assert await store.get_auth_context_by_key_hash(hash_api_key(laptop)) is not None
    assert await store.get_auth_context_by_key_hash(hash_api_key(server)) is not None

    shown = await alice.get(f"/user/organization/members/{bob_id}", headers=headers)

    assert shown.status_code == 200, shown.text
    detail = shown.json()
    assert detail["account"] == {
        "has_key": True,
        "two_factor_enabled": False,
        "email_verified": True,
    }
    assert {key["name"]: key["status"] for key in detail["keys"]} == {
        "laptop": "active",
        "server": "active",
    }
    assert {key["id"] for key in detail["keys"]} == {laptop_id, server_id}
    assert [project["is_default"] for project in detail["projects"]] == [True]
    # By its name only: no value, and not the prefix that begins it.
    assert laptop[:12] not in shown.text and server[:12] not in shown.text
    assert "key_prefix" not in shown.text and "key_masked" not in shown.text
    assert "budget" not in shown.text and "allowed_models" not in shown.text

    def revoke(user_id: str, key_id: int):
        return alice.post(
            f"/user/organization/members/{user_id}/keys/{key_id}/revoke", headers=headers
        )

    revoked = await revoke(bob_id, laptop_id)
    again = await revoke(bob_id, laptop_id)
    not_bobs = await revoke(bob_id, carols_id)
    an_administrators = await revoke(alice_id, alices_id)

    assert revoked.status_code == 200, revoked.text
    assert (again.status_code, not_bobs.status_code, an_administrators.status_code) == (
        409,
        404,
        409,
    )
    # The revoked key is refused at once, and only that one.
    assert await store.get_auth_context_by_key_hash(hash_api_key(laptop)) is None
    assert await store.get_auth_context_by_key_hash(hash_api_key(server)) is not None
    after = (await alice.get(f"/user/organization/members/{bob_id}", headers=headers)).json()
    assert {key["name"]: key["status"] for key in after["keys"]} == {
        "laptop": "revoked",
        "server": "active",
    }
    async with clean_organizations.acquire() as conn:
        statuses = await conn.fetch(
            "SELECT id, status FROM api_keys WHERE id = ANY($1::bigint[])",
            [carols_id, alices_id],
        )
    assert {row["status"] for row in statuses} == {"active"}
    recorded = await _audit(clean_organizations, "organization.member_key.revoke")
    assert [row["key_name"] for row in recorded] == ["laptop"]
    assert laptop[:12] not in json.dumps(recorded)


async def test_the_audit_log_of_the_organization_has_the_category_access(
    app, idp, store, members_sign_straight_in, deployment_models
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    _, key_id = await _key(store, bob_id, "laptop")
    await alice.put(
        f"/user/organization/members/{bob_id}/models",
        json={"disabled_models": ["model-c"]},
        headers=headers,
    )
    await alice.post(f"/user/organization/members/{bob_id}/keys/{key_id}/revoke", headers=headers)

    listed = await alice.get("/user/organization/audit-log?category=access", headers=headers)

    assert listed.status_code == 200, listed.text
    entries = listed.json()["entries"]
    assert [entry["action"] for entry in entries] == [
        "organization.member_key.revoke",
        "organization.member_models.set",
    ]
    assert {entry["category"] for entry in entries} == {"access"}
    assert entries[1]["details"]["disabled_models"] == {"old": [], "new": ["model-c"]}
    assert entries[0]["details"]["key_name"] == "laptop"
