"""PostgreSQL integration tests for the Providers tab's slow views.

``/admin/performance-metrics`` reads its rows through a partial covering index
built concurrently in the background (``perf_metrics_index``), and both it and
``/admin/ttft-scatter`` keep their last result in ``admin_view_results`` so a
restarted gateway serves it at once. These run the real DDL and SQL: the build
in each state the index can be in, the planner choosing the index for the
statements' own filter, identical numbers with and without it, the stored
results, the table's autovacuum parameter, and a hard delete dropping what was
computed over the deleted rows.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from serving.servers.routers.admin import metrics
from serving.storage import admin_view_results, perf_metrics_index
from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

if TYPE_CHECKING:
    from serving.schemas_admin import AdminPerformanceMetricsResponse

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_FENCE_SECRET = "test-fence-secret-not-for-production"


@pytest_asyncio.fixture
async def db():
    schema = "perf_views_" + uuid.uuid4().hex
    admin = await postgres.connect()
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            await ensure_api_logs_schema(conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.fixture(autouse=True)
def _reset_caches():
    metrics.PERFORMANCE_METRICS.clear()
    metrics.TTFT_SCATTER.clear()
    yield
    metrics.PERFORMANCE_METRICS.clear()
    metrics.TTFT_SCATTER.clear()


async def _log(
    conn,
    *,
    ago: timedelta,
    user_id: str = "alice",
    status: int = 200,
    stream: bool = True,
    prompt: int | None = 100,
    completion: int | None = 20,
    ttft: int | None = 300,
    latency: int | None = 2000,
    request_type: str | None = None,
    cache_read: int | None = 0,
) -> None:
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp, stream, "
        "prompt_tokens, completion_tokens, ttft_ms, latency_ms, status_code, metadata, "
        "cache_read_tokens) "
        "VALUES ($1, 'm', 'p', $2, NOW() - $3::interval, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)",
        uuid.uuid4().hex,
        user_id,
        ago,
        stream,
        prompt,
        completion,
        ttft,
        latency,
        status,
        json.dumps({"request_type": request_type}) if request_type else None,
        cache_read,
    )


async def _seed(pool) -> None:
    """Rows in every window and on every edge of the row filter."""
    async with pool.acquire() as conn:
        for i in range(60):
            await _log(
                conn,
                ago=timedelta(minutes=7 * i * i),  # 0 min .. ~17 days back
                prompt=10 + 37 * i,
                completion=(i % 9) or None,
                ttft=None if i % 5 == 0 else 40 * i,
                latency=500 + 90 * i,
                stream=i % 4 != 0,
            )
        # Outside the filter: errors, embeddings, and a month-old row.
        await _log(conn, ago=timedelta(minutes=5), status=500, prompt=99999)
        await _log(conn, ago=timedelta(minutes=5), status=429, prompt=99999)
        await _log(conn, ago=timedelta(minutes=5), request_type="embedding", prompt=99999)
        await _log(conn, ago=timedelta(days=31), prompt=99999)
        # Inside it: other request types, and a 3xx.
        await _log(conn, ago=timedelta(minutes=5), request_type="chat", prompt=7)
        await _log(conn, ago=timedelta(minutes=5), status=304, prompt=8)


async def _index_state(pool) -> bool | None:
    async with pool.acquire() as conn:
        return await conn.fetchval(perf_metrics_index._STATE_SQL, perf_metrics_index.INDEX_NAME)


def _numbers(response: AdminPerformanceMetricsResponse) -> list[dict]:
    return [w.model_dump() for w in response.windows]


# ---------------------------------------------------------------------------
# The covering index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_index_is_built_once_and_valid(db):
    assert await _index_state(db) is None
    assert await perf_metrics_index.ensure_index(db) is True
    assert await _index_state(db) is True
    # A second call finds it valid and builds nothing.
    assert await perf_metrics_index.ensure_index(db) is True


@pytest.mark.asyncio
async def test_an_invalid_index_left_by_an_interrupted_build_is_rebuilt(db):
    """A failed CREATE INDEX CONCURRENTLY leaves an invalid index by that name."""
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1))
        await _log(conn, ago=timedelta(minutes=2))
        with pytest.raises(Exception, match="could not create unique index"):
            await conn.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY {perf_metrics_index.INDEX_NAME} "
                "ON api_logs (model_id)"
            )
    assert await _index_state(db) is False

    assert await perf_metrics_index.ensure_index(db) is True
    async with db.acquire() as conn:
        definition = await conn.fetchval(
            "SELECT pg_get_indexdef(to_regclass($1))", perf_metrics_index.INDEX_NAME
        )
    assert "INCLUDE (prompt_tokens, completion_tokens, ttft_ms, latency_ms, stream)" in definition


@pytest.mark.asyncio
async def test_a_build_already_running_elsewhere_is_left_to_it(db):
    async with db.acquire() as holder:
        await holder.execute(
            f"SELECT pg_advisory_lock({perf_metrics_index._LOCK_KEY_SQL})",
            perf_metrics_index.ADVISORY_LOCK_CLASS,
        )
        assert await perf_metrics_index.ensure_index(db) is False
        assert await _index_state(db) is None
        await holder.execute(
            f"SELECT pg_advisory_unlock({perf_metrics_index._LOCK_KEY_SQL})",
            perf_metrics_index.ADVISORY_LOCK_CLASS,
        )
    assert await perf_metrics_index.ensure_index(db) is True


@pytest.mark.asyncio
async def test_log_writes_go_through_while_the_index_builds(db):
    """The build must not hold the SHARE lock a plain CREATE INDEX would."""
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1))
    async with db.acquire() as reader:
        # An open transaction makes the build wait before finishing.
        tx = reader.transaction()
        await tx.start()
        await reader.fetchval("SELECT count(*) FROM api_logs")
        building = asyncio.create_task(perf_metrics_index.ensure_index(db))
        await asyncio.sleep(0.5)
        assert not building.done()
        async with db.acquire() as writer:
            await asyncio.wait_for(_log(writer, ago=timedelta(seconds=1)), timeout=5)
        await tx.rollback()
    assert await asyncio.wait_for(building, timeout=30) is True


@pytest.mark.asyncio
async def test_the_statements_filter_lets_the_planner_use_the_index(db):
    """Their row filter and the index predicate must stay the same text."""
    await perf_metrics_index.ensure_index(db)
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        await conn.execute("SET LOCAL enable_bitmapscan = off")
        plan = "\n".join(
            row[0]
            for row in await conn.fetch(
                "EXPLAIN " + metrics._PERF_METRICS_CTE + "SELECT count(*) FROM derived", 43200
            )
        )
    assert f"Index Only Scan using {perf_metrics_index.INDEX_NAME}" in plan, plan


@pytest.mark.asyncio
async def test_the_numbers_are_the_same_with_and_without_the_index(db):
    await _seed(db)
    db_logger = MagicMock(pool=db)
    without = await metrics._load_performance_metrics(db_logger)

    await perf_metrics_index.ensure_index(db)
    async with db.acquire() as conn:
        await conn.execute("VACUUM api_logs")
    with_index = await metrics._load_performance_metrics(db_logger)

    assert _numbers(with_index) == _numbers(without)
    # The seed reaches every window, and none of the filtered-out rows counts.
    month = next(w for w in without.windows if w.key == "1mo")
    assert month.prompt_tokens.count == 62
    assert month.prompt_tokens.max < 99999


# ---------------------------------------------------------------------------
# The autovacuum parameter
# ---------------------------------------------------------------------------


async def _reloptions(pool) -> list[str]:
    async with pool.acquire() as conn:
        return list(
            await conn.fetchval("SELECT reloptions FROM pg_class WHERE oid = 'api_logs'::regclass")
            or []
        )


@pytest.mark.asyncio
async def test_api_logs_is_vacuumed_after_far_fewer_inserts(db):
    assert await _reloptions(db) == ["autovacuum_vacuum_insert_scale_factor=0.01"]


@pytest.mark.asyncio
async def test_a_value_an_operator_set_is_left_alone(db):
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE api_logs SET (autovacuum_vacuum_insert_scale_factor = 0.05)"
        )
        await ensure_api_logs_schema(conn)
    assert await _reloptions(db) == ["autovacuum_vacuum_insert_scale_factor=0.05"]


# ---------------------------------------------------------------------------
# Stored results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_result_is_read_back_only_under_its_definition(db):
    at = datetime.now(timezone.utc)
    assert await admin_view_results.write(db, "v", "def-1", {"n": 1}, at, 0)
    assert await admin_view_results.read(db, "v", "def-1") == ({"n": 1}, 0)
    assert await admin_view_results.read(db, "v", "def-2") is None
    assert await admin_view_results.read(db, "other", "def-1") is None


@pytest.mark.asyncio
async def test_an_older_result_never_replaces_a_newer_one(db):
    at = datetime.now(timezone.utc)
    await admin_view_results.write(db, "v", "def-1", {"n": 2}, at, 0)
    await admin_view_results.write(db, "v", "def-1", {"n": 1}, at - timedelta(minutes=1), 0)
    assert await admin_view_results.read(db, "v", "def-1") == ({"n": 2}, 0)
    # A new definition replaces it whatever its time: the old one is not served.
    await admin_view_results.write(db, "v", "def-2", {"n": 3}, at - timedelta(minutes=1), 0)
    assert await admin_view_results.read(db, "v", "def-2") == ({"n": 3}, 0)


@pytest.mark.asyncio
async def test_a_result_computed_before_a_delete_is_not_stored_after_it(db):
    log_store = PostgresLogStore(db, fence_secret=_FENCE_SECRET)
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1))
    before = await admin_view_results.generation(db)

    await log_store.hard_delete_user_data("alice")

    assert await admin_view_results.generation(db) == before + 1
    stored = await admin_view_results.write(
        db, "v", "def-1", {"n": 1}, datetime.now(timezone.utc), before
    )
    assert stored is False
    assert await admin_view_results.read(db, "v", "def-1") is None


@pytest.mark.asyncio
async def test_a_delete_waits_for_a_write_in_progress_and_then_drops_it(db):
    """The generation's row lock orders a write and a delete either way round."""
    log_store = PostgresLogStore(db, fence_secret=_FENCE_SECRET)
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1))
    async with db.acquire() as writer:
        tx = writer.transaction()
        await tx.start()
        # What write() holds while it stores: a share lock on the generation.
        await writer.fetchval("SELECT generation FROM admin_view_state FOR SHARE")
        await writer.execute(
            "INSERT INTO admin_view_results (name, definition, payload, computed_at, generation) "
            "VALUES ('v', 'def-1', '{}', NOW(), 0)"
        )
        deleting = asyncio.create_task(log_store.hard_delete_user_data("alice"))
        await asyncio.sleep(0.3)
        assert not deleting.done()
        await tx.commit()
    await asyncio.wait_for(deleting, timeout=10)
    assert await admin_view_results.read(db, "v", "def-1") is None


@pytest.mark.asyncio
async def test_a_scatter_that_read_a_deleted_users_rows_is_never_published(db, monkeypatch):
    """Codex's reproduction: the scatter has read the rows when the delete commits."""
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1), user_id="alice", prompt=4242)
        await _log(conn, ago=timedelta(minutes=2), user_id="bob", prompt=17)
    real_load = metrics._load_ttft_scatter
    read_rows, resume = asyncio.Event(), asyncio.Event()
    calls = 0

    async def _paused_load(db_logger):
        nonlocal calls
        calls += 1
        result = await real_load(db_logger)
        if calls == 1:
            read_rows.set()
            await resume.wait()
        return result

    monkeypatch.setattr(metrics, "_load_ttft_scatter", _paused_load)
    db_logger = MagicMock(pool=db)
    first = asyncio.create_task(metrics.admin_get_ttft_scatter(db_logger=db_logger))
    await read_rows.wait()

    await PostgresLogStore(db, fence_secret=_FENCE_SECRET).hard_delete_user_data("alice")
    resume.set()
    served = await first

    def _prompts(scatter):
        return {p.prompt_tokens for m in scatter.models for p in m.points}

    # Computed again after the delete: only bob's request, in memory and stored.
    assert _prompts(served) == {17}
    assert calls == 2
    stored = await admin_view_results.read(
        db, "ttft_scatter", metrics.TTFT_SCATTER.store.definition
    )
    assert stored is not None
    assert {p["prompt_tokens"] for m in stored[0]["models"] for p in m["points"]} == {17}


@pytest.mark.asyncio
async def test_a_request_after_a_delete_never_gets_a_scatter_stored_just_before_it(db, monkeypatch):
    """Codex's reproduction: the write committed, the delete completed, then a request.

    The computation's write has committed but not returned (its connection is
    still going back to the pool) when Alice's delete completes. A request made
    after the delete waits for that computation and must not return its result.
    """
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1), user_id="alice", prompt=4242)
        await _log(conn, ago=timedelta(minutes=2), user_id="bob", prompt=17)
    store = metrics.TTFT_SCATTER.store
    real_write = store.write
    stored, resume = asyncio.Event(), asyncio.Event()

    async def _write_then_pause(pool, value, computed_at, generation):
        result = await real_write(pool, value, computed_at, generation)
        if not stored.is_set():
            stored.set()
            await resume.wait()
        return result

    monkeypatch.setattr(store, "write", _write_then_pause)
    db_logger = MagicMock(pool=db)
    before = asyncio.create_task(metrics.admin_get_ttft_scatter(db_logger=db_logger))
    await stored.wait()

    await PostgresLogStore(db, fence_secret=_FENCE_SECRET).hard_delete_user_data("alice")
    after = asyncio.create_task(metrics.admin_get_ttft_scatter(db_logger=db_logger))
    await asyncio.sleep(0.2)
    resume.set()

    def _prompts(scatter):
        return {p.prompt_tokens for m in scatter.models for p in m.points}

    assert _prompts(await after) == {17}
    await before
    assert _prompts(await metrics.admin_get_ttft_scatter(db_logger=db_logger)) == {17}


@pytest.mark.asyncio
async def test_a_request_after_a_delete_never_gets_a_refresh_stored_just_before_it(db, monkeypatch):
    """The third review's reproduction: a kept scatter, refreshing, then a delete.

    The refresh's write has committed but not returned when Alice's delete
    completes; the request after the delete reads the store while the refresh
    finishes. It must return only Bob's request.
    """
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1), user_id="alice", prompt=4242)
        await _log(conn, ago=timedelta(minutes=2), user_id="bob", prompt=17)
    db_logger = MagicMock(pool=db)
    await metrics.admin_get_ttft_scatter(db_logger=db_logger)
    (state,) = metrics.TTFT_SCATTER._states.values()
    state.computed_at -= metrics.TTFT_SCATTER.fresh_seconds + 1

    store = metrics.TTFT_SCATTER.store
    real_write, real_read = store.write, store.read
    stored, resume_write = asyncio.Event(), asyncio.Event()
    reading, resume_read = asyncio.Event(), asyncio.Event()

    async def _write_then_pause(pool, value, computed_at, generation):
        result = await real_write(pool, value, computed_at, generation)
        if not stored.is_set():
            stored.set()
            await resume_write.wait()
        return result

    async def _paused_read(pool):
        if not reading.is_set():
            reading.set()
            await resume_read.wait()
        return await real_read(pool)

    monkeypatch.setattr(store, "write", _write_then_pause)
    monkeypatch.setattr(store, "read", _paused_read)
    await metrics.admin_get_ttft_scatter(db_logger=db_logger)  # starts the refresh
    await stored.wait()

    await PostgresLogStore(db, fence_secret=_FENCE_SECRET).hard_delete_user_data("alice")
    after = asyncio.create_task(metrics.admin_get_ttft_scatter(db_logger=db_logger))
    await reading.wait()
    resume_write.set()
    await asyncio.sleep(0.2)  # the refresh finishes while the request reads
    resume_read.set()

    def _prompts(scatter):
        return {p.prompt_tokens for m in scatter.models for p in m.points}

    assert _prompts(await after) == {17}
    assert _prompts(await metrics.admin_get_ttft_scatter(db_logger=db_logger)) == {17}


@pytest.mark.asyncio
async def test_a_result_held_in_memory_is_not_served_after_a_delete(db):
    async with db.acquire() as conn:
        await _log(conn, ago=timedelta(minutes=1), user_id="alice", prompt=4242)
        await _log(conn, ago=timedelta(minutes=2), user_id="bob", prompt=17)
    db_logger = MagicMock(pool=db)
    before = await metrics.admin_get_ttft_scatter(db_logger=db_logger)
    assert {p.prompt_tokens for m in before.models for p in m.points} == {4242, 17}

    await PostgresLogStore(db, fence_secret=_FENCE_SECRET).hard_delete_user_data("alice")

    after = await metrics.admin_get_ttft_scatter(db_logger=db_logger)
    assert {p.prompt_tokens for m in after.models for p in m.points} == {17}


@pytest.mark.asyncio
async def test_a_restarted_process_serves_the_stored_views_without_computing(db, monkeypatch):
    await _seed(db)
    db_logger = MagicMock(pool=db)
    first = await metrics._get_cached_performance_metrics(db_logger)
    scatter = await metrics.admin_get_ttft_scatter(db_logger=db_logger)
    assert first.generated_at is not None and scatter.models

    # A new process: nothing in memory, and computing would fail.
    metrics.PERFORMANCE_METRICS.clear()
    metrics.TTFT_SCATTER.clear()

    async def _fail(_db_logger):
        raise AssertionError("served from the stored result, not recomputed")

    monkeypatch.setattr(metrics, "_load_performance_metrics", _fail)
    monkeypatch.setattr(metrics, "_load_ttft_scatter", _fail)

    again = await metrics._get_cached_performance_metrics(db_logger)
    assert again.model_dump() == first.model_dump()
    assert (await metrics.admin_get_ttft_scatter(db_logger=db_logger)).model_dump() == (
        scatter.model_dump()
    )


@pytest.mark.asyncio
async def test_a_hard_delete_drops_the_stored_views(db):
    await _seed(db)
    db_logger = MagicMock(pool=db)
    await metrics._get_cached_performance_metrics(db_logger)
    await metrics.admin_get_ttft_scatter(db_logger=db_logger)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM admin_view_results") == 2

    # Someone with no rows: nothing was computed over them, nothing dropped.
    log_store = PostgresLogStore(db, fence_secret=_FENCE_SECRET)
    await log_store.hard_delete_user_data("nobody")
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM admin_view_results") == 2

    await log_store.hard_delete_user_data("alice")
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM admin_view_results") == 0
