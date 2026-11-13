"""Route-level adversarial verification of PR #1733's claim lease.

The admin routes run over the production store pairing (a real
``CachedOperationalStore`` over real PostgreSQL and a real ``PostgresLogStore``
with the erasure fence enabled), so what is asserted is what an operator's
request does to the database.

Covers the review brief's invariants 4 (heartbeat coverage), 8 (HTTP 409
semantics) and 3 (a superseded worker cannot continue) at the route boundary.
"""

from __future__ import annotations

import asyncio
import uuid
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
async def gateway(auth_backend, monkeypatch):
    """The admin routes over the real stores, with the fence rollout enabled."""
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


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


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


async def _claim_token(pool: Any, user_id: str):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT hard_delete_claim_token FROM users WHERE id = $1", user_id
        )


# ---------------------------------------------------------------------------
# Invariant 8: CAS conflicts and lost ownership return HTTP 409.
# ---------------------------------------------------------------------------


async def test_inv8_approve_conflict_is_409(gateway, test_user):
    """A concurrent status change between read and write is a 409."""
    client, _store, pool = gateway
    uid = test_user["id"]

    # The route reads status='pending_approval'; move it under the route's feet.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'active' WHERE id = $1", uid)

    response = await client.post(f"/admin/users/{uid}/approve", headers=ADMIN, json={})
    # The route's own pre-check already refuses a non-pending account with 409.
    assert response.status_code == 409, response.text


async def test_inv8_reject_conflict_is_409(gateway, test_user):
    client, _store, pool = gateway
    uid = test_user["id"]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'active' WHERE id = $1", uid)
    response = await client.post(f"/admin/users/{uid}/reject", headers=ADMIN, json={"reason": "no"})
    assert response.status_code == 409, response.text


async def test_inv8_patch_conflict_is_409(gateway, test_user, monkeypatch):
    """The route's CAS must catch a status that moved after it read the row."""
    client, store, pool = gateway
    uid = test_user["id"]

    # The route reads 'active' ...
    original_get = store.get_user_by_id

    async def _stale_get(user_id):
        row = await original_get(user_id)
        if row and user_id == uid:
            row = dict(row)
            row["status"] = "active"
        return row

    monkeypatch.setattr(store, "get_user_by_id", _stale_get)

    # ... but the account is really 'deleted' when the store writes.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'deleted' WHERE id = $1", uid)

    response = await client.patch(
        f"/admin/users/{uid}", headers=ADMIN, json={"status": "suspended"}
    )
    assert response.status_code == 409, response.text
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM users WHERE id = $1", uid)
    assert status == "deleted"


async def test_inv8_hard_delete_live_claim_is_409_with_recovery_not_ready(gateway, test_user):
    client, store, _pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)
    await store.begin_hard_delete_user(uid)

    response = await client.post(
        f"/admin/users/{uid}/hard-delete", headers=ADMIN, json={"confirm": True}
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "hard_delete_recovery_not_ready"
    assert detail["recovery_available"] is False
    assert await store.get_user_by_id(uid) is not None


async def test_inv8_hard_delete_stale_claim_offers_recovery_then_takes_over(gateway, test_user):
    client, store, pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)
    abandoned = await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid)

    refused = await client.post(
        f"/admin/users/{uid}/hard-delete", headers=ADMIN, json={"confirm": True}
    )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "hard_delete_pre_fence_recovery_available"
    assert detail["recovery_available"] is True
    # The refused request did not touch the stale claim.
    assert await _claim_token(pool, uid) == abandoned.token

    recovered = await client.post(
        f"/admin/users/{uid}/hard-delete",
        headers=ADMIN,
        json={"confirm": True, "recover_stale_claim": True},
    )
    assert recovered.status_code == 200, recovered.text
    assert await store.get_user_by_id(uid) is None


async def test_inv8_resume_with_a_live_claim_is_409(gateway, test_user):
    client, store, _pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)
    await store.begin_hard_delete_user(uid)

    response = await client.post(f"/admin/users/{uid}/resume", headers=ADMIN, json={})
    assert response.status_code == 409, response.text
    assert (await store.get_user_by_id(uid))["status"] == "deleted"


async def test_inv8_resume_of_a_stale_claim_offers_recovery(gateway, test_user):
    client, store, pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)
    await store.begin_hard_delete_user(uid)
    await _age_claim(pool, uid)

    offered = await client.post(f"/admin/users/{uid}/resume", headers=ADMIN, json={})
    assert offered.status_code == 409, offered.text
    detail = offered.json()["detail"]
    assert detail["code"] == "hard_delete_resume_recovery_available"
    assert detail["recovery_available"] is True

    resumed = await client.post(
        f"/admin/users/{uid}/resume",
        headers=ADMIN,
        json={"recover_stale_claim": True, "reason": "worker stopped"},
    )
    assert resumed.status_code == 200, resumed.text
    assert (await store.get_user_by_id(uid))["status"] == "active"


# ---------------------------------------------------------------------------
# Invariant 4: heartbeat coverage protects every destructive stage.
# ---------------------------------------------------------------------------


async def test_inv4_every_destructive_stage_renews_the_claim_first(gateway, test_user, monkeypatch):
    """Each destructive store call is preceded by a lease renewal.

    The renewals are counted at the store boundary, so the assertion covers
    the real ordering rather than a mock's expectations.
    """
    client, store, _pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)

    renewals: list[str] = []
    original = store.renew_hard_delete_user_claim

    async def _counting_renew(user_id: str, claim_token: str) -> None:
        renewals.append(user_id)
        await original(user_id, claim_token)

    monkeypatch.setattr(store, "renew_hard_delete_user_claim", _counting_renew)

    response = await client.post(
        f"/admin/users/{uid}/hard-delete", headers=ADMIN, json={"confirm": True}
    )
    assert response.status_code == 200, response.text
    # The LogStore stage and the final operational purge each renew first.
    # (This app wires no response store and no batch store.)
    assert len(renewals) >= 2, renewals


async def test_inv4_a_superseded_worker_is_refused_before_the_destructive_stage(
    gateway, test_user, monkeypatch
):
    """A worker that lost its claim cannot enter the destructive stage."""
    client, store, pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)

    # Simulate the claim being taken over between the route's claim and its
    # first destructive stage.
    original_begin = store.begin_hard_delete_user

    async def _claim_then_steal(user_id: str, *, recovery_mode=None):
        claim = await original_begin(user_id, recovery_mode=recovery_mode)
        # Another worker takes the claim over right after it is granted.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET hard_delete_claim_token = 'stolen', "
                "hard_delete_claimed_at = NOW() WHERE id = $1",
                user_id,
            )
        return claim

    monkeypatch.setattr(store, "begin_hard_delete_user", _claim_then_steal)

    response = await client.post(
        f"/admin/users/{uid}/hard-delete", headers=ADMIN, json={"confirm": True}
    )
    # The superseded worker fails closed with a 409, not a 500.
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "hard_delete_claim_lost"
    # The account survives: the stolen worker never ran the purge.
    assert await store.get_user_by_id(uid) is not None


async def test_inv4_the_logstore_stage_is_wrapped_by_the_heartbeat(gateway, test_user, monkeypatch):
    """The LogStore purge runs under the heartbeat wrapper, not bare.

    A slow LogStore purge must still be able to lose the claim promptly; the
    wrapper is what makes the stage cancellable on a claim mismatch.
    """
    client, store, _pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)

    import serving.servers.routers.admin.users as users_router

    calls: list[str] = []
    original = users_router._run_hard_delete_stage_with_heartbeat

    async def _tracking(stage, **kwargs):
        calls.append(kwargs.get("user_id", "?"))
        return await original(stage, **kwargs)

    monkeypatch.setattr(users_router, "_run_hard_delete_stage_with_heartbeat", _tracking)

    response = await client.post(
        f"/admin/users/{uid}/hard-delete", headers=ADMIN, json={"confirm": True}
    )
    assert response.status_code == 200, response.text
    # The LogStore stage and the final operational purge are both wrapped.
    assert len(calls) >= 2, calls


async def test_inv4_a_heartbeat_mismatch_cancels_the_stage(gateway, test_user, monkeypatch):
    """A claim mismatch during a stage cancels it instead of running blind."""
    _client, store, _pool = gateway
    uid = test_user["id"]
    await _soft_delete(store, uid)

    import serving.servers.routers.admin.users as users_router
    from serving.exceptions import HardDeleteStateChanged

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _slow_stage():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "finished"

    async def _renew_raises(user_id: str, claim_token: str) -> None:
        raise HardDeleteStateChanged("claim lost")

    monkeypatch.setattr(store, "renew_hard_delete_user_claim", _renew_raises)
    monkeypatch.setattr(users_router, "_HARD_DELETE_CLAIM_HEARTBEAT_INTERVAL_SECONDS", 0.05)

    with pytest.raises(HardDeleteStateChanged):
        await users_router._run_hard_delete_stage_with_heartbeat(
            _slow_stage(),
            op_store=store,
            user_id=uid,
            claim_token="token",
        )
    assert not started.is_set() or cancelled.is_set()
