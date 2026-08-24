"""PostgreSQL integration tests for the admin per-endpoint performance summary.

The rest of the coverage for ``/admin/recent-requests/performance`` asserts the
SQL *text* against a mocked connection. That catches a deleted predicate but not
a wrong one: changing the decode-throughput denominator, computing the TTFT
percentiles over the throughput column, or undercounting ``request_count`` all
leave those assertions passing. These tests execute the real query against
Postgres and assert the numbers, cross-checked against the row-level helper that
produces the table's own Decode column — the reconciliation the endpoint's
docstring promises.

Connection: ``TEST_PG_DSN`` when set, otherwise the ``DB_HOST`` / ``DB_PORT`` /
``DB_NAME`` / ``DB_USER`` / ``DB_PASSWORD`` variables CI exports for its
``postgres`` service (the sibling ``*_postgres.py`` files read only
``TEST_PG_DSN``, so they skip in CI). Skipped when no database is reachable.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin.metrics import (
    _PERF_BREAKDOWN_CACHE,
    _decode_throughput_tps,
    _load_request_perf_breakdown,
)
from serving.storage.log_schema import ensure_api_logs_schema
from tests.fixtures.auth_helpers import assert_test_db_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


def _dsn() -> str:
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
    """A reachable test-database DSN, or skip."""
    import asyncio

    dsn = _dsn()

    async def _probe() -> str | None:
        try:
            conn = await asyncpg.connect(dsn, timeout=2)
        except Exception as exc:
            return str(exc)
        try:
            assert_test_db_name(await conn.fetchval("SELECT current_database()"), "perf breakdown")
        finally:
            await conn.close()
        return None

    loop = asyncio.new_event_loop()
    try:
        failure = loop.run_until_complete(_probe())
    finally:
        loop.close()
    if failure is not None:
        pytest.skip(f"PostgreSQL test database not available: {failure}")
    return dsn


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str, request: pytest.FixtureRequest) -> AsyncGenerator[Any, None]:
    """A pool scoped to a private schema holding a fresh ``api_logs``.

    Schema-per-test rather than TRUNCATE: CI runs several test files against one
    database concurrently, so truncating the shared ``public.api_logs`` would
    delete rows out from under whatever else is running. ``search_path`` is set
    on every pooled connection, so the unqualified names in the schema builder
    and in the endpoint's SQL resolve here.
    """
    # Named from a stable digest of the test id, not hash(): str hashing is
    # salted per process, so hash() would give the same test a different schema
    # every run and a schema left behind by a crash could not be traced back to
    # the test that made it.
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    test_digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    schema = f"perf_breakdown_{worker}_{test_digest}"

    admin_conn = await asyncpg.connect(pg_dsn)
    try:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin_conn.close()

    pool = await asyncpg.create_pool(
        pg_dsn, min_size=1, max_size=3, server_settings={"search_path": schema}
    )
    assert pool is not None
    async with pool.acquire() as conn:
        await ensure_api_logs_schema(conn)
        # The list endpoint LEFT JOINs users unconditionally; only the columns it
        # reads are needed here.
        await conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                user_name TEXT
            )
            """
        )
        await conn.execute(
            "INSERT INTO users (id, email, user_name) VALUES "
            "('user-1', 'ada@example.com', 'Ada'), ('user-2', 'bob@example.com', 'Bob')"
        )

    _PERF_BREAKDOWN_CACHE.clear()
    try:
        yield SimpleNamespace(pool=pool)
    finally:
        _PERF_BREAKDOWN_CACHE.clear()
        await pool.close()
        admin_conn = await asyncpg.connect(pg_dsn)
        try:
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin_conn.close()


# One seeded request. ttft/latency/completion are chosen so the expected
# throughput is computable by hand as well as by the helper.
def _row(
    request_id: str,
    *,
    model: str = "glm-4.6",
    provider: str = "zai",
    served_model: str | None = "glm-4.6",
    served_endpoint: str | None = "glm-4.6:local-12003",
    stream: bool = True,
    status: int = 200,
    ttft: int | None = 500,
    latency: int | None = 4500,
    completion: int | None = 41,
    user_id: str = "user-1",
    request_type: str | None = None,
    age_days: int = 1,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "model_id": model,
        "provider": provider,
        "served_model_id": served_model,
        "served_endpoint_id": served_endpoint,
        "stream": stream,
        "status_code": status,
        "ttft_ms": ttft,
        "latency_ms": latency,
        "completion_tokens": completion,
        "user_id": user_id,
        "request_type": request_type,
        "age_days": age_days,
    }


async def _seed(pool: asyncpg.Pool, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, served_model_id, served_endpoint_id,
                stream, status_code, ttft_ms, latency_ms, prompt_tokens,
                completion_tokens, user_id, metadata, timestamp
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,100,$10,$11,$12::jsonb,$13)
            """,
            [
                (
                    r["request_id"],
                    r["model_id"],
                    r["provider"],
                    r["served_model_id"],
                    r["served_endpoint_id"],
                    r["stream"],
                    r["status_code"],
                    r["ttft_ms"],
                    r["latency_ms"],
                    r["completion_tokens"],
                    r["user_id"],
                    '{{"request_type": "{}"}}'.format(r["request_type"])
                    if r["request_type"]
                    else "{}",
                    now - timedelta(days=r["age_days"]),
                )
                for r in rows
            ],
        )


def _percentile_cont(values: list[float], q: float) -> float:
    """Postgres ``percentile_cont``: linear interpolation at ``(n - 1) * q``."""
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _expected(rows: list[dict[str, Any]], key: tuple[str, str]) -> dict[str, Any]:
    """Compute one group's expected summary from the seeded rows, in Python.

    Deliberately independent of the SQL: TTFT comes from the seeded column and
    throughput from ``_decode_throughput_tps`` — the same helper that fills the
    Recent Requests table's Decode column — so a divergence between the two is a
    test failure rather than a silently different dashboard.
    """
    model, endpoint = key
    group = [
        r
        for r in rows
        if (r["served_model_id"] or r["model_id"]) == model
        and (r["served_endpoint_id"] or r["provider"]) == endpoint
        and r["stream"] is True
        and r["status_code"] is not None
        and 200 <= r["status_code"] <= 399
        and r["age_days"] <= 7
    ]
    ttfts = [float(r["ttft_ms"]) for r in group if r["ttft_ms"] and r["ttft_ms"] > 0]
    throughputs = [
        tps
        for tps in (
            _decode_throughput_tps(
                r["stream"], r["latency_ms"], r["ttft_ms"], r["completion_tokens"]
            )
            for r in group
        )
        if tps is not None
    ]

    def summary(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"count": 0, "mean": None, "p10": None, "p50": None, "p90": None}
        return {
            "count": len(values),
            "mean": round(sum(values) / len(values), 2),
            "p10": round(_percentile_cont(values, 0.1), 2),
            "p50": round(_percentile_cont(values, 0.5), 2),
            "p90": round(_percentile_cont(values, 0.9), 2),
        }

    return {
        "request_count": len(group),
        "ttft_ms": summary(ttfts),
        "decode_throughput_tps": summary(throughputs),
    }


def _mixed_rows() -> list[dict[str, Any]]:
    """A local endpoint, a remote endpoint, a legacy row, and rows that must not count."""
    rows = [
        # Local endpoint: TTFT 100..500 ms, 41 tokens over a 4 s decode window.
        *[
            _row(f"local-{i}", ttft=ttft, latency=ttft + 4000, completion=41)
            for i, ttft in enumerate([100, 200, 300, 400, 500])
        ],
        # Remote endpoint: slower first token, 21 tokens over the same window.
        *[
            _row(
                f"remote-{i}",
                served_endpoint="glm-4.6:zai-api",
                ttft=ttft,
                latency=ttft + 4000,
                completion=21,
            )
            for i, ttft in enumerate([1000, 2000, 3000])
        ],
        # Counted for TTFT, but the decode window / token count is below the
        # floor the row-level helper enforces, so throughput must stay undefined.
        _row("short-window", ttft=700, latency=1200, completion=50),
        _row("few-tokens", ttft=800, latency=6000, completion=4),
        # Streamed and successful, but no first-token timestamp was recorded, so
        # the route's traffic count and its TTFT sample count must diverge.
        _row("no-ttft", ttft=None, latency=4000),
        _row("zero-ttft", ttft=0, latency=4000),
        # Must not appear at all.
        _row("not-streamed", stream=False, ttft=9000),
        _row("failed", status=500, ttft=9000),
        _row("out-of-window", ttft=9000, age_days=40),
        # Logged before served_* existed: groups under model_id / provider.
        _row(
            "legacy",
            model="legacy-model",
            provider="openai",
            served_model=None,
            served_endpoint=None,
            ttft=250,
            latency=4250,
        ),
    ]
    return rows


@pytest.mark.asyncio
async def test_group_metrics_match_row_level_computation(db_logger):
    """Every number the panel shows equals the same statistic computed in Python."""
    rows = _mixed_rows()
    await _seed(db_logger.pool, rows)

    response = await _load_request_perf_breakdown(
        db_logger, days=7, user_id=None, model_id=None, request_type=None
    )
    by_key = {(g.model_id, g.endpoint_id): g for g in response.groups}

    assert set(by_key) == {
        ("glm-4.6", "glm-4.6:local-12003"),
        ("glm-4.6", "glm-4.6:zai-api"),
        ("legacy-model", "openai"),
    }

    for key, group in by_key.items():
        expected = _expected(rows, key)
        assert group.request_count == expected["request_count"], key
        for metric in ("ttft_ms", "decode_throughput_tps"):
            actual = getattr(group, metric)
            assert {
                "count": actual.count,
                "mean": actual.mean,
                "p10": actual.p10,
                "p50": actual.p50,
                "p90": actual.p90,
            } == expected[metric], (key, metric)

    # Spot-check the hand-computable values, so a bug in _expected cannot make
    # this test vacuous: 41 tokens over 4 s is 10 tok/s, 21 tokens is 5 tok/s.
    local = by_key[("glm-4.6", "glm-4.6:local-12003")]
    assert local.decode_throughput_tps.p50 == 10.0
    assert local.ttft_ms.p50 == 400.0
    assert by_key[("glm-4.6", "glm-4.6:zai-api")].decode_throughput_tps.p50 == 5.0


@pytest.mark.asyncio
async def test_unmeasurable_rows_count_as_traffic_but_not_as_samples(db_logger):
    """A short decode window still counts as a request; it is not a throughput sample."""
    await _seed(db_logger.pool, _mixed_rows())

    response = await _load_request_perf_breakdown(
        db_logger, days=7, user_id=None, model_id=None, request_type=None
    )
    local = next(g for g in response.groups if g.endpoint_id == "glm-4.6:local-12003")

    # 5 clean rows + short-window + few-tokens + no-ttft + zero-ttft; the
    # non-streamed, failed and out-of-window rows are gone. The three counts are
    # deliberately all different: traffic is not the same as a measured first
    # token, which is not the same as a measurable decode window.
    assert local.request_count == 9
    assert local.ttft_ms.count == 7
    assert local.decode_throughput_tps.count == 5


@pytest.mark.asyncio
async def test_group_cap_reports_truncation(db_logger, monkeypatch):
    """The cap keeps the busiest routes and says the tail was dropped."""
    from serving.servers.routers.admin import metrics as admin_metrics

    monkeypatch.setattr(admin_metrics, "_PERF_BREAKDOWN_MAX_GROUPS", 2)
    rows = [
        *[_row(f"busy-{i}", served_endpoint="ep-busy") for i in range(4)],
        *[_row(f"mid-{i}", served_endpoint="ep-mid") for i in range(2)],
        _row("quiet-0", served_endpoint="ep-quiet"),
    ]
    await _seed(db_logger.pool, rows)

    response = await _load_request_perf_breakdown(
        db_logger, days=7, user_id=None, model_id=None, request_type=None
    )

    assert response.truncated is True
    assert [g.endpoint_id for g in response.groups] == ["ep-busy", "ep-mid"]
    assert [g.request_count for g in response.groups] == [4, 2]


@pytest.mark.asyncio
async def test_summary_reconciles_with_the_list_views_decode_column(db_logger):
    """The panel's numbers and the table's per-row Decode column agree.

    Both surfaces are driven through HTTP here, so the handler's clamping, the
    per-filter cache and the response models are all in the path.
    """
    rows = _mixed_rows()
    await _seed(db_logger.pool, rows)

    app = FastAPI()
    app.state.services = AppServices(router=RouteExecutor(), db_logger=db_logger)
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        summary = await client.get("/admin/recent-requests/performance?days=7")
        listing = await client.get("/admin/recent-requests?days=7&limit=200")
    assert summary.status_code == 200, summary.text
    assert listing.status_code == 200, listing.text

    # Group the list view's own Decode values by served route and compare the
    # mean with the summary's.
    per_route: dict[str, list[float]] = {}
    for item in listing.json()["requests"]:
        row = next(r for r in rows if r["request_id"] == item["request_id"])
        if item["decode_throughput_tps"] is None:
            continue
        endpoint = row["served_endpoint_id"] or row["provider"]
        per_route.setdefault(endpoint, []).append(item["decode_throughput_tps"])

    assert per_route, "the list view reported no measurable decode throughput"
    groups = {g["endpoint_id"]: g for g in summary.json()["groups"]}
    for endpoint, values in per_route.items():
        group = groups[endpoint]
        assert group["decode_throughput_tps"]["count"] == len(values), endpoint
        assert group["decode_throughput_tps"]["mean"] == round(sum(values) / len(values), 2), (
            endpoint
        )


@pytest.mark.asyncio
async def test_filters_narrow_the_same_rows_as_the_list_view(db_logger):
    """The shared filter builder is applied to the aggregate, not just the list."""
    await _seed(db_logger.pool, [*_mixed_rows(), _row("other-user", user_id="user-2")])

    only_bob = await _load_request_perf_breakdown(
        db_logger, days=7, user_id="bob@example", model_id=None, request_type=None
    )
    assert [g.request_count for g in only_bob.groups] == [1]

    only_legacy = await _load_request_perf_breakdown(
        db_logger, days=7, user_id=None, model_id="LEGACY", request_type=None
    )
    assert {g.model_id for g in only_legacy.groups} == {"legacy-model"}

    wide = await _load_request_perf_breakdown(
        db_logger, days=90, user_id=None, model_id=None, request_type=None
    )
    local_wide = next(g for g in wide.groups if g.endpoint_id == "glm-4.6:local-12003")
    # The 40-day-old row is in range now.
    assert local_wide.request_count == 11
