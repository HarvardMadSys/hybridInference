# Per-Provider Hourly Performance Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggregate `api_logs` into a new `provider_hourly_stats` rollup table once per hour grouped by `(provider, model_id, hour_bucket)`, and surface the resulting TTFT and throughput time series in a new "Provider Performance" tab on the admin dashboard.

**Architecture:** APScheduler `CronTrigger(minute=5)` job runs an `INSERT ... SELECT` aggregation against `api_logs` for the just-completed hour, guarded by `pg_try_advisory_lock` for multi-replica safety. On startup, a one-shot `backfill_if_empty(days=30)` populates history. Frontend reads pre-aggregated rows via a new admin endpoint and renders two Recharts line charts (TTFT p50/p95/p99 and throughput avg/p50/p95).

**Tech Stack:** Python 3.12 (asyncio/asyncpg/APScheduler/FastAPI/Pydantic), Postgres 14+ (`PERCENTILE_CONT`, advisory locks), pytest + pytest-asyncio integration tests against a real test DB, Prometheus client, Next.js + Recharts + Tailwind.

**Spec:** [docs/superpowers/specs/2026-05-02-per-provider-hourly-performance-design.md](../specs/2026-05-02-per-provider-hourly-performance-design.md)

---

## File Map

**New files:**
- `serving/admin/provider_stats_rollup.py` — rollup SQL constants, `run_rollup`, `hourly_job`, `backfill_if_empty`, `purge_old`, `register_rollup_job`.
- `test/integration/test_provider_stats_rollup.py` — integration tests (rollup correctness, idempotency, lock, purge, backfill).
- `test/servers/test_admin_provider_stats.py` — API tests (auth, filters, range cap).
- `frontend/src/app/dashboard/admin/ProviderPerformanceTab.tsx` — new tab component.
- `infrastructure/prometheus/rules/provider_stats_rollup.yml` — alert rule for stale rollup.

**Modified files:**
- `serving/storage/database.py` — add `provider_hourly_stats` DDL + indexes inside `_create_tables`.
- `serving/utils/email_scheduler.py` — add `get_scheduler()` accessor (renaming the module is out of scope; keep filename).
- `serving/servers/bootstrap.py` — call `register_rollup_job(scheduler, db_logger.pool)` after `email_scheduler.start_scheduler(...)`.
- `serving/schemas_admin.py` — add `ProviderStatsRow`, `ProviderStatsResponse`.
- `serving/servers/routers/admin.py` — add `GET /admin/api/provider-stats`.
- `serving/observability/metrics.py` — add `PROVIDER_STATS_ROLLUP_RUNS_TOTAL`, `PROVIDER_STATS_ROLLUP_DURATION`, `PROVIDER_STATS_LAST_SUCCESS_UNIXTIME`.
- `frontend/src/lib/api/admin.ts` — add types + `getProviderStats(...)` client.
- `frontend/src/app/dashboard/admin/page.tsx` — add `'provider-perf'` tab to the tab union, switch case, and tab nav.

---

## Task 0: Setup — Pull, Worktree, Branch

**Files:** none yet.

- [ ] **Step 1: Pull origin/dev**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull origin dev --no-rebase --ff-only || git pull origin dev
```

- [ ] **Step 2: Create worktree on a new branch**

```bash
cd /home/juncheng/hybridInference
git worktree add .worktrees/provider-hourly-perf -b jason/claude/provider-hourly-perf origin/dev
cd .worktrees/provider-hourly-perf
```

All subsequent tasks run inside `/home/juncheng/hybridInference/.worktrees/provider-hourly-perf`.

- [ ] **Step 3: Verify the worktree**

```bash
git status
git log --oneline -1
```

Expected: `On branch jason/claude/provider-hourly-perf`, HEAD at the latest dev commit.

- [ ] **Step 4: Verify the test database is reachable**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db"
echo "TEST_PG_DSN=$TEST_PG_DSN" > .env.test
uv run python -c "
import asyncio, asyncpg, os
async def main():
    c = await asyncpg.connect('$TEST_PG_DSN')
    print(await c.fetchval('SELECT current_database()'))
    await c.close()
asyncio.run(main())
"
```

Expected: prints `freeinference_test_db`. If this fails, ask the user to start the test DB (`make db-test-up` or local Postgres) before proceeding.

---

## Task 1: Add `provider_hourly_stats` schema to `DatabaseLogger`

**Files:**
- Modify: `serving/storage/database.py`
- Test: `test/integration/test_provider_stats_rollup.py` (new file — schema test only in this task)

- [ ] **Step 1: Write the failing schema test**

Create `test/integration/test_provider_stats_rollup.py` with the following content:

```python
"""Integration tests for provider_hourly_stats rollup."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from serving.storage.database import DatabaseLogger

if TYPE_CHECKING:
    import asyncpg

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


async def _truncate(pool) -> None:
    async with pool.acquire() as conn:
        for table in ("provider_hourly_stats", "api_logs"):
            exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
            if exists:
                await conn.execute(f"TRUNCATE TABLE {table}")


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str):
    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    await _truncate(logger.pool)
    try:
        yield logger
    finally:
        assert logger.pool is not None
        await _truncate(logger.pool)
        await logger.cleanup()


@pytest.mark.asyncio
async def test_provider_hourly_stats_schema(db_logger: DatabaseLogger):
    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'provider_hourly_stats'
            """
        )
        cols = {r["column_name"] for r in rows}

    expected = {
        "hour_bucket", "provider", "model_id",
        "request_count", "error_count", "stream_count",
        "ttft_p50_ms", "ttft_p95_ms", "ttft_p99_ms",
        "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
        "throughput_avg_tps", "throughput_p50_tps", "throughput_p95_tps",
        "prompt_tokens_avg", "completion_tokens_avg", "total_completion_tokens",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


@pytest.mark.asyncio
async def test_provider_hourly_stats_primary_key(db_logger: DatabaseLogger):
    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        pk_cols = await conn.fetch(
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid
                                AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'public.provider_hourly_stats'::regclass
              AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """
        )
        names = [r["column_name"] for r in pk_cols]
    assert names == ["provider", "model_id", "hour_bucket"]
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
cd /home/juncheng/hybridInference/.worktrees/provider-hourly-perf
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py -v
```

Expected: FAIL with `relation "public.provider_hourly_stats" does not exist`.

- [ ] **Step 3: Add the table DDL to `_create_tables`**

In `serving/storage/database.py`, find the end of `_create_tables` (just before the method's last block of `await conn.execute(...)` migrations), and insert this block immediately after the `idx_api_logs_response_hash` index creation (around line 281):

```python
            # ====================================================
            # provider_hourly_stats — hourly rollup of api_logs by
            # (provider, model_id). Populated by the
            # rollup_provider_stats APScheduler job.
            # ====================================================
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS provider_hourly_stats (
                    hour_bucket             TIMESTAMPTZ NOT NULL,
                    provider                TEXT        NOT NULL,
                    model_id                TEXT        NOT NULL,

                    request_count           INTEGER     NOT NULL,
                    error_count             INTEGER     NOT NULL,
                    stream_count            INTEGER     NOT NULL,

                    ttft_p50_ms             INTEGER,
                    ttft_p95_ms             INTEGER,
                    ttft_p99_ms             INTEGER,

                    latency_p50_ms          INTEGER,
                    latency_p95_ms          INTEGER,
                    latency_p99_ms          INTEGER,

                    throughput_avg_tps      FLOAT,
                    throughput_p50_tps      FLOAT,
                    throughput_p95_tps      FLOAT,

                    prompt_tokens_avg       FLOAT,
                    completion_tokens_avg   FLOAT,
                    total_completion_tokens BIGINT      NOT NULL,

                    PRIMARY KEY (provider, model_id, hour_bucket)
                )
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_phs_hour
                ON provider_hourly_stats(hour_bucket DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_phs_provider_hour
                ON provider_hourly_stats(provider, hour_bucket DESC)
            """)
```

- [ ] **Step 4: Run the schema tests, verify they pass**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add serving/storage/database.py test/integration/test_provider_stats_rollup.py
git commit -m "$(cat <<'EOF'
feat(storage): add provider_hourly_stats rollup table

Schema and indexes for the hourly aggregation of api_logs by
(provider, model_id, hour_bucket). Populated by the upcoming
rollup_provider_stats APScheduler job.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Expose scheduler accessor on `email_scheduler`

**Files:**
- Modify: `serving/utils/email_scheduler.py`

The provider-stats rollup job needs to register on the same `AsyncIOScheduler` instance that `email_scheduler` already starts. Add a public accessor.

- [ ] **Step 1: Add `get_scheduler()` to `serving/utils/email_scheduler.py`**

After the `stop_scheduler()` function (around line 54), add:

```python
def get_scheduler() -> AsyncIOScheduler | None:
    """Return the live AsyncIOScheduler, or None if not started.

    Allows other modules (e.g., provider-stats rollup) to register additional
    jobs on the same scheduler.
    """
    return _scheduler
```

- [ ] **Step 2: Verify import**

```bash
uv run python -c "
from serving.utils.email_scheduler import get_scheduler
print(get_scheduler())  # None before start_scheduler is called
"
```

Expected: prints `None`.

- [ ] **Step 3: Commit**

```bash
git add serving/utils/email_scheduler.py
git commit -m "$(cat <<'EOF'
refactor(scheduler): expose get_scheduler() accessor

Allow other modules to register additional jobs on the same
AsyncIOScheduler instance without reaching into module-private state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Build rollup module — SQL constant + `run_rollup`

**Files:**
- Create: `serving/admin/provider_stats_rollup.py`
- Modify: `test/integration/test_provider_stats_rollup.py` (extend)

- [ ] **Step 1: Add the rollup-correctness test**

Append to `test/integration/test_provider_stats_rollup.py`:

```python
async def _insert_api_log(
    pool,
    *,
    request_id: str,
    provider: str,
    model_id: str,
    timestamp: datetime,
    stream: bool,
    ttft_ms: int | None,
    latency_ms: int,
    completion_tokens: int | None,
    prompt_tokens: int | None = 100,
    status_code: int = 200,
    error: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, timestamp,
                stream, ttft_ms, latency_ms,
                prompt_tokens, completion_tokens,
                status_code, error
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            request_id, model_id, provider, timestamp,
            stream, ttft_ms, latency_ms,
            prompt_tokens, completion_tokens,
            status_code, error,
        )


@pytest.mark.asyncio
async def test_run_rollup_aggregates_one_hour(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 13, 0, tzinfo=timezone.utc)
    # 3 successful streaming requests on (openrouter, qwen)
    for i, (ttft, latency, ctok) in enumerate(
        [(400, 5400, 200), (800, 9800, 400), (1200, 12200, 220)]
    ):
        await _insert_api_log(
            pool,
            request_id=f"r-stream-{i}",
            provider="openrouter",
            model_id="qwen/qwen3-coder",
            timestamp=hour + timedelta(minutes=10 + i),
            stream=True,
            ttft_ms=ttft,
            latency_ms=latency,
            completion_tokens=ctok,
        )
    # 1 error
    await _insert_api_log(
        pool,
        request_id="r-err",
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        timestamp=hour + timedelta(minutes=20),
        stream=True,
        ttft_ms=None,
        latency_ms=2000,
        completion_tokens=None,
        status_code=500,
        error="upstream_timeout",
    )
    # 1 non-stream success
    await _insert_api_log(
        pool,
        request_id="r-nostream",
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        timestamp=hour + timedelta(minutes=30),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        completion_tokens=160,
    )

    written = await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    assert written == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM provider_hourly_stats
            WHERE provider='openrouter' AND model_id='qwen/qwen3-coder'
            """
        )
    assert row is not None
    assert row["request_count"] == 5
    assert row["error_count"] == 1
    assert row["stream_count"] == 3  # 3 successful streaming with ttft
    # TTFT p50 of [400, 800, 1200] = 800
    assert row["ttft_p50_ms"] == 800
    # All non-error latencies: [5400, 9800, 12200, 4000] -> p50 ~ 7600
    assert 7000 <= row["latency_p50_ms"] <= 8200
    # Throughput per stream row: ctok / ((latency-ttft)/1000)
    #   r-stream-0: 200 / 5.0 = 40.0
    #   r-stream-1: 400 / 9.0 ≈ 44.44
    #   r-stream-2: 220 / 11.0 = 20.0
    #   r-nostream: 160 / 4.0 = 40.0
    # avg ≈ 36.11
    assert 33.0 <= row["throughput_avg_tps"] <= 40.0
    assert row["total_completion_tokens"] == 200 + 400 + 220 + 160
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py::test_run_rollup_aggregates_one_hour -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'serving.admin.provider_stats_rollup'`.

- [ ] **Step 3: Create the rollup module**

Create `serving/admin/provider_stats_rollup.py`:

```python
"""Hourly rollup of api_logs into provider_hourly_stats.

Spec: docs/superpowers/specs/2026-05-02-per-provider-hourly-performance-design.md
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

# A constant 64-bit integer so all replicas serialize on the same lock.
ADVISORY_LOCK_KEY = 0x70726F76737473  # ascii "provsts" packed

ROLLUP_SQL = """
INSERT INTO provider_hourly_stats AS p (
    hour_bucket, provider, model_id,
    request_count, error_count, stream_count,
    ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
    latency_p50_ms, latency_p95_ms, latency_p99_ms,
    throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
    prompt_tokens_avg, completion_tokens_avg, total_completion_tokens
)
SELECT
    date_trunc('hour', timestamp)                                          AS hour_bucket,
    provider,
    model_id,
    COUNT(*)                                                                AS request_count,
    COUNT(*) FILTER (WHERE status_code >= 400 OR error IS NOT NULL)         AS error_count,
    COUNT(*) FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                          AND status_code < 400 AND error IS NULL)          AS stream_count,

    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p50_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p95_ms,
    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p99_ms,

    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p50_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p95_ms,
    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p99_ms,

    AVG(throughput_tps)                                                     AS throughput_avg_tps,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY throughput_tps)            AS throughput_p50_tps,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY throughput_tps)            AS throughput_p95_tps,

    AVG(prompt_tokens)::FLOAT                                               AS prompt_tokens_avg,
    AVG(completion_tokens)::FLOAT                                           AS completion_tokens_avg,
    COALESCE(SUM(completion_tokens), 0)::BIGINT                             AS total_completion_tokens
FROM (
    SELECT
        timestamp, provider, model_id, status_code, error,
        stream, ttft_ms, latency_ms, prompt_tokens, completion_tokens,
        CASE
            WHEN status_code >= 400
                 OR error IS NOT NULL
                 OR completion_tokens IS NULL
                 OR completion_tokens <= 0                  THEN NULL
            WHEN stream = TRUE AND ttft_ms IS NOT NULL
                 AND latency_ms > ttft_ms
                THEN completion_tokens::FLOAT / ((latency_ms - ttft_ms) / 1000.0)
            WHEN latency_ms > 0
                THEN completion_tokens::FLOAT / (latency_ms / 1000.0)
            ELSE NULL
        END AS throughput_tps
    FROM api_logs
    WHERE timestamp >= $1 AND timestamp < $2
) src
GROUP BY hour_bucket, provider, model_id
HAVING COUNT(*) > 0
ON CONFLICT (provider, model_id, hour_bucket) DO UPDATE SET
    request_count           = EXCLUDED.request_count,
    error_count             = EXCLUDED.error_count,
    stream_count            = EXCLUDED.stream_count,
    ttft_p50_ms             = EXCLUDED.ttft_p50_ms,
    ttft_p95_ms             = EXCLUDED.ttft_p95_ms,
    ttft_p99_ms             = EXCLUDED.ttft_p99_ms,
    latency_p50_ms          = EXCLUDED.latency_p50_ms,
    latency_p95_ms          = EXCLUDED.latency_p95_ms,
    latency_p99_ms          = EXCLUDED.latency_p99_ms,
    throughput_avg_tps      = EXCLUDED.throughput_avg_tps,
    throughput_p50_tps      = EXCLUDED.throughput_p50_tps,
    throughput_p95_tps      = EXCLUDED.throughput_p95_tps,
    prompt_tokens_avg       = EXCLUDED.prompt_tokens_avg,
    completion_tokens_avg   = EXCLUDED.completion_tokens_avg,
    total_completion_tokens = EXCLUDED.total_completion_tokens
"""


async def run_rollup(
    pool: "asyncpg.Pool",
    *,
    start: datetime,
    end: datetime,
) -> int:
    """Aggregate api_logs in the half-open interval [start, end) into
    provider_hourly_stats. Returns number of rows affected (inserted+updated).

    Idempotent: re-running with the same window updates existing rows.
    Caller is responsible for taking the advisory lock when concurrent
    runs are possible.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be tz-aware")
    if end <= start:
        raise ValueError("end must be after start")

    async with pool.acquire() as conn:
        result = await conn.execute(ROLLUP_SQL, start, end)
    # asyncpg returns "INSERT 0 N" for INSERT statements (the 0 is oid).
    # Parse the trailing integer.
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0
```

Also create `serving/admin/__init__.py` if it doesn't exist:

```bash
test -f serving/admin/__init__.py || : > serving/admin/__init__.py
```

- [ ] **Step 4: Run the rollup test, verify it passes**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py::test_run_rollup_aggregates_one_hour -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add serving/admin/provider_stats_rollup.py serving/admin/__init__.py \
        test/integration/test_provider_stats_rollup.py
git commit -m "$(cat <<'EOF'
feat(admin): add provider stats rollup module

run_rollup aggregates api_logs into provider_hourly_stats over a
half-open hourly window using PERCENTILE_CONT and per-row throughput
(decode-only when streaming). Idempotent via PRIMARY KEY UPSERT.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Idempotency test for `run_rollup`

**Files:**
- Modify: `test/integration/test_provider_stats_rollup.py`

- [ ] **Step 1: Add the idempotency test**

Append to `test/integration/test_provider_stats_rollup.py`:

```python
@pytest.mark.asyncio
async def test_run_rollup_is_idempotent(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
    for i in range(3):
        await _insert_api_log(
            pool,
            request_id=f"idem-{i}",
            provider="anthropic",
            model_id="claude-opus-4-7",
            timestamp=hour + timedelta(minutes=5 + i),
            stream=True, ttft_ms=300 + i * 50,
            latency_ms=2000 + i * 100, completion_tokens=120,
        )

    await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))

    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
        row = await conn.fetchrow(
            "SELECT request_count FROM provider_hourly_stats LIMIT 1"
        )
    assert n == 1
    assert row["request_count"] == 3
```

- [ ] **Step 2: Run, verify it passes**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py::test_run_rollup_is_idempotent -v
```

Expected: PASS (UPSERT already implemented in Task 3).

- [ ] **Step 3: Commit**

```bash
git add test/integration/test_provider_stats_rollup.py
git commit -m "$(cat <<'EOF'
test(admin): rollup is idempotent on repeated runs

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `hourly_job` + `purge_old` + advisory lock

**Files:**
- Modify: `serving/admin/provider_stats_rollup.py`
- Modify: `test/integration/test_provider_stats_rollup.py`

- [ ] **Step 1: Add the hourly_job + purge tests**

Append to `test/integration/test_provider_stats_rollup.py`:

```python
@pytest.mark.asyncio
async def test_hourly_job_rolls_up_previous_hour(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import hourly_job

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    prev = now - timedelta(hours=1)

    await _insert_api_log(
        pool,
        request_id="hr-1",
        provider="chutes",
        model_id="meta/llama-3.3-70b",
        timestamp=prev + timedelta(minutes=12),
        stream=True, ttft_ms=500, latency_ms=4500, completion_tokens=300,
    )

    await hourly_job(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT request_count, hour_bucket
            FROM provider_hourly_stats
            WHERE provider='chutes' AND model_id='meta/llama-3.3-70b'
            """
        )
    assert row is not None
    assert row["request_count"] == 1
    # The bucket equals the previous hour (hour-truncated)
    assert row["hour_bucket"] == prev


@pytest.mark.asyncio
async def test_purge_old_removes_aged_rows(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import purge_old

    assert db_logger.pool is not None
    pool = db_logger.pool

    old = datetime.now(timezone.utc) - timedelta(days=45)
    new = datetime.now(timezone.utc) - timedelta(days=2)

    async with pool.acquire() as conn:
        for ts in (old, new):
            await conn.execute(
                """
                INSERT INTO provider_hourly_stats (
                    hour_bucket, provider, model_id,
                    request_count, error_count, stream_count,
                    total_completion_tokens
                ) VALUES ($1, 'p', 'm', 1, 0, 1, 100)
                ON CONFLICT DO NOTHING
                """,
                ts.replace(minute=0, second=0, microsecond=0),
            )

    deleted = await purge_old(pool, retention_days=30)
    assert deleted == 1

    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
    assert n == 1


@pytest.mark.asyncio
async def test_hourly_job_skips_when_locked(db_logger: DatabaseLogger, pg_dsn: str):
    """A second concurrent hourly_job acquires no lock and returns no work."""
    import asyncpg
    from serving.admin.provider_stats_rollup import hourly_job, ADVISORY_LOCK_KEY

    assert db_logger.pool is not None
    pool = db_logger.pool

    # Hold the advisory lock on a separate session.
    holder = await asyncpg.connect(pg_dsn)
    try:
        got = await holder.fetchval(
            "SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY
        )
        assert got is True

        # Run hourly_job: should log "lock held, skipping" and not insert.
        await hourly_job(pool)

        async with pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
        assert n == 0
    finally:
        await holder.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
        await holder.close()
```

- [ ] **Step 2: Run, verify they fail**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py -v -k "hourly or purge"
```

Expected: FAIL with `ImportError: cannot import name 'hourly_job'` (and similar for `purge_old`).

- [ ] **Step 3: Implement `hourly_job`, `purge_old`, advisory-lock wrapper**

Append to `serving/admin/provider_stats_rollup.py`:

```python
PURGE_SQL = """
DELETE FROM provider_hourly_stats
WHERE hour_bucket < NOW() - $1::interval
"""


async def purge_old(pool: "asyncpg.Pool", *, retention_days: int = 30) -> int:
    """Delete rows older than retention_days. Returns count deleted."""
    async with pool.acquire() as conn:
        result = await conn.execute(PURGE_SQL, f"{retention_days} days")
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def _try_lock_run(
    pool: "asyncpg.Pool",
    coro_factory,
) -> bool:
    """Acquire pg_try_advisory_lock; if it succeeds, run coro_factory(conn).

    Returns True if the work ran, False if the lock was already held.
    The lock is scoped to a dedicated connection that we hold for the
    duration of the work and release in a finally.
    """
    async with pool.acquire() as conn:
        got = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY
        )
        if not got:
            logger.info("rollup_provider_stats: lock held, skipping")
            return False
        try:
            await coro_factory(conn)
            return True
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY
            )


async def hourly_job(
    pool: "asyncpg.Pool",
    *,
    retention_days: int = 30,
) -> None:
    """APScheduler entrypoint. Roll up the previous full hour, then purge."""
    started_at = time.monotonic()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start, end = now - timedelta(hours=1), now

    rows_written = 0
    outcome = "error"

    async def _do(conn) -> None:
        nonlocal rows_written
        result = await conn.execute(ROLLUP_SQL, start, end)
        try:
            rows_written = int(result.rsplit(" ", 1)[-1])
        except ValueError:
            rows_written = 0
        await conn.execute(PURGE_SQL, f"{retention_days} days")

    try:
        ran = await _try_lock_run(pool, _do)
        outcome = "ok" if ran else "locked"
    except Exception as exc:
        logger.exception(f"rollup_provider_stats failed: {exc}")
        outcome = "error"
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "rollup_provider_stats: window=[%s, %s) rows=%d duration_ms=%d outcome=%s",
            start.isoformat(),
            end.isoformat(),
            rows_written,
            duration_ms,
            outcome,
        )
        # Metrics emission is added in Task 9.
```

- [ ] **Step 4: Run, verify they pass**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/admin/provider_stats_rollup.py test/integration/test_provider_stats_rollup.py
git commit -m "$(cat <<'EOF'
feat(admin): add hourly_job, purge_old, advisory-lock guard

hourly_job rolls up the previous completed hour under a Postgres
advisory lock so concurrent replicas serialize. purge_old enforces
30-day retention. Includes an idempotency + lock-skip test.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `backfill_if_empty`

**Files:**
- Modify: `serving/admin/provider_stats_rollup.py`
- Modify: `test/integration/test_provider_stats_rollup.py`

- [ ] **Step 1: Add backfill test**

Append to `test/integration/test_provider_stats_rollup.py`:

```python
@pytest.mark.asyncio
async def test_backfill_if_empty_seeds_history(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import backfill_if_empty

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Seed 3 distinct hours of traffic
    for h in (now - timedelta(hours=3), now - timedelta(hours=2), now - timedelta(hours=1)):
        await _insert_api_log(
            pool,
            request_id=f"bf-{h.isoformat()}",
            provider="zai",
            model_id="zai/glm-4.6",
            timestamp=h + timedelta(minutes=5),
            stream=True, ttft_ms=500, latency_ms=3500, completion_tokens=200,
        )

    await backfill_if_empty(pool, days=1)

    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
    assert n == 3

    # Second call must be a no-op (table not empty).
    await backfill_if_empty(pool, days=1)
    async with pool.acquire() as conn:
        n2 = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
    assert n2 == 3
```

- [ ] **Step 2: Run, verify it fails**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py::test_backfill_if_empty_seeds_history -v
```

Expected: FAIL with `ImportError: cannot import name 'backfill_if_empty'`.

- [ ] **Step 3: Implement `backfill_if_empty`**

Append to `serving/admin/provider_stats_rollup.py`:

```python
async def backfill_if_empty(
    pool: "asyncpg.Pool",
    *,
    days: int = 30,
) -> int:
    """If provider_hourly_stats has no rows, aggregate the last `days` of
    api_logs in a single pass. Idempotent: no-op when rows exist.
    Returns the number of rows written (0 when skipped).
    """
    async with pool.acquire() as conn:
        any_row = await conn.fetchval(
            "SELECT 1 FROM provider_hourly_stats LIMIT 1"
        )
    if any_row is not None:
        logger.info("backfill_if_empty: table populated, skipping")
        return 0

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    rows = await run_rollup(pool, start=start, end=end)
    logger.info(
        "backfill_if_empty: window=[%s, %s) rows=%d", start.isoformat(),
        end.isoformat(), rows,
    )
    return rows
```

- [ ] **Step 4: Run, verify it passes**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py::test_backfill_if_empty_seeds_history -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add serving/admin/provider_stats_rollup.py test/integration/test_provider_stats_rollup.py
git commit -m "$(cat <<'EOF'
feat(admin): backfill_if_empty seeds 30 days on first deploy

One-shot, idempotent: only runs when provider_hourly_stats is empty.
Uses run_rollup over a wide window so charts have history immediately.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Bootstrap registration

**Files:**
- Modify: `serving/admin/provider_stats_rollup.py`
- Modify: `serving/servers/bootstrap.py`

- [ ] **Step 1: Add `register_rollup_job` helper**

Append to `serving/admin/provider_stats_rollup.py`:

```python
def register_rollup_job(scheduler, pool: "asyncpg.Pool") -> None:
    """Register the hourly rollup job on the existing AsyncIOScheduler.

    Fires at minute 5 every hour to give the previous hour's writes
    time to flush.
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        hourly_job,
        trigger=CronTrigger(minute=5, timezone=timezone.utc),
        args=[pool],
        id="rollup_provider_stats",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("rollup_provider_stats: registered on scheduler (cron minute=5)")
```

- [ ] **Step 2: Wire into bootstrap**

In `serving/servers/bootstrap.py`, find the existing block that starts the email scheduler (around lines 342-354). Inside the `try` block, immediately after the line `await email_scheduler.rehydrate_scheduled_broadcasts()`, add:

```python
                        # Provider-stats hourly rollup
                        from serving.admin.provider_stats_rollup import (
                            backfill_if_empty,
                            register_rollup_job,
                        )

                        sched = email_scheduler.get_scheduler()
                        if sched is not None:
                            register_rollup_job(sched, db_logger.pool)
                            try:
                                await backfill_if_empty(db_logger.pool, days=30)
                            except Exception as bf_exc:
                                logger.warning(
                                    f"provider-stats backfill failed (non-fatal): {bf_exc}"
                                )
```

This runs only when both the DB pool and the scheduler are healthy. A backfill failure does not block startup.

- [ ] **Step 3: Verify imports**

```bash
uv run python -c "
from serving.servers.bootstrap import initialize  # noqa: F401
from serving.admin.provider_stats_rollup import register_rollup_job, backfill_if_empty  # noqa: F401
print('ok')
"
```

Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add serving/admin/provider_stats_rollup.py serving/servers/bootstrap.py
git commit -m "$(cat <<'EOF'
feat(serving): register hourly provider-stats rollup at startup

CronTrigger(minute=5) on the existing AsyncIOScheduler. On startup,
runs backfill_if_empty(days=30) so charts have history on first
deploy. Backfill failure is logged and swallowed so it cannot block
server startup.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Pydantic schemas for the response

**Files:**
- Modify: `serving/schemas_admin.py`

- [ ] **Step 1: Add the schemas**

Append to `serving/schemas_admin.py` (at the bottom of the file, before any trailing `__all__` if present — otherwise just at end):

```python
# ============================================================
# Provider Performance (admin /admin/api/provider-stats)
# ============================================================


class ProviderStatsRow(BaseModel):
    hour_bucket: datetime
    provider: str
    model_id: str

    request_count: int
    error_count: int
    stream_count: int

    ttft_p50_ms: int | None = None
    ttft_p95_ms: int | None = None
    ttft_p99_ms: int | None = None

    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    latency_p99_ms: int | None = None

    throughput_avg_tps: float | None = None
    throughput_p50_tps: float | None = None
    throughput_p95_tps: float | None = None

    prompt_tokens_avg: float | None = None
    completion_tokens_avg: float | None = None
    total_completion_tokens: int


class ProviderStatsResponse(BaseModel):
    rows: list[ProviderStatsRow]
    providers: list[str]
    models: list[str]
```

If `BaseModel` and `datetime` are not already imported at the top of `serving/schemas_admin.py`, add them:

```python
from datetime import datetime
from pydantic import BaseModel
```

- [ ] **Step 2: Verify schemas import**

```bash
uv run python -c "
from serving.schemas_admin import ProviderStatsRow, ProviderStatsResponse
print(ProviderStatsRow.model_fields.keys())
"
```

Expected: prints the field names defined above.

- [ ] **Step 3: Commit**

```bash
git add serving/schemas_admin.py
git commit -m "$(cat <<'EOF'
feat(admin): schemas for /admin/api/provider-stats

ProviderStatsRow mirrors provider_hourly_stats columns; the wrapper
response also returns distinct providers/models lists for dashboard
dropdowns.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Admin API endpoint

**Files:**
- Modify: `serving/servers/routers/admin.py`
- Create: `test/servers/test_admin_provider_stats.py`

- [ ] **Step 1: Write the failing API tests**

Look at `test/servers/test_admin_provider_quotas.py` for the existing admin-route test pattern (FastAPI TestClient + admin auth). Mirror its setup. Create `test/servers/test_admin_provider_stats.py`:

```python
"""API tests for /admin/api/provider-stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from serving.storage.database import DatabaseLogger

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def populated_pool(pg_dsn):
    """A DB pool with the schema in place and one row in provider_hourly_stats."""
    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    async with logger.pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE provider_hourly_stats")
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                ttft_p50_ms, throughput_avg_tps, total_completion_tokens
            )
            VALUES ($1, 'openrouter', 'qwen/qwen3-coder',
                    100, 1, 90, 410, 42.0, 9000)
            """,
            datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            - timedelta(hours=1),
        )
    try:
        yield logger.pool
    finally:
        async with logger.pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE provider_hourly_stats")
        await logger.cleanup()


@pytest.fixture
def pg_dsn():
    import os

    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set")
    return dsn


def test_provider_stats_requires_admin(client_no_admin):
    # client_no_admin is the existing fixture from test/servers/conftest.py that
    # builds a TestClient without admin auth headers. If a different fixture name
    # is used in this codebase, swap accordingly — see test_admin_provider_quotas.py.
    resp = client_no_admin.get(
        "/admin/api/provider-stats",
        params={"provider": "openrouter", "model_id": "qwen/qwen3-coder"},
    )
    assert resp.status_code in (401, 403)


def test_provider_stats_happy_path(client_admin, populated_pool):
    resp = client_admin.get(
        "/admin/api/provider-stats",
        params={"provider": "openrouter", "model_id": "qwen/qwen3-coder"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "rows" in body and "providers" in body and "models" in body
    assert any(r["provider"] == "openrouter" for r in body["rows"])
    assert "openrouter" in body["providers"]
    assert "qwen/qwen3-coder" in body["models"]


def test_provider_stats_rejects_oversize_range(client_admin):
    resp = client_admin.get(
        "/admin/api/provider-stats",
        params={
            "provider": "openrouter",
            "model_id": "qwen/qwen3-coder",
            "from": "2024-01-01T00:00:00Z",
            "to": "2026-01-01T00:00:00Z",
        },
    )
    assert resp.status_code == 400
```

> **Note:** This codebase's TestClient fixtures live in `test/servers/conftest.py`. Open it and use whichever names exist for "client with admin auth" and "client without admin auth" — the names above are placeholders and must be replaced with the actual fixture names from `test_admin_provider_quotas.py` before running. If only one client fixture exists and it sends an admin token by default, simulate "no admin" by clearing the header on that fixture. Do not invent new fixtures.

- [ ] **Step 2: Run, verify it fails**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/servers/test_admin_provider_stats.py -v
```

Expected: FAIL — endpoint not implemented (404), or fixture-name fix needed (resolve those, then expect 404).

- [ ] **Step 3: Implement the endpoint**

In `serving/servers/routers/admin.py`, append (after the existing `admin_provider_quotas` route):

```python
from serving.schemas_admin import ProviderStatsResponse, ProviderStatsRow

_PROVIDER_STATS_MAX_DAYS = 90
_PROVIDER_STATS_DEFAULT_DAYS = 7


def _truncate_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


@router.get("/admin/api/provider-stats", response_model=ProviderStatsResponse)
async def admin_provider_stats(
    request: Request,
    provider: str,
    model_id: str,
    from_: datetime | None = None,
    to: datetime | None = None,
    _admin_id: str = Depends(verify_admin_access),
) -> ProviderStatsResponse:
    """Return hourly performance stats for a provider+model in a time window."""
    # FastAPI cannot use 'from' as a Python identifier; expose it via alias.
    # (See Step 4 for the `from` alias wiring.)

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        # Fall back to the db_logger's pool if app.state.db_pool isn't set.
        services = getattr(request.app.state, "services", None)
        db_logger = getattr(services, "db_logger", None) if services else None
        pool = getattr(db_logger, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database unavailable")

    now = datetime.now(timezone.utc)
    end = _truncate_hour(to) if to else _truncate_hour(now)
    start = _truncate_hour(from_) if from_ else end - timedelta(days=_PROVIDER_STATS_DEFAULT_DAYS)

    if end <= start:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")
    if (end - start) > timedelta(days=_PROVIDER_STATS_MAX_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"range must be <= {_PROVIDER_STATS_MAX_DAYS} days",
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT hour_bucket, provider, model_id,
                   request_count, error_count, stream_count,
                   ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
                   latency_p50_ms, latency_p95_ms, latency_p99_ms,
                   throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
                   prompt_tokens_avg, completion_tokens_avg, total_completion_tokens
              FROM provider_hourly_stats
             WHERE provider = $1 AND model_id = $2
               AND hour_bucket >= $3 AND hour_bucket < $4
             ORDER BY hour_bucket ASC
            """,
            provider, model_id, start, end,
        )
        providers = await conn.fetch(
            """
            SELECT DISTINCT provider FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY provider
            """,
            start, end,
        )
        models = await conn.fetch(
            """
            SELECT DISTINCT model_id FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY model_id
            """,
            start, end,
        )

    return ProviderStatsResponse(
        rows=[ProviderStatsRow(**dict(r)) for r in rows],
        providers=[r["provider"] for r in providers],
        models=[r["model_id"] for r in models],
    )
```

- [ ] **Step 4: Wire the `from` query alias**

FastAPI cannot bind a parameter named `from` directly. Replace the `from_: datetime | None = None,` parameter declaration above with:

```python
    from_: datetime | None = Query(default=None, alias="from"),
```

Add `Query` to the existing FastAPI import at the top of `serving/servers/routers/admin.py` if it isn't already imported (e.g., `from fastapi import APIRouter, Depends, HTTPException, Query, Request`).

- [ ] **Step 5: Verify the test client can reach the route**

If `client_admin`'s underlying app does not expose `app.state.services.db_logger.pool` to this route, also wire `app.state.db_pool` once at app construction. Search for `app.state` in `serving/servers/bootstrap.py` and `serving/servers/app.py` (or equivalent) and add `app.state.db_pool = db_logger.pool` next to existing state assignments. If state already exposes services, leave as-is.

- [ ] **Step 6: Run the API tests**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/servers/test_admin_provider_stats.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add serving/servers/routers/admin.py test/servers/test_admin_provider_stats.py
# Only stage bootstrap.py / app.py if Step 5 required wiring app.state.db_pool.
git status
git commit -m "$(cat <<'EOF'
feat(admin): GET /admin/api/provider-stats

Returns rows from provider_hourly_stats for a (provider, model_id)
filter and a time window, plus distinct providers and models for
dashboard dropdowns. Range capped at 90 days.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Prometheus metrics for the rollup job

**Files:**
- Modify: `serving/observability/metrics.py`
- Modify: `serving/admin/provider_stats_rollup.py`

- [ ] **Step 1: Add the metrics**

In `serving/observability/metrics.py`, find the section where existing counters and gauges are declared (around the same area as the existing `provider_*` metrics) and add:

```python
PROVIDER_STATS_ROLLUP_RUNS_TOTAL = Counter(
    "provider_stats_rollup_runs_total",
    "Hourly provider-stats rollup runs by outcome",
    labelnames=("outcome",),  # ok | locked | error
)

PROVIDER_STATS_ROLLUP_DURATION = Histogram(
    "provider_stats_rollup_duration_seconds",
    "Duration of the hourly provider-stats rollup job",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

PROVIDER_STATS_LAST_SUCCESS_UNIXTIME = Gauge(
    "provider_stats_last_success_unixtime",
    "Wall-clock unixtime of the last successful provider-stats rollup",
)
```

If `Counter`, `Histogram`, `Gauge` aren't all imported in this file already, extend the existing `from prometheus_client import ...` line.

- [ ] **Step 2: Wire emission into `hourly_job`**

In `serving/admin/provider_stats_rollup.py`, replace the trailing `# Metrics emission is added in Task 9.` comment in `hourly_job` with metric updates. The `finally:` block becomes:

```python
    finally:
        duration_s = time.monotonic() - started_at
        duration_ms = int(duration_s * 1000)
        logger.info(
            "rollup_provider_stats: window=[%s, %s) rows=%d duration_ms=%d outcome=%s",
            start.isoformat(),
            end.isoformat(),
            rows_written,
            duration_ms,
            outcome,
        )
        try:
            from serving.observability.metrics import (
                PROVIDER_STATS_LAST_SUCCESS_UNIXTIME,
                PROVIDER_STATS_ROLLUP_DURATION,
                PROVIDER_STATS_ROLLUP_RUNS_TOTAL,
            )

            PROVIDER_STATS_ROLLUP_RUNS_TOTAL.labels(outcome=outcome).inc()
            PROVIDER_STATS_ROLLUP_DURATION.observe(duration_s)
            if outcome == "ok":
                PROVIDER_STATS_LAST_SUCCESS_UNIXTIME.set(time.time())
        except Exception:
            # Metrics must never break the job.
            pass
```

- [ ] **Step 3: Verify the metric is exposed**

```bash
uv run python -c "
from serving.observability.metrics import (
    PROVIDER_STATS_ROLLUP_RUNS_TOTAL,
    PROVIDER_STATS_ROLLUP_DURATION,
    PROVIDER_STATS_LAST_SUCCESS_UNIXTIME,
)
print(type(PROVIDER_STATS_ROLLUP_RUNS_TOTAL).__name__,
      type(PROVIDER_STATS_ROLLUP_DURATION).__name__,
      type(PROVIDER_STATS_LAST_SUCCESS_UNIXTIME).__name__)
"
```

Expected: `Counter Histogram Gauge`.

- [ ] **Step 4: Run the integration tests again to make sure nothing regressed**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add serving/observability/metrics.py serving/admin/provider_stats_rollup.py
git commit -m "$(cat <<'EOF'
feat(observability): metrics for provider-stats rollup

Counter (outcome=ok|locked|error), histogram (duration_seconds), and
gauge (last_success_unixtime). Metric emission is wrapped in
try/except so it never breaks the job.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Alertmanager rule for stale rollup

**Files:**
- Create: `infrastructure/prometheus/rules/provider_stats_rollup.yml`

- [ ] **Step 1: Verify the rules directory exists and find an existing rule for style reference**

```bash
ls infrastructure/prometheus/rules/ | head
```

Open one existing `.yml` rule file and note its overall structure (`groups:`, `rules:`, `alert:`, `expr:`, `labels:`, `annotations:`). Mirror its style.

- [ ] **Step 2: Create the rule file**

Create `infrastructure/prometheus/rules/provider_stats_rollup.yml`:

```yaml
groups:
  - name: provider_stats_rollup
    interval: 1m
    rules:
      - alert: ProviderStatsRollupStale
        expr: time() - provider_stats_last_success_unixtime > 3 * 3600
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Provider-stats rollup has not succeeded in over 3 hours"
          description: |
            The hourly provider-stats rollup job (rollup_provider_stats) last
            succeeded at {{ $value | humanizeTimestamp }}. Investigate the
            serving process logs for "rollup_provider_stats" entries.
```

If the existing rule files use a different `severity` label or annotation key naming, conform to whatever the existing rules use. Do not invent new label keys.

- [ ] **Step 3: Validate (best-effort) with `promtool` if available**

```bash
which promtool && promtool check rules infrastructure/prometheus/rules/provider_stats_rollup.yml || echo "promtool not present; skipping syntax check"
```

If promtool is present and reports an error, fix the YAML.

- [ ] **Step 4: Commit**

```bash
git add infrastructure/prometheus/rules/provider_stats_rollup.yml
git commit -m "$(cat <<'EOF'
ops(prometheus): alert when provider-stats rollup goes stale

Fires when the rollup has not succeeded for over 3 hours.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Frontend API client + types

**Files:**
- Modify: `frontend/src/lib/api/admin.ts`

- [ ] **Step 1: Add types and the client function**

In `frontend/src/lib/api/admin.ts`, near other admin types/clients, add:

```ts
export interface ProviderStatsRow {
  hour_bucket: string;
  provider: string;
  model_id: string;
  request_count: number;
  error_count: number;
  stream_count: number;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
  ttft_p99_ms: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  latency_p99_ms: number | null;
  throughput_avg_tps: number | null;
  throughput_p50_tps: number | null;
  throughput_p95_tps: number | null;
  prompt_tokens_avg: number | null;
  completion_tokens_avg: number | null;
  total_completion_tokens: number;
}

export interface ProviderStatsResponse {
  rows: ProviderStatsRow[];
  providers: string[];
  models: string[];
}

export async function getProviderStats(params: {
  provider: string;
  model_id: string;
  from?: string;
  to?: string;
}): Promise<ProviderStatsResponse> {
  const search = new URLSearchParams({
    provider: params.provider,
    model_id: params.model_id,
    ...(params.from ? { from: params.from } : {}),
    ...(params.to ? { to: params.to } : {}),
  });
  const res = await fetch(`/admin/api/provider-stats?${search}`, {
    credentials: 'include',
  });
  if (!res.ok) {
    throw new Error(`provider-stats: ${res.status} ${res.statusText}`);
  }
  return res.json();
}
```

If this file uses a different fetch wrapper (look for an existing `apiFetch` or `request` helper), use that instead of bare `fetch` to match conventions.

- [ ] **Step 2: Verify the frontend type-checks**

```bash
cd frontend
pnpm run typecheck || npm run typecheck
cd ..
```

Expected: no new TypeScript errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api/admin.ts
git commit -m "$(cat <<'EOF'
feat(frontend): admin API client for /admin/api/provider-stats

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Frontend Provider Performance tab component

**Files:**
- Create: `frontend/src/app/dashboard/admin/ProviderPerformanceTab.tsx`

- [ ] **Step 1: Create the component**

Mirror the structure of the existing `AnalyticsTab.tsx`. Create `frontend/src/app/dashboard/admin/ProviderPerformanceTab.tsx`:

```tsx
'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Legend,
} from 'recharts';
import {
  ProviderStatsResponse,
  ProviderStatsRow,
  getProviderStats,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

type RangeKey = '24h' | '7d' | '30d';

const RANGES: { key: RangeKey; label: string; days: number }[] = [
  { key: '24h', label: 'Last 24h', days: 1 },
  { key: '7d', label: 'Last 7d', days: 7 },
  { key: '30d', label: 'Last 30d', days: 30 },
];

function rangeWindow(key: RangeKey): { from: string; to: string } {
  const days = RANGES.find((r) => r.key === key)?.days ?? 7;
  const to = new Date();
  to.setMinutes(0, 0, 0);
  const from = new Date(to.getTime() - days * 24 * 60 * 60 * 1000);
  return { from: from.toISOString(), to: to.toISOString() };
}

function fmtHour(iso: string): string {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:00`;
}

export function ProviderPerformanceTab() {
  const [data, setData] = useState<ProviderStatsResponse | null>(null);
  const [providers, setProviders] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [provider, setProvider] = useState<string>('');
  const [model, setModel] = useState<string>('');
  const [range, setRange] = useState<RangeKey>('7d');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Bootstrap dropdowns by issuing a request with placeholder filters and
  // reading the providers/models lists out of the response.
  const loadFilters = useCallback(async () => {
    try {
      const window_ = rangeWindow(range);
      const resp = await getProviderStats({
        provider: provider || '__none__',
        model_id: model || '__none__',
        from: window_.from,
        to: window_.to,
      });
      setProviders(resp.providers);
      setModels(resp.models);
      if (!provider && resp.providers.length > 0) setProvider(resp.providers[0]);
      if (!model && resp.models.length > 0) setModel(resp.models[0]);
    } catch (exc) {
      setError(getErrorMessage(exc));
    }
  }, [provider, model, range]);

  const loadData = useCallback(async () => {
    if (!provider || !model) return;
    setLoading(true);
    setError(null);
    try {
      const window_ = rangeWindow(range);
      const resp = await getProviderStats({
        provider,
        model_id: model,
        from: window_.from,
        to: window_.to,
      });
      setData(resp);
      setProviders(resp.providers);
      setModels(resp.models);
    } catch (exc) {
      setError(getErrorMessage(exc));
    } finally {
      setLoading(false);
    }
  }, [provider, model, range]);

  useEffect(() => {
    void loadFilters();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const chartData = useMemo(
    () =>
      (data?.rows ?? []).map((r: ProviderStatsRow) => ({
        t: fmtHour(r.hour_bucket),
        ttft_p50: r.ttft_p50_ms ?? null,
        ttft_p95: r.ttft_p95_ms ?? null,
        ttft_p99: r.ttft_p99_ms ?? null,
        thru_avg: r.throughput_avg_tps ?? null,
        thru_p50: r.throughput_p50_tps ?? null,
        thru_p95: r.throughput_p95_tps ?? null,
      })),
    [data],
  );

  const totals = useMemo(() => {
    const rows = data?.rows ?? [];
    const requests = rows.reduce((acc, r) => acc + r.request_count, 0);
    const errors = rows.reduce((acc, r) => acc + r.error_count, 0);
    const tokens = rows.reduce((acc, r) => acc + r.total_completion_tokens, 0);
    const errorRate = requests === 0 ? 0 : errors / requests;
    return { requests, errors, errorRate, tokens };
  }, [data]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3 items-end">
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Provider</span>
          <select
            className="border rounded px-2 py-1"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            {providers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Model</span>
          <select
            className="border rounded px-2 py-1"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Range</span>
          <select
            className="border rounded px-2 py-1"
            value={range}
            onChange={(e) => setRange(e.target.value as RangeKey)}
          >
            {RANGES.map((r) => (
              <option key={r.key} value={r.key}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error ? <div className="text-red-600 text-sm">{error}</div> : null}
      {loading ? <div className="text-gray-500 text-sm">Loading…</div> : null}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Kpi label="Requests" value={totals.requests.toLocaleString()} />
        <Kpi
          label="Error rate"
          value={`${(totals.errorRate * 100).toFixed(2)}%`}
        />
        <Kpi
          label="Completion tokens"
          value={totals.tokens.toLocaleString()}
        />
      </div>

      <div className="rounded-xl border p-4">
        <p className="text-sm font-semibold mb-2">TTFT (ms)</p>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="t" minTickGap={32} />
              <YAxis />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="ttft_p50" stroke="#3b82f6" dot={false} name="p50" />
              <Line type="monotone" dataKey="ttft_p95" stroke="#f59e0b" dot={false} name="p95" />
              <Line type="monotone" dataKey="ttft_p99" stroke="#ef4444" dot={false} name="p99" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="rounded-xl border p-4">
        <p className="text-sm font-semibold mb-2">Throughput (tokens/sec)</p>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="t" minTickGap={32} />
              <YAxis />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="thru_avg" stroke="#10b981" dot={false} name="avg" />
              <Line type="monotone" dataKey="thru_p50" stroke="#3b82f6" dot={false} name="p50" />
              <Line type="monotone" dataKey="thru_p95" stroke="#8b5cf6" dot={false} name="p95" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border p-4">
      <p className="text-[11px] uppercase tracking-wide text-gray-400">{label}</p>
      <p className="mt-1 text-2xl font-bold text-gray-900">{value}</p>
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend
pnpm run typecheck || npm run typecheck
cd ..
```

Expected: no new TS errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/ProviderPerformanceTab.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): Provider Performance tab component

Two Recharts line charts (TTFT p50/p95/p99 and throughput
avg/p50/p95) plus a KPI strip. Provider/model/range filters.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Wire the new tab into the admin page

**Files:**
- Modify: `frontend/src/app/dashboard/admin/page.tsx`

- [ ] **Step 1: Extend the tab type union**

In `frontend/src/app/dashboard/admin/page.tsx`, find every occurrence of the literal type string `'users' | 'audit' | 'requests' | 'broadcast' | 'providers' | 'analytics'` (search for `'broadcast' | 'providers'`). Add `| 'provider-perf'` to each. Lines to update (per current grep): 332, 339-346, 718, 807, 818-826.

For example, the tab list literal at line 807 becomes:

```tsx
{(['users', 'requests', 'providers', 'provider-perf', 'audit', 'broadcast', 'analytics'] as const).map(
```

In the tab-label switch (around lines 818-826), add a branch for the new tab key returning a human label:

```tsx
                  : tab === 'provider-perf'
                    ? 'Provider Performance'
```

- [ ] **Step 2: Render the tab body**

Find the existing `{activeTab === 'analytics' && (` block (around line 1841). Add an analogous block for the new tab right above or below it:

```tsx
{activeTab === 'provider-perf' && (
  <ProviderPerformanceTab />
)}
```

Add the import at the top of the file alongside `import { AnalyticsTab } from './AnalyticsTab';`:

```tsx
import { ProviderPerformanceTab } from './ProviderPerformanceTab';
```

- [ ] **Step 3: Type-check + smoke-build the frontend**

```bash
cd frontend
pnpm run typecheck || npm run typecheck
pnpm run lint    || npm run lint
cd ..
```

Expected: no new errors.

- [ ] **Step 4: Manual smoke test (browser)**

```bash
# In one shell:
make dev-frontend  # or the project's standard frontend dev command (check Makefile)
# In another:
make dev-backend   # or equivalent
```

Then open the admin dashboard, log in as `admin@admin.com / admin`, click the new "Provider Performance" tab, choose a provider+model, and confirm the two charts render with data (or "Loading…" then a chart). If `provider_hourly_stats` is empty in your local dev DB, run a manual rollup once:

```bash
TEST_PG_DSN="$DB_DSN" uv run python -c "
import asyncio, asyncpg, os
from datetime import datetime, timezone, timedelta
from serving.admin.provider_stats_rollup import backfill_if_empty
async def main():
    pool = await asyncpg.create_pool(os.environ['DB_DSN'])
    await backfill_if_empty(pool, days=7)
    await pool.close()
asyncio.run(main())
"
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/dashboard/admin/page.tsx
git commit -m "$(cat <<'EOF'
feat(admin): wire Provider Performance tab into admin dashboard

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Lint, format, push, open PR

**Files:** none.

- [ ] **Step 1: Run ruff format check (per CLAUDE.md)**

```bash
cd /home/juncheng/hybridInference/.worktrees/provider-hourly-perf
uv run ruff format --check .
uv run ruff check .
```

Expected: both pass. If `ruff format --check` fails, run `uv run ruff format .`, review the diff, and amend or add a follow-up commit. If `ruff check .` flags issues, fix them in a new commit.

- [ ] **Step 2: Run the full integration + servers test suites for changed paths**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/freeinference_test_db" \
    uv run pytest test/integration/test_provider_stats_rollup.py test/servers/test_admin_provider_stats.py -v
```

Expected: all green.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin jason/claude/provider-hourly-perf
```

- [ ] **Step 4: Open the PR against `dev`**

```bash
gh pr create --base dev --title "feat(admin): per-provider hourly performance tracking" --body "$(cat <<'EOF'
## Summary
- Adds `provider_hourly_stats` table (PRIMARY KEY `(provider, model_id, hour_bucket)`) populated hourly from `api_logs` via APScheduler `CronTrigger(minute=5)`.
- Multi-replica safe via `pg_try_advisory_lock`; idempotent `INSERT ... ON CONFLICT DO UPDATE`.
- One-shot 30-day backfill on first deploy (when the table is empty).
- New admin endpoint `GET /admin/api/provider-stats?provider=&model_id=&from=&to=` (range capped at 90 days).
- New "Provider Performance" admin tab: TTFT p50/p95/p99 and throughput avg/p50/p95 line charts, KPI strip (requests, error rate, completion tokens).
- Prometheus metrics `provider_stats_rollup_runs_total{outcome}`, `provider_stats_rollup_duration_seconds`, `provider_stats_last_success_unixtime`, plus a stale-rollup alert rule.

## Spec / Plan
- Spec: `docs/superpowers/specs/2026-05-02-per-provider-hourly-performance-design.md`
- Plan: `docs/superpowers/plans/2026-05-02-per-provider-hourly-performance.md`

## Test plan
- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run pytest test/integration/test_provider_stats_rollup.py -v`
- [ ] `uv run pytest test/servers/test_admin_provider_stats.py -v`
- [ ] Manual: open the new "Provider Performance" admin tab on staging, choose a provider+model, confirm both charts render and the KPI strip shows real values.
- [ ] Manual: query Prometheus at `/metrics` for `provider_stats_rollup_runs_total` and `provider_stats_last_success_unixtime` after the first hour ticks.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: 8-minute CI watch (per CLAUDE.md)**

After PR creation, set a reminder to revisit in 8 minutes. Then:

```bash
gh pr view --web   # optional
gh pr checks       # check CI status
gh api repos/:owner/:repo/pulls/$(gh pr view --json number -q .number)/comments
```

Address any CI failures or review comments by adding new commits to the same branch.

- [ ] **Step 6: After merge, delete branch and worktree (per CLAUDE.md)**

```bash
cd /home/juncheng/hybridInference
git worktree remove .worktrees/provider-hourly-perf
git branch -D jason/claude/provider-hourly-perf
git push origin --delete jason/claude/provider-hourly-perf
```
