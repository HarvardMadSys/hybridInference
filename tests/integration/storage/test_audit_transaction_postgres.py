"""Account mutations must roll back when PostgreSQL rejects their audit row."""

from __future__ import annotations

import secrets

import asyncpg
import pytest
import pytest_asyncio

from serving.audit import AuditActor, AuditTarget
from serving.storage import account_schema
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_USER_ID = "u-audit-transaction"
_ADMIN = AuditActor.admin("u-transaction-admin", "192.0.2.40")


@pytest_asyncio.fixture
async def store(monkeypatch):
    """Keep the deliberately rejecting constraint out of every other test's tables."""
    monkeypatch.setattr(account_schema, "_current", account_schema.COMPLETE)
    admin = await postgres.connect()
    schema = f"audit_transaction_{secrets.token_hex(4)}"
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await postgres.create_pool(
            min_size=1, max_size=2, server_settings={"search_path": schema}
        )
        op_store = PostgresOperationalStore(pool)
        await op_store.initialize()
        yield op_store, pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _reject_audit_inserts(pool) -> None:
    async with pool.acquire() as conn:
        # Existing history remains valid for this test; only the attempted
        # mutation's new audit INSERT fails, inside the real SQL transaction.
        await conn.execute(
            "ALTER TABLE admin_audit_log ADD CONSTRAINT audit_insert_rejected "
            "CHECK (FALSE) NOT VALID"
        )


async def _allow_audit_inserts(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE admin_audit_log DROP CONSTRAINT audit_insert_rejected")


async def _account_state(pool) -> dict[str, list[dict]]:
    async with pool.acquire() as conn:
        return {
            table: [dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("users", "api_keys", "auth_sessions", "admin_audit_log")
        }


@pytest.mark.parametrize("operation", ["delete_user", "resume_user", "hard_delete_user"])
async def test_lifecycle_rolls_back_when_audit_insert_fails(store, operation):
    op_store, pool = store
    await op_store.create_user(
        user_id=_USER_ID, email="transaction@example.test", password_hash="test-hash"
    )
    async with pool.acquire() as conn:
        if operation != "delete_user":
            await conn.execute("UPDATE users SET status = 'deleted' WHERE id = $1", _USER_ID)
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id) "
            "VALUES ('transaction-key-hash', 'transaction-key', $1, $1)",
            _USER_ID,
        )
        await conn.execute(
            "INSERT INTO auth_sessions (id, user_id, refresh_token_hash, sid, expires_at) "
            "VALUES ('transaction-session', $1, 'transaction-refresh', 'transaction-sid', "
            "NOW() + INTERVAL '1 day')",
            _USER_ID,
        )
    await op_store.log_admin_action(
        action="itest.audit.transaction.history", actor=_ADMIN, target=AuditTarget.user(_USER_ID)
    )
    kwargs = {"actor": _ADMIN}
    if operation == "hard_delete_user":
        kwargs["claim_token"] = (await op_store.begin_hard_delete_user(_USER_ID)).token
    before = await _account_state(pool)
    await _reject_audit_inserts(pool)

    with pytest.raises(asyncpg.CheckViolationError) as rejected:
        await getattr(op_store, operation)(_USER_ID, **kwargs)

    assert rejected.value.constraint_name == "audit_insert_rejected"
    # Account state, key revocation, session erasure and prior audit history
    # are all restored, including the hard-delete claim needed for a retry.
    assert await _account_state(pool) == before

    await _allow_audit_inserts(pool)
    await getattr(op_store, operation)(_USER_ID, **kwargs)
    total, [row] = await op_store.list_audit_log(action=operation, target_user_id=_USER_ID)
    assert total == 1
    assert (row["actor_type"], row["actor_id"], row["source_ip"]) == (
        "admin",
        _ADMIN.id,
        _ADMIN.source_ip,
    )
    assert (row["target_type"], row["target_id"]) == ("user", _USER_ID)
    user = await op_store.get_user_by_id(_USER_ID)
    if operation == "hard_delete_user":
        assert user is None
    else:
        assert user["status"] == ("deleted" if operation == "delete_user" else "active")


async def test_setup_rolls_back_account_marker_and_code_when_audit_insert_fails(store):
    op_store, pool = store
    marker_key = "setup_completed_at"
    code_key = "setup_code"
    code = await op_store.get_or_create_setup_code(
        marker_key=marker_key,
        code_key=code_key,
        completed_at="2026-10-25T00:00:00+00:00",
        candidate_code="AUDITTESTSETUP",
    )
    args = {
        "user_id": _USER_ID,
        "login_name": "AuditAdmin",
        "password_hash": "test-hash",
        "user_name": "Audit administrator",
        "marker_key": marker_key,
        "marker_value": "2026-10-25T00:00:00+00:00",
        "code_key": code_key,
        "actor": AuditActor.bootstrap("192.0.2.41"),
    }
    before = await _account_state(pool)
    code_before = await op_store.get_setting(code_key)
    await _reject_audit_inserts(pool)

    with pytest.raises(asyncpg.CheckViolationError) as rejected:
        await op_store.create_first_admin(**args)

    assert rejected.value.constraint_name == "audit_insert_rejected"
    assert await _account_state(pool) == before
    assert await op_store.get_setting(marker_key) is None
    assert await op_store.get_setting(code_key) == code_before
    assert code_before["value"] == code

    await _allow_audit_inserts(pool)
    assert await op_store.create_first_admin(**args) is True
    assert await op_store.get_setting(code_key) is None
    assert (await op_store.get_setting(marker_key))["value"] == args["marker_value"]
    total, [row] = await op_store.list_audit_log(action="setup.admin_created")
    assert total == 1
    assert (row["actor_type"], row["actor_id"], row["source_ip"]) == (
        "bootstrap",
        None,
        "192.0.2.41",
    )
    assert (row["target_type"], row["target_id"], row["target_user_id"]) == (
        "user",
        _USER_ID,
        _USER_ID,
    )
