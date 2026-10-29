"""Audit attribution against real PostgreSQL: migration, round trip, erasure.

The migration runs on a legacy-shaped ``admin_audit_log`` in a throwaway
schema, so altering it never touches the table other tests share. The round
trip and lifecycle tests use the store on the shared table, tagging their
rows (``itest.attr.*`` actions, ``u-attr-*`` users) and removing them after.
"""

from __future__ import annotations

import json
import secrets
from types import SimpleNamespace

import pytest
import pytest_asyncio

from serving.audit import AuditActor, AuditTarget, audit_columns
from serving.servers.auth import log_admin_action
from serving.storage.audit_schema import (
    ATTRIBUTION_COLUMNS,
    ensure_admin_audit_log_schema,
    insert_audit_row,
)
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.log_schema import existing_columns
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_LEGACY_TABLE = """
    CREATE TABLE admin_audit_log (
        id BIGSERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        admin_ip TEXT NOT NULL,
        action TEXT NOT NULL,
        target_user_id TEXT,
        details JSONB,
        success BOOLEAN DEFAULT TRUE
    )
"""


@pytest_asyncio.fixture
async def legacy_conn():
    """A connection whose search path finds a legacy-shaped table in its own schema."""
    schema = f"audit_attr_{secrets.token_hex(4)}"
    conn = await postgres.connect()
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}")
        await conn.execute(_LEGACY_TABLE)
        yield conn
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def test_the_migration_is_additive_idempotent_and_keeps_old_rows(legacy_conn):
    conn = legacy_conn
    # Rows as legacy writers left them: an email where an address belongs, and
    # a provider name where a user id belongs.
    await conn.execute(
        "INSERT INTO admin_audit_log (admin_ip, action, target_user_id, details) VALUES "
        "('ops@example.test', 'resume_user', 'u-1', '{\"admin_id\": \"ops@example.test\"}'), "
        "('192.0.2.3', 'providers.disabled.update', 'openrouter', NULL)"
    )
    before = [dict(r) for r in await conn.fetch("SELECT * FROM admin_audit_log ORDER BY id")]

    await ensure_admin_audit_log_schema(conn)
    await ensure_admin_audit_log_schema(conn)  # a second boot changes nothing

    assert set(ATTRIBUTION_COLUMNS) <= await existing_columns(conn, "admin_audit_log")
    after = [dict(r) for r in await conn.fetch("SELECT * FROM admin_audit_log ORDER BY id")]
    assert len(after) == len(before)
    for old, new in zip(before, after, strict=True):
        assert {key: new[key] for key in old} == old
        # Nothing is backfilled from the legacy values: not recorded.
        assert all(new[column] is None for column in ATTRIBUTION_COLUMNS)


async def test_new_rows_on_a_migrated_table_carry_their_attribution(legacy_conn):
    conn = legacy_conn
    await ensure_admin_audit_log_schema(conn)
    columns = audit_columns(
        admin_ip=None,
        target_user_id=None,
        actor=AuditActor.admin("u-admin", "192.0.2.20"),
        target=AuditTarget.model("glm-4.6"),
    )

    await insert_audit_row(
        conn, columns=columns, action="models.concurrency.update", details=None, success=True
    )

    row = await conn.fetchrow("SELECT * FROM admin_audit_log")
    assert row["admin_ip"] == row["source_ip"] == "192.0.2.20"
    assert (row["actor_type"], row["actor_id"]) == ("admin", "u-admin")
    assert (row["target_type"], row["target_id"], row["target_user_id"]) == (
        "model",
        "glm-4.6",
        None,
    )


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=3)
    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM admin_audit_log WHERE action LIKE 'itest.attr.%' "
                "OR target_user_id LIKE 'u-attr-%' OR actor_id LIKE 'u-attr-%'"
            )
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-attr-%'")

    await _wipe()
    try:
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


@pytest.mark.parametrize("writer_kind", ["store", "cache", "legacy_pool"])
async def test_the_store_round_trips_attribution_and_legacy_rows(store, writer_kind):
    op_store, pool = store
    writer = {
        "store": op_store,
        "cache": CachedOperationalStore(op_store, InMemoryCache()),
        "legacy_pool": SimpleNamespace(pool=pool),
    }[writer_kind]
    await log_admin_action(
        writer,
        action="itest.attr.new",
        actor=AuditActor.admin("u-attr-admin", "192.0.2.30"),
        target=AuditTarget.provider("openrouter"),
        details={"disabled": True},
    )
    await log_admin_action(writer, "ops@example.test", "itest.attr.legacy", "openrouter")

    _total, new_rows = await op_store.list_audit_log(action="itest.attr.new")
    _total, legacy_rows = await op_store.list_audit_log(action="itest.attr.legacy")

    [new] = new_rows
    assert (new["actor_type"], new["actor_id"], new["source_ip"]) == (
        "admin",
        "u-attr-admin",
        "192.0.2.30",
    )
    assert (new["target_type"], new["target_id"], new["target_user_id"]) == (
        "provider",
        "openrouter",
        None,
    )
    [legacy] = legacy_rows
    assert legacy["admin_ip"] == "ops@example.test"
    assert legacy["target_user_id"] == "openrouter"
    assert all(legacy[column] is None for column in ATTRIBUTION_COLUMNS)
    # The user filter matches only rows whose target_user_id says so.
    total, _rows = await op_store.list_audit_log(
        action="itest.attr.new", target_user_id="openrouter"
    )
    assert total == 0


async def test_lifecycle_rows_are_attributed_and_erasure_spares_actor_rows(store):
    """Soft delete, resume and hard delete name their actor; erasure keeps rows by actors."""
    op_store, pool = store
    admin = AuditActor.admin("u-attr-admin", "192.0.2.31")
    await op_store.create_user(user_id="u-attr-target", email="t@attr.test", password_hash="x")
    await op_store.create_user(user_id="u-attr-actor", email="a@attr.test", password_hash="x")
    # Something the soon-erased account did, as an actor, to another object.
    await op_store.log_admin_action(
        action="itest.attr.by_actor",
        actor=AuditActor.user("u-attr-target", "192.0.2.32"),
        target=AuditTarget.model("glm-4.6"),
    )

    await op_store.delete_user("u-attr-target", admin_id="ops", actor=admin)
    await op_store.resume_user("u-attr-target", admin_id="ops", actor=admin)
    await op_store.delete_user("u-attr-target", admin_id="ops", actor=admin)
    async with pool.acquire() as conn:
        lifecycle = await conn.fetch(
            "SELECT action, actor_type, actor_id, target_type, details FROM admin_audit_log "
            "WHERE target_user_id = 'u-attr-target' ORDER BY id"
        )
    assert [r["action"] for r in lifecycle] == ["delete_user", "resume_user", "delete_user"]
    for r in lifecycle:
        assert (r["actor_type"], r["actor_id"], r["target_type"]) == (
            "admin",
            "u-attr-admin",
            "user",
        )
    assert json.loads(lifecycle[0]["details"])["admin_id"] == "ops"

    claim = await op_store.begin_hard_delete_user("u-attr-target")
    await op_store.hard_delete_user(
        "u-attr-target", claim_token=claim.token, admin_id="ops", actor=admin
    )

    async with pool.acquire() as conn:
        about_target = await conn.fetch(
            "SELECT action, actor_id FROM admin_audit_log WHERE target_user_id = 'u-attr-target'"
        )
        by_target = await conn.fetchval(
            "SELECT count(*) FROM admin_audit_log WHERE actor_id = 'u-attr-target'"
        )
    # The account's own history goes, save the record of its erasure...
    assert [(r["action"], r["actor_id"]) for r in about_target] == [
        ("hard_delete_user", "u-attr-admin")
    ]
    # ...and what it did to other objects keeps naming it: no actor cascade.
    assert by_target == 1
