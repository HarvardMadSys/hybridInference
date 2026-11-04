"""``PATCH /user/api-keys/{key_prefix}`` with a key's budgets and allowlist (P2a)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from routing.routers import FixedRouter
from serving.model_access import DISABLED_MODELS_PREFERENCE_KEY
from serving.servers.deps import get_embedding_adapters, get_router

from .test_completions import DummyAdapter, _mk_cfg

if TYPE_CHECKING:
    from httpx import AsyncClient

pytest_plugins = ["tests.servers.conftest_auth"]
pytestmark = pytest.mark.dbtest

MODEL_A = "key-limits-model-a"
MODEL_B = "key-limits-model-b"
ALIAS_A = "key-limits-alias-a"
EMBED = "key-limits-embed"
EMBED_ALIAS = "key-limits-embed-alias"
EMBED_ADMIN = "key-limits-embed-admin"
EMBED_ADMIN_ALIAS = "key-limits-embed-admin-alias"


@pytest.fixture
def models(auth_test_app):
    """Two models the test user can use, and an alias of the first."""
    router_exec = FixedRouter()
    adapter = DummyAdapter(_mk_cfg(MODEL_A))
    router_exec.register_route(MODEL_A, [(adapter, 1.0)])
    router_exec.register_route(ALIAS_A, [(adapter, 1.0)])
    router_exec.register_route(MODEL_B, [(DummyAdapter(_mk_cfg(MODEL_B)), 1.0)])
    # Embedding models keep their own route table, aliases included.
    embed = DummyAdapter(_mk_cfg(EMBED))
    embed_admin = DummyAdapter(_mk_cfg(EMBED_ADMIN))
    router_exec.register_embedding_route(EMBED, [(embed, 1.0)], aliases=[EMBED_ALIAS])
    router_exec.register_embedding_route(
        EMBED_ADMIN, [(embed_admin, 1.0)], aliases=[EMBED_ADMIN_ALIAS], required_role="admin"
    )
    embedding_adapters = {
        EMBED: embed,
        EMBED_ALIAS: embed,
        EMBED_ADMIN: embed_admin,
        EMBED_ADMIN_ALIAS: embed_admin,
    }
    auth_test_app.dependency_overrides[get_router] = lambda: router_exec
    auth_test_app.dependency_overrides[get_embedding_adapters] = lambda: embedding_adapters
    yield router_exec
    auth_test_app.dependency_overrides.pop(get_router, None)
    auth_test_app.dependency_overrides.pop(get_embedding_adapters, None)


async def _patch(client: AsyncClient, prefix: str, headers: dict, body: dict):
    return await client.patch(f"/user/api-keys/{prefix}", headers=headers, json=body)


async def _listed(client: AsyncClient, headers: dict, prefix: str) -> dict:
    keys = (await client.get("/user/api-keys/all", headers=headers)).json()["keys"]
    return next(key for key in keys if key["key_prefix"] == prefix)


@pytest.mark.asyncio
async def test_limits_are_set_changed_one_at_a_time_and_cleared(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, models
):
    prefix = test_user_with_key["key_prefix"]

    set_all = await _patch(
        auth_app_client,
        prefix,
        auth_headers,
        {"budget_daily_usd": 5, "budget_monthly_usd": 50.5, "allowed_models": [MODEL_B, MODEL_A]},
    )
    assert set_all.status_code == 200
    assert set_all.json() == {
        "key_prefix": prefix,
        "name": None,
        "budget_daily_usd": 5.0,
        "budget_monthly_usd": 50.5,
        "allowed_models": [MODEL_A, MODEL_B],
    }

    # A field left out stays; null clears.
    cleared = await _patch(auth_app_client, prefix, auth_headers, {"budget_daily_usd": None})
    assert cleared.json()["budget_daily_usd"] is None
    assert cleared.json()["budget_monthly_usd"] == 50.5
    assert cleared.json()["allowed_models"] == [MODEL_A, MODEL_B]

    listed = await _listed(auth_app_client, auth_headers, prefix)
    assert (listed["budget_daily_usd"], listed["budget_monthly_usd"]) == (None, 50.5)
    assert listed["allowed_models"] == [MODEL_A, MODEL_B]


@pytest.mark.asyncio
async def test_a_zero_budget_is_accepted(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers
):
    response = await _patch(
        auth_app_client, test_user_with_key["key_prefix"], auth_headers, {"budget_daily_usd": 0}
    )

    assert response.status_code == 200 and response.json()["budget_daily_usd"] == 0.0


@pytest.mark.asyncio
async def test_an_alias_is_stored_as_its_model(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, models
):
    response = await _patch(
        auth_app_client,
        test_user_with_key["key_prefix"],
        auth_headers,
        {"allowed_models": [ALIAS_A, MODEL_A]},
    )

    assert response.json()["allowed_models"] == [MODEL_A]


@pytest.mark.asyncio
async def test_an_embedding_alias_is_stored_as_its_model_once(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, models
):
    """Embedding aliases resolve through the embedding route table, not the chat one."""
    response = await _patch(
        auth_app_client,
        test_user_with_key["key_prefix"],
        auth_headers,
        {"allowed_models": [EMBED_ALIAS, EMBED, MODEL_A]},
    )

    assert response.status_code == 200
    assert response.json()["allowed_models"] == sorted([EMBED, MODEL_A])


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [EMBED_ADMIN, EMBED_ADMIN_ALIAS])
async def test_an_embedding_model_above_the_accounts_role_is_refused(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, models, model
):
    response = await _patch(
        auth_app_client, test_user_with_key["key_prefix"], auth_headers, {"allowed_models": [model]}
    )

    assert response.status_code == 422
    assert model in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [EMBED, EMBED_ALIAS])
async def test_an_embedding_model_the_account_disabled_is_refused(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_backend, models, model
):
    await auth_backend[0].set_user_preference(
        test_user_with_key["id"], DISABLED_MODELS_PREFERENCE_KEY, [EMBED]
    )

    response = await _patch(
        auth_app_client, test_user_with_key["key_prefix"], auth_headers, {"allowed_models": [model]}
    )

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"budget_daily_usd": -1},
        {"budget_monthly_usd": 1_000_001},
        {"budget_daily_usd": "1.00001"},
        {"allowed_models": []},
        {"allowed_models": [""]},
        {"allowed_models": ["not-a-model"]},
    ],
    ids=["empty", "negative", "too-large", "too-precise", "no-models", "blank", "unknown"],
)
async def test_invalid_changes_are_refused_and_change_nothing(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, models, body
):
    prefix = test_user_with_key["key_prefix"]

    response = await _patch(auth_app_client, prefix, auth_headers, body)

    assert response.status_code == 422
    listed = await _listed(auth_app_client, auth_headers, prefix)
    assert (listed["budget_daily_usd"], listed["allowed_models"]) == (None, None)


@pytest.mark.asyncio
async def test_another_accounts_key_is_not_found(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers
):
    response = await _patch(auth_app_client, "hyi-notmine00", auth_headers, {"budget_daily_usd": 1})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_change_is_audited_with_its_old_and_new_values(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_db_logger
):
    prefix = test_user_with_key["key_prefix"]
    await _patch(auth_app_client, prefix, auth_headers, {"budget_daily_usd": 5})
    await _patch(auth_app_client, prefix, auth_headers, {"budget_daily_usd": 7})

    async with auth_db_logger.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT details FROM admin_audit_log "
            "WHERE target_user_id = $1 AND action = 'update_key_settings' "
            "ORDER BY timestamp DESC LIMIT 1",
            test_user_with_key["id"],
        )
    details = row["details"] if isinstance(row["details"], dict) else json.loads(row["details"])
    assert details["key_prefix"] == prefix
    assert details["changed"] == {"budget_daily_usd": {"from": 5.0, "to": 7.0}}


@pytest.mark.asyncio
async def test_regenerating_keeps_the_limits_and_the_spend(
    auth_app_client: AsyncClient, test_user_with_key, auth_headers, auth_backend
):
    """Regenerate changes the key's secret, not the key: its budget still sees what it spent."""
    operational_store = auth_backend[0]
    prefix = test_user_with_key["key_prefix"]
    await _patch(auth_app_client, prefix, auth_headers, {"budget_daily_usd": 5})
    key_id = (await operational_store.list_account_keys(test_user_with_key["id"]))[0]["id"]
    await operational_store.increment_user_cost(test_user_with_key["id"], 1.25, api_key_id=key_id)

    rotated = (
        await auth_app_client.post(f"/user/api-keys/{prefix}/regenerate", headers=auth_headers)
    ).json()

    listed = await _listed(auth_app_client, auth_headers, rotated["key_prefix"])
    assert listed["budget_daily_usd"] == 5.0
    assert listed["spent_today_usd"] == 1.25
