"""Integration tests for ``query_users_at_daily_quota`` against real Postgres.

The predicate here is the quota gate's own — ``spend + estimate > cap`` — so
what matters is the boundary: a user the gate has started refusing must appear,
and a user with one more request left must not. That is a question about SQL
semantics (numeric vs float, the day key's TEXT comparison, the key join), so
it is asked of a real database rather than a mock.

Marked ``dbtest``: excluded from the default suite, run with
``pytest -m dbtest tests/integration/``.

**Unproven in this environment.** These tests have never been executed here —
no Postgres test database is wired up on this host, and the default ``make
test`` deselects the ``dbtest`` marker, so nothing in this file has run even
once. Read every assertion below as *intent*, not as evidence. Anyone with a
test database should run them before trusting the claims they make.

That matters because three behaviours have no other cover anywhere in the
suite. The unit tests in ``tests/unit/storage/test_users_at_daily_quota.py``
mock asyncpg, so they can only check the shape of the SQL string, never what it
selects. These are the cases that only a real database can decide, and only
this file asks:

1. **An expired key is excluded** — ``test_expired_key_is_excluded``. Exercises
   ``k.expires_at > NOW()``, which no mock evaluates.
2. **A revoked key (and a suspended user) is excluded** —
   ``test_suspended_user_and_revoked_key_are_excluded``. Exercises the
   ``status = 'active'`` filters on both sides of the join.
3. **A NULL quota falls back to the enforcer's default of 1000** —
   ``test_null_quota_falls_back_to_the_enforcers_default``. Exercises
   ``COALESCE(k.quota_daily_cost_usd, $2)`` against a real pre-migration row.

If any of those three regressed, the default suite would stay green.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from serving import quota
from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"

_USER_PREFIX = "u-quota-"


@pytest_asyncio.fixture
async def postgres_op_store():
    """A bare PostgresOperationalStore on the test database.

    Same shape as ``tests/integration/storage/test_postgres_role_quota.py`` —
    no cache wrapper, so seeded rows are visible to assertions immediately.
    """
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run against a non-test database."
        )

    if worker_id != "master":
        try:
            admin_conn = await asyncpg.connect(
                host=os.getenv("TEST_DB_HOST", "localhost"),
                port=int(os.getenv("TEST_DB_PORT", "5432")),
                user=os.getenv("TEST_DB_USER", "postgres"),
                password=os.getenv("TEST_DB_PASSWORD", "postgres"),
                database="postgres",
                timeout=5,
            )
            try:
                exists = await admin_conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", test_db_name
                )
                if not exists:
                    await admin_conn.execute(f'CREATE DATABASE "{test_db_name}"')
            finally:
                await admin_conn.close()
        except Exception:
            pass

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable; satisfies the type-checker

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute(f"DELETE FROM user_daily_cost WHERE user_id LIKE '{_USER_PREFIX}%'")
            await conn.execute(f"DELETE FROM api_keys WHERE user_id LIKE '{_USER_PREFIX}%'")
            await conn.execute(f"DELETE FROM users WHERE id LIKE '{_USER_PREFIX}%'")

    await _wipe()
    try:
        yield store
    finally:
        await _wipe()
        await pool.close()


async def _seed(
    store: PostgresOperationalStore,
    *,
    suffix: str,
    role: str = "pro",
    cap: Decimal,
    spend: Decimal,
    day: str | None = None,
    user_status: str = "active",
    key_status: str = "active",
    key_expires_at: datetime | None = None,
) -> str:
    """Create a user, their single active key, and today's cost counter row."""
    user_id = f"{_USER_PREFIX}{suffix}"
    await store.create_user(user_id=user_id, email=f"{suffix}@example.com", password_hash="x")
    # create_user always inserts role='free'; update to the desired role.
    if role != "free":
        await store.update_user_fields(user_id, role=role)
    await store.create_key(
        key_hash=f"hash-{user_id}",
        key_prefix=f"sk-{suffix}",
        user_id=user_id,
        account_id=user_id,
        quota_daily_cost_usd=cap,
        expires_at=key_expires_at,
    )
    # Status last on both rows: update_key refuses to touch a revoked row, so a
    # revoked key cannot be edited afterwards.
    if key_status != "active":
        await store.update_key(user_id, status=key_status)
    if user_status != "active":
        await store.update_user_fields(user_id, status=user_status)
    await store.increment_user_cost(user_id, float(spend), day=day)
    return user_id


def _ids(rows) -> list[str]:
    return [row[0] for row in rows]


async def test_user_the_gate_refuses_is_reported(postgres_op_store):
    """19.99 spent against a 20.00 cap: the next request is already refused."""
    at_cap = await _seed(
        postgres_op_store, suffix="atcap", role="free", cap=Decimal("20.00"), spend=Decimal("19.99")
    )

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert _ids(rows) == [at_cap]
    _, role, spend, cap = rows[0]
    assert role == "free"
    assert spend == pytest.approx(19.99)
    assert cap == pytest.approx(20.00)
    # Belt and braces: the row is here for exactly the reason quota.check
    # would raise on this account's next request.
    with pytest.raises(quota.QuotaExceeded):
        quota.check(quota_usd=cap, spent_usd=spend)


async def test_user_with_one_request_left_is_not_reported(postgres_op_store):
    """One estimate's worth of headroom is still headroom."""
    await _seed(
        postgres_op_store, suffix="under", role="free", cap=Decimal("20.00"), spend=Decimal("19.98")
    )

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert rows == []


async def test_overshoot_past_the_cap_is_reported(postgres_op_store):
    """A last request that cost more than the estimate leaves spend over cap."""
    over = await _seed(
        postgres_op_store, suffix="over", cap=Decimal("80.00"), spend=Decimal("80.18")
    )

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert _ids(rows) == [over]


async def test_caps_differ_within_one_role(postgres_op_store):
    """Why per-role thresholds cannot work, stated as a test.

    Both users are ``pro``. One is finished at 40, the other has 39 dollars
    left at the same spend. No single per-role number separates them.
    """
    capped = await _seed(
        postgres_op_store, suffix="pro40", cap=Decimal("40.00"), spend=Decimal("40.00")
    )
    await _seed(postgres_op_store, suffix="pro80", cap=Decimal("80.00"), spend=Decimal("40.00"))

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert _ids(rows) == [capped]


async def test_ordered_by_overage_not_by_spend(postgres_op_store):
    """The biggest spender is usually just the biggest allowance."""
    small = await _seed(
        postgres_op_store, suffix="small", role="free", cap=Decimal("20.00"), spend=Decimal("25.00")
    )  # $5 over
    large = await _seed(
        postgres_op_store, suffix="large", cap=Decimal("200.00"), spend=Decimal("201.00")
    )  # $1 over, but four times the spend

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert _ids(rows) == [small, large]


async def test_yesterdays_spend_is_not_todays(postgres_op_store):
    """The counter is keyed by TEXT day; only today's row counts."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    await _seed(
        postgres_op_store,
        suffix="yday",
        cap=Decimal("80.00"),
        spend=Decimal("80.00"),
        day=yesterday,
    )

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert rows == []


async def test_suspended_user_and_revoked_key_are_excluded(postgres_op_store):
    """Both are excluded because the enforcer resolves no cap for them.

    ``UserCostOverrunJob`` compensates for an already-reported user: when one
    leaves the result set it re-checks them with ``get_quota_context_for_user``,
    gets the same empty answer this exclusion produces, and holds the incident
    open rather than letting the stale sweep post a false "Recovered". (A user
    who leaves because their cap was *raised* still resolves there, and is
    allowed to close.)
    """
    await _seed(
        postgres_op_store,
        suffix="susp",
        cap=Decimal("80.00"),
        spend=Decimal("80.00"),
        user_status="suspended",
    )
    await _seed(
        postgres_op_store,
        suffix="revk",
        cap=Decimal("80.00"),
        spend=Decimal("80.00"),
        key_status="revoked",
    )

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert rows == []


async def test_expired_key_is_excluded(postgres_op_store):
    await _seed(
        postgres_op_store,
        suffix="expd",
        cap=Decimal("80.00"),
        spend=Decimal("80.00"),
        key_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert rows == []


async def test_null_quota_falls_back_to_the_enforcers_default(postgres_op_store):
    """A pre-migration row with no cap means the same 1000 the gate assumes."""
    await _seed(postgres_op_store, suffix="nullq", cap=Decimal("80.00"), spend=Decimal("999.00"))
    # Clear the column the way an old row would have it.
    async with postgres_op_store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET quota_daily_cost_usd = NULL WHERE user_id = $1",
            f"{_USER_PREFIX}nullq",
        )

    rows = await postgres_op_store.query_users_at_daily_quota()

    assert rows == []  # 999 + estimate is still under the 1000 default

    async with postgres_op_store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_daily_cost SET cost_usd = $2 WHERE user_id = $1",
            f"{_USER_PREFIX}nullq",
            Decimal("1000.00"),
        )

    rows = await postgres_op_store.query_users_at_daily_quota()
    assert _ids(rows) == [f"{_USER_PREFIX}nullq"]
    assert rows[0][3] == pytest.approx(quota.DEFAULT_DAILY_QUOTA_USD)


async def test_limit_truncates_to_the_largest_overages(postgres_op_store):
    await _seed(
        postgres_op_store, suffix="t1", role="free", cap=Decimal("20.00"), spend=Decimal("30.00")
    )
    await _seed(
        postgres_op_store, suffix="t2", role="free", cap=Decimal("20.00"), spend=Decimal("25.00")
    )
    await _seed(
        postgres_op_store, suffix="t3", role="free", cap=Decimal("20.00"), spend=Decimal("21.00")
    )

    rows = await postgres_op_store.query_users_at_daily_quota(limit=2)

    assert _ids(rows) == [f"{_USER_PREFIX}t1", f"{_USER_PREFIX}t2"]
