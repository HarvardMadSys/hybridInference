"""Hard delete and resume through the real admin routes over real PostgreSQL.

``tests/servers/test_admin_users.py`` checks the routes' branches with the
store mocked. Here the app runs the production pairing — the admin router over
``CachedOperationalStore`` and ``PostgresLogStore`` — so what is asserted is
what an operator's request does to the database: a live claim refuses the
request, an expired lease offers recovery, and only the confirmed recovery (or
a durable erasure fence) takes the claim over.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.config.settings import get_settings
from serving.servers.deps import AppServices
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import admin as admin_router
from tests.fixtures import postgres

pytest_plugins = ["tests.servers.conftest_auth"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

ADMIN = {"Authorization": "Bearer test-admin-token"}


@pytest_asyncio.fixture
async def hard_delete_gateway(auth_backend, monkeypatch):
    """The admin routes over the real stores, with the fence rollout enabled.

    The log store is rebuilt with the shared test fence secret: the shared
    ``auth_backend`` fixture builds one without a secret, and the erasure
    fence needs one to derive its digests (it fails closed without it).
    """
    monkeypatch.setenv("ERASURE_FENCE_PROTOCOL_READY", "true")
    monkeypatch.setenv("ERASURE_FENCE_SECRET", postgres.FENCE_SECRET)
    get_settings.cache_clear()
    operational_store, _log_store, db_logger, _ = auth_backend
    from serving.storage.postgres_log import PostgresLogStore

    log_store = PostgresLogStore(
        db_logger.pool,
        store_full_prompts=False,
        fence_secret=postgres.FENCE_SECRET,
    )
    app = FastAPI()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=FixedRouter(),
        db_logger=db_logger,
        operational_store=operational_store,
        log_store=log_store,
        routing_manager=None,
    )
    install_exception_handlers(app)
    app.include_router(admin_router.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, operational_store, db_logger.pool
    get_settings.cache_clear()


async def _soft_delete(store, user_id: str) -> None:
    await store.delete_user(user_id, admin_id="ops", reason="test")


async def _age_claim(pool: Any, user_id: str, *, minutes: int = 90) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hard_delete_claimed_at = NOW() - make_interval(mins => $2) "
            "WHERE id = $1",
            user_id,
            minutes,
        )


async def test_a_live_claim_refuses_hard_delete_and_keeps_the_account(
    hard_delete_gateway, test_user
):
    client, store, _pool = hard_delete_gateway
    await _soft_delete(store, test_user["id"])
    await store.begin_hard_delete_user(test_user["id"])

    response = await client.post(
        f"/admin/users/{test_user['id']}/hard-delete",
        headers=ADMIN,
        json={"confirm": True},
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "hard_delete_recovery_not_ready"
    assert detail["recovery_available"] is False
    assert (await store.get_user_by_id(test_user["id"])) is not None


async def test_a_stale_claim_needs_confirmation_before_the_route_takes_it_over(
    hard_delete_gateway, test_user
):
    client, store, pool = hard_delete_gateway
    await _soft_delete(store, test_user["id"])
    abandoned = await store.begin_hard_delete_user(test_user["id"])
    await _age_claim(pool, test_user["id"])

    refused = await client.post(
        f"/admin/users/{test_user['id']}/hard-delete",
        headers=ADMIN,
        json={"confirm": True},
    )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "hard_delete_pre_fence_recovery_available"
    assert detail["recovery_available"] is True
    # The stale claim was not touched by the refused request.
    async with pool.acquire() as conn:
        token = await conn.fetchval(
            "SELECT hard_delete_claim_token FROM users WHERE id = $1", test_user["id"]
        )
    assert token == abandoned.token

    recovered = await client.post(
        f"/admin/users/{test_user['id']}/hard-delete",
        headers=ADMIN,
        json={"confirm": True, "recover_stale_claim": True},
    )
    assert recovered.status_code == 200, recovered.text
    assert (await store.get_user_by_id(test_user["id"])) is None


async def test_a_crashed_workers_claim_is_resumable_only_after_confirmation(
    hard_delete_gateway, test_user
):
    """A worker that dies before the fence must not strand the account forever."""
    client, store, pool = hard_delete_gateway
    await _soft_delete(store, test_user["id"])
    await store.begin_hard_delete_user(test_user["id"])

    live = await client.post(f"/admin/users/{test_user['id']}/resume", headers=ADMIN, json={})
    assert live.status_code == 409, live.text

    await _age_claim(pool, test_user["id"])
    offered = await client.post(f"/admin/users/{test_user['id']}/resume", headers=ADMIN, json={})
    assert offered.status_code == 409, offered.text
    detail = offered.json()["detail"]
    assert detail["code"] == "hard_delete_resume_recovery_available"
    assert detail["recovery_available"] is True

    resumed = await client.post(
        f"/admin/users/{test_user['id']}/resume",
        headers=ADMIN,
        json={"recover_stale_claim": True, "reason": "worker was stopped"},
    )
    assert resumed.status_code == 200, resumed.text
    assert (await store.get_user_by_id(test_user["id"]))["status"] == "active"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hard_delete_pending, hard_delete_claim_token FROM users WHERE id = $1",
            test_user["id"],
        )
    assert row["hard_delete_pending"] is False
    assert row["hard_delete_claim_token"] is None
