"""Admin account decisions take effect on a real key, through the real cache.

``tests/servers/test_admin_users.py`` checks the routes' branches with the
store mocked. Here the store is the production pairing (``CachedOperationalStore``
over PostgreSQL) and the key is checked by the real ``verify_api_key``, so what
is asserted is what a caller sees: a key the gateway has already accepted (and
cached) stops working the moment an admin suspends its owner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import admin as admin_router

pytest_plugins = ["tests.servers.conftest_auth"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

ADMIN = {"Authorization": "Bearer test-admin-token"}


@pytest_asyncio.fixture
async def gateway(auth_backend, monkeypatch):
    """The admin routes and one key-authenticated route over the real stores."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    operational_store, log_store, db_logger, _ = auth_backend
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

    @app.get("/whoami")
    async def whoami(user_ctx: dict = Depends(verify_api_key)) -> dict[str, Any]:
        return {"user_id": user_ctx["user_id"]}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, operational_store, db_logger.pool


def _key(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['api_key']}"}


async def _audit(pool: Any, user_id: str, action: str) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, details FROM admin_audit_log WHERE target_user_id = $1 AND action = $2",
            user_id,
            action,
        )
    return [dict(row) for row in rows]


async def test_suspending_a_user_stops_their_cached_key_at_once(gateway, test_user_with_key):
    client, _store, pool = gateway
    user = test_user_with_key

    # Accepted, and now cached as a valid auth context.
    assert (await client.get("/whoami", headers=_key(user))).json() == {"user_id": user["id"]}

    response = await client.patch(
        f"/admin/users/{user['id']}", headers=ADMIN, json={"status": "suspended"}
    )
    assert response.status_code == 200, response.text

    assert (await client.get("/whoami", headers=_key(user))).status_code in (401, 403)
    async with pool.acquire() as conn:
        statuses = await conn.fetch("SELECT status FROM api_keys WHERE user_id = $1", user["id"])
    assert {row["status"] for row in statuses} == {"revoked"}
    assert await _audit(pool, user["id"], "update_user")


async def test_resuming_a_suspended_user_does_not_revive_the_revoked_key(
    gateway, test_user_with_key
):
    """Suspension revokes the key rather than pausing it: resuming restores the
    account, and the owner issues a new key. Recorded so that changing it is a
    decision."""
    client, store, _pool = gateway
    user = test_user_with_key
    for status in ("suspended", "active"):
        response = await client.patch(
            f"/admin/users/{user['id']}", headers=ADMIN, json={"status": status}
        )
        assert response.status_code == 200, response.text

    assert (await store.get_user_by_id(user["id"]))["status"] == "active"
    assert (await client.get("/whoami", headers=_key(user))).status_code in (401, 403)


@pytest.mark.parametrize(("decision", "status"), [("approve", "active"), ("reject", "rejected")])
async def test_a_pending_signup_is_decided_once_and_audited(gateway, test_user, decision, status):
    client, store, pool = gateway
    await store.update_user_fields(test_user["id"], status="pending_approval")
    body = {"reason": "not a fit for the beta"} if decision == "reject" else {"note": "welcome"}

    response = await client.post(
        f"/admin/users/{test_user['id']}/{decision}", headers=ADMIN, json=body
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == status
    assert (await store.get_user_by_id(test_user["id"]))["status"] == status
    [audit] = await _audit(pool, test_user["id"], f"{decision}_user")
    assert next(iter(body.values())) in str(audit["details"])

    # Decided: a second decision either way is a conflict, not a silent flip.
    again = await client.post(
        f"/admin/users/{test_user['id']}/approve", headers=ADMIN, json={"note": "again"}
    )
    assert again.status_code == 409


async def test_a_quota_change_lands_on_the_live_key(gateway, test_user_with_key):
    client, _store, pool = gateway
    user = test_user_with_key

    response = await client.patch(
        f"/admin/users/{user['id']}", headers=ADMIN, json={"quota_daily_cost_usd": 7.5}
    )

    assert response.status_code == 200, response.text
    async with pool.acquire() as conn:
        quota = await conn.fetchval(
            "SELECT quota_daily_cost_usd FROM api_keys WHERE user_id = $1 AND status = 'active'",
            user["id"],
        )
    assert float(quota) == 7.5


async def test_suspending_a_user_signs_out_every_session(gateway, test_user, auth_backend):
    """Their sessions are revoked, not only their key, and resuming does not restore them."""
    client, store, pool = gateway
    for _ in range(2):  # two devices
        await store.create_session(
            session_id=f"s-{uuid4()}",
            user_id=test_user["id"],
            refresh_token_hash=f"h-{uuid4()}",
            jti=str(uuid4()),
            sid=str(uuid4()),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

    async def _live() -> int:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM auth_sessions WHERE user_id = $1 AND revoked IS NOT TRUE",
                test_user["id"],
            )

    assert await _live() == 2
    for status in ("suspended", "active"):
        response = await client.patch(
            f"/admin/users/{test_user['id']}", headers=ADMIN, json={"status": status}
        )
        assert response.status_code == 200, response.text
        assert await _live() == 0


async def test_a_suspension_that_fails_part_way_commits_nothing(gateway, test_user_with_key):
    """Status, key and sessions are one transaction (review of #1579).

    When the session revocation failed after the status had been committed on
    its own, the account was left suspended, a retried suspension was refused
    (suspended -> suspended is no transition), and resuming brought the old
    sessions back. A trigger makes that revocation fail for this user only.
    """
    client, store, pool = gateway
    user = test_user_with_key
    await store.create_session(
        session_id=f"s-{uuid4()}",
        user_id=user["id"],
        refresh_token_hash=f"h-{uuid4()}",
        jti=str(uuid4()),
        sid=str(uuid4()),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    tag = uuid4().hex[:12]
    function, trigger = f"fail_session_revoke_{tag}", f"fail_session_revoke_{tag}"

    async def _state() -> tuple[str, set[str], int]:
        async with pool.acquire() as conn:
            status = await conn.fetchval("SELECT status FROM users WHERE id = $1", user["id"])
            keys = await conn.fetch("SELECT status FROM api_keys WHERE user_id = $1", user["id"])
            live = await conn.fetchval(
                "SELECT count(*) FROM auth_sessions WHERE user_id = $1 AND revoked IS NOT TRUE",
                user["id"],
            )
        return status, {row["status"] for row in keys}, live

    async with pool.acquire() as conn:
        # Identifiers and the id cannot be bound in DDL; all three are generated here.
        await conn.execute(
            f"CREATE FUNCTION {function}() RETURNS trigger AS $$ BEGIN "
            f"IF NEW.user_id = '{user['id']}' THEN RAISE EXCEPTION 'injected failure'; END IF; "
            f"RETURN NEW; END $$ LANGUAGE plpgsql"
        )
        await conn.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE ON auth_sessions "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    try:
        try:
            failed = await client.patch(
                f"/admin/users/{user['id']}", headers=ADMIN, json={"status": "suspended"}
            )
            assert failed.status_code >= 500
        except Exception as exc:  # the transport may re-raise the app's exception
            assert "injected failure" in str(exc)

        assert await _state() == ("active", {"active"}, 1)  # nothing half-applied
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f"DROP TRIGGER IF EXISTS {trigger} ON auth_sessions")
            await conn.execute(f"DROP FUNCTION IF EXISTS {function}()")

    retried = await client.patch(
        f"/admin/users/{user['id']}", headers=ADMIN, json={"status": "suspended"}
    )
    assert retried.status_code == 200, retried.text  # not a 409
    assert await _state() == ("suspended", {"revoked"}, 0)

    resumed = await client.patch(
        f"/admin/users/{user['id']}", headers=ADMIN, json={"status": "active"}
    )
    assert resumed.status_code == 200, resumed.text
    assert await _state() == ("active", {"revoked"}, 0)  # the old session stays revoked
