"""Three-state cache accounting in provider observability, against real Postgres.

A provider that reports no cache usage must not be scored as though it missed
every lookup. These drive the real ``/admin/api/provider-observability`` route
against real ``api_logs`` rows so the SQL predicates are exercised, not a canned
fake pool.

States (identical positive prompt tokens): NULL = unknown, 0 = measured miss,
900 = measured hit.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, get_db_logger, verify_admin_access
from serving.servers.routers import admin as admin_router
from serving.storage.database import DatabaseLogger
from tests.fixtures.auth_helpers import assert_test_db_name

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_PROVIDER = "sglang"
_MODEL = "deepseek-v4.1-flash"
_BASE = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class _Pool:
    """Minimal db_logger stand-in: the route only reads ``.pool``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool


def _dsn() -> str:
    cache_test_port = os.getenv("CACHE_TELEMETRY_TEST_DB_PORT")
    if cache_test_port:
        host = os.getenv("DB_HOST", "localhost")
        name = os.getenv("CACHE_TELEMETRY_TEST_DB_NAME", "hybridinference_test_db")
        user = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "postgres")
        return f"postgresql://{user}:{password}@{host}:{cache_test_port}/{name}"

    # Prefer an explicit DB_* connection over TEST_PG_DSN when both are set.
    # CI uses DB_PORT for the dynamically mapped Postgres service; a runner's
    # TEST_PG_DSN can point at a different port or database.
    if os.getenv("DB_HOST") or os.getenv("DB_PORT"):
        host = os.getenv("DB_HOST", "localhost")
        port = os.getenv("DB_PORT", "5432")
        name = os.getenv("DB_NAME", "hybridinference_test_db")
        user = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "postgres")
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"

    dsn = os.getenv("TEST_PG_DSN")
    if dsn:
        return dsn
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "hybridinference_test_db")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """A reachable dedicated test database, or skip when Postgres is absent."""
    import asyncio

    dsn = _dsn()

    async def _probe() -> str | None:
        try:
            conn = await asyncpg.connect(dsn, timeout=2)
        except Exception as exc:
            return str(exc)
        try:
            assert_test_db_name(
                await conn.fetchval("SELECT current_database()"), "cache three-state"
            )
        finally:
            await conn.close()
        return None

    loop = asyncio.new_event_loop()
    try:
        failure = loop.run_until_complete(_probe())
    finally:
        loop.close()
    if failure is not None:
        message = f"PostgreSQL test database not available: {failure}"
        if os.getenv("CI"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    return dsn


@pytest_asyncio.fixture
async def pool(pg_dsn: str):
    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    # Refuse a non-test database before touching anything: the cleanup below
    # deletes rows, and a stray staging/production DSN must not be destroyed.
    async with logger.pool.acquire() as conn:
        assert_test_db_name(await conn.fetchval("SELECT current_database()"), "cache three-state")
    # Unique per fixture instance so cleanup deletes only this file's rows and
    # never another database-backed test's rows under concurrent runs.
    prefix = f"cache3s-{uuid.uuid4().hex[:12]}"
    try:
        yield logger.pool, prefix
    finally:
        await logger.pool.execute("DELETE FROM api_logs WHERE request_id LIKE $1", f"{prefix}%")
        await logger.cleanup()


async def _seed(pool: asyncpg.Pool, prefix: str, cache_read_tokens: list[int | None]) -> None:
    """Insert one successful api_logs row per entry, all with 1000 prompt tokens."""
    rows = [
        (f"{prefix}-{i}", _MODEL, _PROVIDER, _BASE + timedelta(minutes=i), 1000, cached)
        for i, cached in enumerate(cache_read_tokens)
    ]
    await pool.executemany(
        "INSERT INTO api_logs (request_id, model_id, provider, timestamp,"
        " prompt_tokens, cache_read_tokens, status_code)"
        " VALUES ($1, $2, $3, $4, $5, $6, 200)",
        rows,
    )


async def _totals(pool: asyncpg.Pool) -> dict:
    app = FastAPI()
    app.state.services = AppServices(router=None, db_logger=_Pool(pool))  # type: ignore[arg-type]
    app.include_router(admin_router.router)
    app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
    app.dependency_overrides[get_db_logger] = lambda: _Pool(pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={
                "provider": _PROVIDER,
                "model_id": _MODEL,
                "from": (_BASE - timedelta(hours=1)).isoformat(),
                "to": (_BASE + timedelta(hours=1)).isoformat(),
            },
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["totals"]


@pytest.mark.asyncio
async def test_null_zero_and_hit_are_three_states(pool):
    """Only reported values are eligible; NULL is neither a hit nor a miss."""
    pool, prefix = pool
    await _seed(pool, prefix, [None, 0, 900])
    totals = await _totals(pool)

    assert totals["request_count"] == 3
    assert totals["cache_eligible_count"] == 2  # 0 and 900, not the NULL
    assert totals["cache_hit_count"] == 1


@pytest.mark.asyncio
async def test_all_unknown_is_not_a_zero_percent_rate(pool):
    """An all-unknown provider has no measurable rate -- not 0%."""
    pool, prefix = pool
    await _seed(pool, prefix, [None, None, None, None])
    totals = await _totals(pool)

    assert totals["request_count"] == 4
    assert totals["cache_eligible_count"] == 0
    assert totals["cache_hit_count"] == 0


@pytest.mark.asyncio
async def test_reported_zero_stays_eligible(pool):
    """A measured miss must stay in the denominator, or it vanishes from the rate."""
    pool, prefix = pool
    await _seed(pool, prefix, [0, 0, 0])
    totals = await _totals(pool)

    assert totals["cache_eligible_count"] == 3
    assert totals["cache_hit_count"] == 0
