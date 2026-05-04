"""Postgres-fixture tests for the login_events table and store methods.

Skipped when PG_TEST_DSN is unset (matches existing storage tests).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("PG_TEST_DSN"),
    reason="PG_TEST_DSN not set; skipping Postgres-fixture tests",
)


@pytest.fixture
async def store() -> PostgresOperationalStore:
    pool = await asyncpg.create_pool(os.environ["PG_TEST_DSN"], min_size=1, max_size=2)
    s = PostgresOperationalStore(pool)
    await s.initialize()
    # Clean slate for the test run.
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM login_events")
    try:
        yield s
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_record_login_event_success(store: PostgresOperationalStore):
    await store.record_login_event(
        email="alice@example.com",
        outcome="success",
        failure_reason=None,
        user_id="u1",
        ip="203.0.113.5",
        user_agent="curl/8",
    )
    async with store._pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM login_events WHERE email=$1", "alice@example.com")
    assert row["outcome"] == "success"
    assert row["failure_reason"] is None
    assert row["user_id"] == "u1"
    assert row["ip"] == "203.0.113.5"
    assert row["user_agent"] == "curl/8"


@pytest.mark.asyncio
async def test_record_login_event_failure_unknown_user(store: PostgresOperationalStore):
    await store.record_login_event(
        email="bogus@example.com",
        outcome="failure",
        failure_reason="user_not_found",
        user_id=None,
        ip=None,
        user_agent=None,
    )
    async with store._pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM login_events WHERE email=$1", "bogus@example.com")
    assert row["user_id"] is None
    assert row["outcome"] == "failure"
    assert row["failure_reason"] == "user_not_found"


@pytest.mark.asyncio
async def test_record_login_event_rejects_bad_outcome(store: PostgresOperationalStore):
    """The CHECK constraint rejects unknown outcome values."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await store.record_login_event(
            email="x@y.com",
            outcome="maybe",
            failure_reason=None,
            user_id=None,
            ip=None,
            user_agent=None,
        )


@pytest.mark.asyncio
async def test_purge_older_than_days(store: PostgresOperationalStore):
    # Insert two rows: one fresh, one 30 days old.
    await store.record_login_event(
        email="fresh@example.com",
        outcome="success",
        failure_reason=None,
        user_id="u_fresh",
        ip=None,
        user_agent=None,
    )
    async with store._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO login_events (created_at, email, outcome, failure_reason, user_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            datetime.now(timezone.utc) - timedelta(days=30),
            "old@example.com",
            "success",
            None,
            "u_old",
        )

    deleted = await store.purge_login_events_older_than(7)
    assert deleted == 1

    async with store._pool.acquire() as conn:
        rows = await conn.fetch("SELECT email FROM login_events ORDER BY created_at")
    assert [r["email"] for r in rows] == ["fresh@example.com"]


@pytest.mark.asyncio
async def test_purge_for_user(store: PostgresOperationalStore):
    for uid, email in (("u1", "a@x.com"), ("u1", "a@x.com"), ("u2", "b@x.com")):
        await store.record_login_event(
            email=email,
            outcome="success",
            failure_reason=None,
            user_id=uid,
            ip=None,
            user_agent=None,
        )
    deleted = await store.purge_login_events_for_user("u1")
    assert deleted == 2
    async with store._pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM login_events")
    assert [r["user_id"] for r in rows] == ["u2"]


@pytest.mark.asyncio
async def test_initialize_is_idempotent(store: PostgresOperationalStore):
    # Calling initialize twice must not raise.
    await store.initialize()
    await store.initialize()


@pytest.mark.asyncio
async def test_hard_delete_user_sweeps_login_events(store: PostgresOperationalStore):
    """hard_delete_user removes the user's login_events rows + reports count."""
    # Seed a target user + an unrelated user, each with login events.
    async with store._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, user_name, password_hash, role, status) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "doomed",
            "d@x.com",
            "Doomed",
            "$2b$12$X",
            "free",
            "active",
        )
        await conn.execute(
            "INSERT INTO users (id, email, user_name, password_hash, role, status) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "kept",
            "k@x.com",
            "Kept",
            "$2b$12$X",
            "free",
            "active",
        )

    for _ in range(3):
        await store.record_login_event(
            email="d@x.com",
            outcome="success",
            failure_reason=None,
            user_id="doomed",
            ip=None,
            user_agent=None,
        )
    await store.record_login_event(
        email="k@x.com",
        outcome="success",
        failure_reason=None,
        user_id="kept",
        ip=None,
        user_agent=None,
    )

    counts = await store.hard_delete_user(
        "doomed",
        admin_ip="127.0.0.1",
        admin_id="admin1",
        reason="test",
        email="d@x.com",
    )
    assert counts.get("login_events") == 3

    async with store._pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM login_events")
    assert {r["user_id"] for r in rows} == {"kept"}
