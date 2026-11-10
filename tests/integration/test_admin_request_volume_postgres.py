"""PostgreSQL integration tests for the admin Request volume rollup and list total.

``/admin/request-metrics`` used to count each card straight off ``api_logs`` with
one query per card. It now reads completed hours from ``request_volume_hourly``
and counts only the edges live (see ``admin/request_volume.py``). The unit tests
check the assembly against an in-memory stand-in; these run the real SQL and hold
the endpoint to the per-card query it replaced -- kept here verbatim as the
oracle -- over rows placed on every seam the assembly has. The oracle narrowed
to one source is the oracle for that source's counts and for the batch
requests every bar reports.

Also here: the Recent Requests total's time budget, which only Postgres can
enforce.

Connection: ``TEST_PG_DSN`` when set, otherwise the ``DB_HOST`` / ``DB_PORT`` /
``DB_NAME`` / ``DB_USER`` / ``DB_PASSWORD`` variables CI exports for its
``postgres`` service. Skipped when no database is reachable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.servers.deps import AppServices, get_admin_audit_actor, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin import metrics, request_volume
from serving.servers.routers.admin.metrics import REQUEST_METRIC_WINDOWS
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import migrations as mig, postgres
from tests.fixtures.audit import ADMIN_ACTOR

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# The per-card query /admin/request-metrics ran before the rollup, verbatim.
# $1 window minutes, $2 bucket minutes, $3 499, $4 'client_disconnect'.
_PER_CARD_SQL = """
WITH config AS (
    SELECT ($2::int * 60) AS bucket_seconds
),
bounds AS (
    SELECT
        date_trunc('minute', NOW()) AS end_time,
        date_trunc('minute', NOW())
            - ($1::int * interval '1 minute') AS start_time,
        to_timestamp(
            floor(
                extract(
                    epoch FROM date_trunc('minute', NOW())
                        - ($1::int * interval '1 minute')
                ) / config.bucket_seconds
            ) * config.bucket_seconds
        ) AS aligned_start
    FROM config
),
series AS (
    SELECT generate_series(
        (SELECT aligned_start FROM bounds),
        (SELECT end_time FROM bounds),
        $2::int * interval '1 minute'
    ) AS bucket_start
),
bucketed_logs AS (
    SELECT
        to_timestamp(
            floor(extract(epoch from timestamp) / ($2::int * 60))
            * ($2::int * 60)
        ) AS bucket_start,
        COUNT(*) AS request_count,
        COUNT(*) FILTER (
            WHERE status_code >= 200 AND status_code < 400
        ) AS success_count,
        COUNT(*) FILTER (
            WHERE (error IS NOT NULL
                OR status_code IS NULL
                OR status_code < 200
                OR status_code >= 400)
              AND (status_code = $3::int
                   AND metadata->>'terminal_state' = $4) IS NOT TRUE
        ) AS error_count,
        COUNT(*) FILTER (
            WHERE status_code = $3::int
              AND metadata->>'terminal_state' = $4
        ) AS client_disconnect_count,
        AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL)
            AS avg_latency_ms
    FROM api_logs, bounds
    WHERE timestamp >= bounds.start_time
      AND timestamp <= bounds.end_time
    GROUP BY 1
)
SELECT
    series.bucket_start,
    COALESCE(bucketed_logs.request_count, 0) AS request_count,
    COALESCE(bucketed_logs.success_count, 0) AS success_count,
    COALESCE(bucketed_logs.error_count, 0) AS error_count,
    COALESCE(bucketed_logs.client_disconnect_count, 0)
        AS client_disconnect_count,
    bucketed_logs.avg_latency_ms
FROM series
LEFT JOIN bucketed_logs
  ON bucketed_logs.bucket_start = series.bucket_start
ORDER BY series.bucket_start ASC
"""

# (status, error, metadata) for each outcome a card tells apart.
_OUTCOMES: list[tuple[int | None, str | None, dict[str, Any]]] = [
    (200, None, {}),
    (500, "boom", {}),
    (499, "Client disconnected", {"terminal_state": "client_disconnect"}),
    # An upstream's own 499 is a failure, not a disconnect.
    (499, "upstream returned 499", {}),
    (None, "no response", {}),
    # A 2xx that also recorded an error counts as a success and an error.
    (200, "partial", {}),
]


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """This worker's test database (tests/fixtures/postgres.py), reachable or skipped."""
    return postgres.worker_dsn()


@pytest_asyncio.fixture
async def pool(pg_dsn: str, request: pytest.FixtureRequest) -> AsyncGenerator[asyncpg.Pool, None]:
    """A pool scoped to a private schema holding a fresh ``api_logs``.

    Schema-per-test rather than TRUNCATE: CI runs several files against one
    database concurrently.
    """
    worker = postgres.worker_id()
    test_digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    schema = f"volume_{worker}_{test_digest}"

    admin_conn = await postgres.connect()
    try:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin_conn.close()

    pool = await postgres.create_pool(
        min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    assert pool is not None
    async with pool.acquire() as conn:
        await mig.ensure_api_logs_tables(conn)
        await conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT, user_name TEXT, login_name TEXT)"
        )
    metrics._RECENT_REQUESTS_COUNT_CACHE.clear()
    try:
        yield pool
    finally:
        metrics._RECENT_REQUESTS_COUNT_CACHE.clear()
        await pool.close()
        admin_conn = await postgres.connect()
        try:
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin_conn.close()


@pytest_asyncio.fixture
async def client(pool: asyncpg.Pool) -> AsyncGenerator[AsyncClient, None]:
    app = FastAPI(title="Admin Request Volume Postgres Test")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=FixedRouter(),
        db_logger=SimpleNamespace(pool=pool),
        log_store=PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET),
    )
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_admin_audit_actor] = lambda: ADMIN_ACTOR
    app.include_router(admin.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _seed(pool: asyncpg.Pool, stamps: list[datetime]) -> None:
    rows = []
    for index, at in enumerate(stamps):
        status, error, metadata = _OUTCOMES[index % len(_OUTCOMES)]
        latency = None if index % 7 == 0 else 100 + index % 9000
        # Every fifth row is the batch worker's: each outcome comes up among them.
        batch = f"batch_{index % 3}" if index % 5 == 0 else None
        rows.append((f"req-{index}", status, error, json.dumps(metadata), latency, at, batch))
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, status_code, error, metadata,
                latency_ms, timestamp, batch_job_id
            ) VALUES ($1, 'glm-4.6', 'zai', $2, $3, $4::jsonb, $5, $6, $7)
            """,
            rows,
        )


def _seam_stamps(now_minute: datetime) -> list[datetime]:
    """Timestamps on, and a microsecond either side of, every seam a card has."""
    tick = timedelta(microseconds=1)
    hour = timedelta(hours=1)
    settled = request_volume.settled_before(now_minute)
    seams = [now_minute, settled, settled + hour, now_minute - timedelta(minutes=1)]
    for _key, _label, window, _bucket in REQUEST_METRIC_WINDOWS:
        start = now_minute - timedelta(minutes=window)
        floor_hour = start.replace(minute=0, second=0, microsecond=0)
        seams += [start, floor_hour, floor_hour + hour]
    return [seam + nudge for seam in seams for nudge in (-tick, timedelta(0), tick)]


def _spread_stamps(now_minute: datetime, count: int) -> list[datetime]:
    """Timestamps over the month card and a day past either end of it."""
    span = timedelta(days=32)
    step = span / count
    oldest = now_minute - timedelta(days=31)
    return [oldest + step * i + timedelta(microseconds=i * 7919 % 10**6) for i in range(count)]


# The oracle's row filter, and what it gains for one source.
_PER_CARD_ROWS = "WHERE timestamp >= bounds.start_time"
_SOURCE_ROWS = {
    None: "",
    "all": "",
    "interactive": " AND batch_job_id IS NULL",
    "batch": " AND batch_job_id IS NOT NULL",
}


async def _old_cards(
    pool: asyncpg.Pool, source: str | None = None
) -> dict[str, list[tuple[Any, ...]]]:
    """The per-card query's cards, over *source*'s rows alone when one is named."""
    assert _PER_CARD_SQL.count(_PER_CARD_ROWS) == 1
    query = _PER_CARD_SQL.replace(_PER_CARD_ROWS, _PER_CARD_ROWS + _SOURCE_ROWS[source])
    cards = {}
    async with pool.acquire() as conn:
        for key, _label, window, bucket in REQUEST_METRIC_WINDOWS:
            rows = await conn.fetch(query, window, bucket, 499, "client_disconnect")
            cards[key] = [
                (
                    row["bucket_start"],
                    row["request_count"],
                    row["success_count"],
                    row["error_count"],
                    row["client_disconnect_count"],
                    (
                        round(float(row["avg_latency_ms"]), 1)
                        if row["avg_latency_ms"] is not None
                        else None
                    ),
                )
                for row in rows
            ]
    return cards


def _batch_bars(body: dict[str, Any]) -> dict[str, list[tuple[Any, ...]]]:
    """Each bar's batch requests and failures, as the oracle's bars read them."""
    return {
        window["key"]: [
            (
                datetime.fromisoformat(bar["start_time"].replace("Z", "+00:00")),
                bar["batch_request_count"],
                bar["batch_error_count"],
            )
            for bar in window["buckets"]
        ]
        for window in body["windows"]
    }


def _new_cards(body: dict[str, Any]) -> dict[str, list[tuple[Any, ...]]]:
    return {
        window["key"]: [
            (
                datetime.fromisoformat(bar["start_time"].replace("Z", "+00:00")),
                bar["request_count"],
                bar["success_count"],
                bar["error_count"],
                bar["client_disconnect_count"],
                bar["avg_latency_ms"],
            )
            for bar in window["buckets"]
        ]
        for window in body["windows"]
    }


async def _db_minute(pool: asyncpg.Pool) -> datetime:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT date_trunc('minute', NOW())")


async def _compare_with_per_card_queries(
    client: AsyncClient, pool: asyncpg.Pool, source: str | None = None
) -> None:
    """The endpoint's cards equal the per-card query's, read in the same minute.

    The counts are *source*'s; every bar's batch requests are the per-card
    query's over the batch worker's rows, whatever the source.
    """
    for _attempt in range(3):
        before = await _db_minute(pool)
        resp = await client.get(
            "/admin/request-metrics", params={"source": source} if source else {}
        )
        assert resp.status_code == 200, resp.text
        old = await _old_cards(pool, source)
        old_batch = await _old_cards(pool, "batch")
        if await _db_minute(pool) != before:
            continue  # the minute turned mid-comparison; the cards moved with it
        assert _new_cards(resp.json()) == old
        assert _batch_bars(resp.json()) == {
            key: [(start, count, errors) for start, count, _ok, errors, *_rest in bars]
            for key, bars in old_batch.items()
        }
        return
    pytest.fail("could not read both versions within one minute")


@pytest.mark.asyncio
async def test_cards_match_the_per_card_queries(client: AsyncClient, pool: asyncpg.Pool):
    now_minute = await _db_minute(pool)
    await _seed(pool, _seam_stamps(now_minute) + _spread_stamps(now_minute, 3000))

    # Cold: every completed hour is counted and stored on the way.
    await _compare_with_per_card_queries(client, pool)
    # Warm: the stored hours are read back, for each source.
    for source in (None, "interactive", "batch"):
        await _compare_with_per_card_queries(client, pool, source)


@pytest.mark.asyncio
async def test_an_hour_stored_under_another_definition_is_counted_again(
    client: AsyncClient, pool: asyncpg.Pool
):
    """The hours the previous release stored count no batch requests: counted afresh."""
    now_minute = await _db_minute(pool)
    await _seed(pool, _spread_stamps(now_minute, 800))
    assert (await client.get("/admin/request-metrics")).status_code == 200

    async with pool.acquire() as conn:
        # As the previous release would have stored it, give or take a count no
        # row could give.
        stale = await conn.fetchval(
            """
            UPDATE request_volume_hourly
            SET definition = '', batch_request_count = 0, request_count = request_count + 100000
            WHERE hour_bucket = (
                SELECT max(hour_bucket) FROM request_volume_hourly
                WHERE hour_bucket < NOW() - interval '3 days'
            )
            RETURNING hour_bucket
            """
        )
    assert stale is not None

    await _compare_with_per_card_queries(client, pool, "interactive")
    async with pool.acquire() as conn:
        definition = await conn.fetchval(
            "SELECT definition FROM request_volume_hourly WHERE hour_bucket = $1", stale
        )
    assert definition == request_volume.DEFINITION


@pytest.mark.asyncio
async def test_completed_hours_are_stored_and_read_back(client: AsyncClient, pool: asyncpg.Pool):
    now_minute = await _db_minute(pool)
    await _seed(pool, _spread_stamps(now_minute, 500))
    assert (await client.get("/admin/request-metrics")).status_code == 200

    async with pool.acquire() as conn:
        stored = await conn.fetchval("SELECT count(*) FROM request_volume_hourly")
        newest = await conn.fetchval("SELECT max(hour_bucket) FROM request_volume_hourly")
        # Stamp a stored hour three days back with a count no row could give.
        marked = await conn.fetchval(
            """
            UPDATE request_volume_hourly SET request_count = request_count + 100000
            WHERE hour_bucket = (
                SELECT max(hour_bucket) FROM request_volume_hourly
                WHERE hour_bucket < NOW() - interval '3 days'
            )
            RETURNING hour_bucket
            """
        )
    # A month of completed hours, quiet ones included, and none still live.
    assert 718 <= stored <= 721
    assert newest < request_volume.settled_before(now_minute)

    body = (await client.get("/admin/request-metrics")).json()
    month = next(w for w in body["windows"] if w["key"] == "1mo")
    day_of_marked = marked.replace(hour=0)
    bar = next(
        b
        for b in month["buckets"]
        if datetime.fromisoformat(b["start_time"].replace("Z", "+00:00")) == day_of_marked
    )
    assert bar["request_count"] >= 100000, "a stored hour must be read, not counted again"


@pytest.mark.asyncio
async def test_clear_errors_drops_the_stored_hours_it_reached(
    client: AsyncClient, pool: asyncpg.Pool
):
    now_minute = await _db_minute(pool)
    # Failures over the last five hours, the stored ones among them.
    stamps = [now_minute - timedelta(minutes=17 * i + 3) for i in range(18)]
    await _seed(pool, stamps)
    assert (await client.get("/admin/request-metrics")).status_code == 200

    resp = await client.post("/admin/recent-requests/clear-errors?hours=4")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_count"] > 0

    # What the cards show now is what is left in api_logs.
    await _compare_with_per_card_queries(client, pool)


# The rows "Clear errors" deletes, for knowing which hours it reaches.
_CLEARED_HOURS_SQL = """
SELECT DISTINCT to_timestamp(floor(extract(epoch FROM timestamp) / 3600) * 3600) AS hour
FROM api_logs
WHERE timestamp >= NOW() - make_interval(hours => $1::int)
  AND (error IS NOT NULL OR status_code IS NULL OR status_code < 200 OR status_code >= 400)
"""


async def _stored_hours(pool: asyncpg.Pool) -> set[datetime]:
    async with pool.acquire() as conn:
        return {
            r["hour_bucket"]
            for r in await conn.fetch("SELECT hour_bucket FROM request_volume_hourly")
        }


@pytest.mark.asyncio
async def test_a_failed_invalidation_rolls_the_clear_back(client: AsyncClient, pool: asyncpg.Pool):
    """The delete and the invalidation commit together or not at all.

    Were they separate, the delete could commit and the invalidation fail: the
    hour would stay stored with the deleted rows counted in it, a stored hour is
    never counted again, and a second clear would delete nothing and so
    invalidate nothing.
    """
    now_minute = await _db_minute(pool)
    stamps = [now_minute - timedelta(minutes=17 * i + 3) for i in range(18)]
    await _seed(pool, stamps)
    assert (await client.get("/admin/request-metrics")).status_code == 200
    stored = await _stored_hours(pool)
    async with pool.acquire() as conn:
        cleared = {r["hour"] for r in await conn.fetch(_CLEARED_HOURS_SQL, 4)}
    assert stored & cleared, "the clear must reach a stored hour"
    assert stored - cleared, "and leave one alone"

    store = PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)

    async def fail(conn: asyncpg.Connection, hours: list[datetime]) -> None:
        raise asyncpg.QueryCanceledError("canceling statement due to statement timeout")

    with pytest.raises(asyncpg.QueryCanceledError):
        await store.delete_recent_error_requests(hours=4, on_deleted=fail)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_logs") == len(stamps)
    assert await _stored_hours(pool) == stored
    await _compare_with_per_card_queries(client, pool)

    # Retried, the clear drops exactly the stored hours its rows were in.
    deleted = await store.delete_recent_error_requests(
        hours=4, on_deleted=request_volume.invalidate_hours
    )
    assert deleted > 0
    assert await _stored_hours(pool) == stored - cleared
    await _compare_with_per_card_queries(client, pool)


@pytest.mark.asyncio
async def test_backfill_stores_every_completed_hour_once(pool: asyncpg.Pool):
    now_minute = await _db_minute(pool)
    await _seed(pool, _spread_stamps(now_minute, 300))

    written = await request_volume.backfill_missing(pool, days=2)
    assert written == 48
    assert await request_volume.backfill_missing(pool, days=2) == 0

    async with pool.acquire() as conn:
        mismatched = await conn.fetchval(
            """
            SELECT count(*) FROM request_volume_hourly v
            WHERE v.request_count <> (
                SELECT count(*) FROM api_logs l
                WHERE l.timestamp >= v.hour_bucket
                  AND l.timestamp < v.hour_bucket + interval '1 hour'
            )
            OR v.batch_request_count <> (
                SELECT count(*) FROM api_logs l
                WHERE l.timestamp >= v.hour_bucket
                  AND l.timestamp < v.hour_bucket + interval '1 hour'
                  AND l.batch_job_id IS NOT NULL
            )
            OR v.definition <> $1
            """,
            request_volume.DEFINITION,
        )
        batch_rows = await conn.fetchval(
            "SELECT sum(batch_request_count) FROM request_volume_hourly"
        )
    assert mismatched == 0
    assert batch_rows > 0


@pytest.mark.asyncio
async def test_a_bar_lists_the_requests_it_counted(client: AsyncClient, pool: asyncpg.Pool):
    """A day bar of the month card, older than the list's week, lists its own rows."""
    now_minute = await _db_minute(pool)
    await _seed(pool, _spread_stamps(now_minute, 1200))
    body = (await client.get("/admin/request-metrics?source=interactive")).json()
    month = next(w for w in body["windows"] if w["key"] == "1mo")
    bar = next(b for b in month["buckets"][1:-1] if b["request_count"] > 0)
    start = datetime.fromisoformat(bar["start_time"].replace("Z", "+00:00"))
    assert start < now_minute - timedelta(days=8), "the bar must lie past the default lookback"

    resp = await client.get(
        "/admin/recent-requests",
        params={
            "source": "interactive",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=month["bucket_minutes"])).isoformat(),
            "limit": 200,
        },
    )

    assert resp.status_code == 200, resp.text
    listed = resp.json()
    assert listed["total"] == bar["request_count"]
    assert listed["total_exact"] is True
    assert all(
        start
        <= datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        < start + timedelta(days=1)
        for row in listed["requests"]
    )
    assert all(row["batch_job_id"] is None for row in listed["requests"])


@pytest.mark.asyncio
async def test_a_count_past_its_budget_is_cancelled_by_postgres(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(metrics, "_RECENT_REQUESTS_COUNT_BUDGET_MS", 50)
    slow = "SELECT COUNT(*) AS total FROM (SELECT pg_sleep(1)) AS s"
    assert await metrics._count_recent_requests(pool, slow, []) is None

    fast = "SELECT COUNT(*) AS total FROM generate_series(1, $1::int)"
    assert await metrics._count_recent_requests(pool, fast, [7]) == 7

    # SET LOCAL left nothing behind on the pooled connections.
    async with pool.acquire() as conn:
        assert await conn.fetchval("SHOW statement_timeout") == "0"


@pytest.mark.asyncio
async def test_list_total_is_a_lower_bound_past_the_budget(
    client: AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
):
    now_minute = await _db_minute(pool)
    await _seed(pool, [now_minute - timedelta(minutes=i) for i in range(1, 8)])

    exact = (await client.get("/admin/recent-requests?limit=5&days=1")).json()
    assert (exact["total"], exact["total_exact"], exact["has_more"]) == (7, True, True)

    # A budget no count can meet: the page still arrives, with what it knows.
    monkeypatch.setattr(metrics, "_RECENT_REQUESTS_COUNT_BUDGET_MS", 1)
    monkeypatch.setattr(
        metrics,
        "_count_recent_requests",
        _always_slow(metrics._count_recent_requests),
    )
    body = (await client.get("/admin/recent-requests?limit=5&days=1&refresh=true")).json()
    assert body["total_exact"] is False
    assert body["total"] == 5
    assert body["has_more"] is True
    assert len(body["requests"]) == 5


def _always_slow(count):
    """Run the real budgeted count over a statement that cannot finish in time."""

    async def _slow(pool: Any, _count_sql: str, _params: list[Any]) -> int | None:
        return await count(pool, "SELECT COUNT(*) AS total FROM (SELECT pg_sleep(1)) s", [])

    return _slow
