# Provider Token Usage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new admin "Token Usage" tab that shows per-provider, per-model totals of input / output / cached / reasoning tokens and cost over a selectable hourly window (1h | 24h | 7d | 30d).

**Architecture:** Extend the existing `provider_hourly_stats` table (added in #268) with four nullable BIGINT/DECIMAL totals. The existing hourly rollup job at `:05` UTC populates them via an updated `ROLLUP_SQL`. A new `backfill_token_columns` helper re-runs the rollup hour-by-hour on first deploy to populate prior 30 days. A new admin endpoint `GET /admin/api/provider-token-usage?range=…` aggregates over the chosen window and returns rows + totals to a new React tab.

**Tech Stack:** Python 3.11 + asyncpg + APScheduler (backend), FastAPI + Pydantic (admin API), Next.js 14 + React 18 + TypeScript (frontend), pytest + pytest-asyncio (tests).

**Spec:** [docs/agents/specs/2026-05-02-provider-token-usage-design.md](2026-05-02-provider-token-usage-design.md)

---

## File Structure

| Path | Responsibility |
|------|---------------|
| [serving/storage/database.py](serving/storage/database.py) | Schema: `ALTER TABLE provider_hourly_stats ADD COLUMN ...` for the 4 new totals (additive, idempotent). |
| [serving/admin/provider_stats_rollup.py](serving/admin/provider_stats_rollup.py) | Extend `ROLLUP_SQL` with the 4 new totals; add `backfill_token_columns(pool, days=30)`. |
| [serving/servers/bootstrap.py](serving/servers/bootstrap.py) | Extend the existing `_run_backfill` helper to also call `backfill_token_columns` after `backfill_if_empty`. |
| [serving/schemas_admin.py](serving/schemas_admin.py) | New Pydantic models: `ProviderTokenUsageRow`, `ProviderTokenUsageTotals`, `ProviderTokenUsageResponse`. |
| [serving/servers/routers/admin.py](serving/servers/routers/admin.py) | New route `GET /admin/api/provider-token-usage?range=…` (uses existing `verify_admin_access`, `get_db_logger`). |
| [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py) | Extend existing integration suite: schema check for new columns, rollup populates totals, backfill helper. |
| `test/servers/test_admin_token_usage.py` | New: route tests (auth, range validation, happy path). Mirrors `test_admin_provider_stats.py`. |
| [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts) | New TypeScript types + `getProviderTokenUsage(range)` client. |
| `frontend/src/app/dashboard/admin/TokenUsageTab.tsx` | New React component (range selector + KPI strip + grouped per-provider tables). |
| [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) | Add `'token-usage'` tab key, button, and render line. |

---

## Task 1: Add 4 nullable columns to `provider_hourly_stats`

**Files:**
- Modify: [serving/storage/database.py](serving/storage/database.py)
- Test: [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py)

- [ ] **Step 1: Write the failing test**

Append to [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py):

```python
@pytest.mark.asyncio
async def test_provider_hourly_stats_has_token_total_columns(db_logger: DatabaseLogger):
    """The 4 new BIGINT/DECIMAL totals exist and are nullable."""
    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'provider_hourly_stats'
              AND column_name IN (
                  'total_prompt_tokens',
                  'total_cache_read_tokens',
                  'total_reasoning_tokens',
                  'total_cost_usd'
              )
            """
        )
        by_name = {r["column_name"]: r for r in rows}

    assert set(by_name) == {
        "total_prompt_tokens",
        "total_cache_read_tokens",
        "total_reasoning_tokens",
        "total_cost_usd",
    }
    for name in ("total_prompt_tokens", "total_cache_read_tokens", "total_reasoning_tokens"):
        assert by_name[name]["data_type"] == "bigint", name
        assert by_name[name]["is_nullable"] == "YES", name
    assert by_name["total_cost_usd"]["data_type"] == "numeric"
    assert by_name["total_cost_usd"]["is_nullable"] == "YES"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py::test_provider_hourly_stats_has_token_total_columns -v
```

Expected: FAIL with `assert set(by_name) == {...}` — columns do not exist yet.

- [ ] **Step 3: Add the ALTER TABLE statements in database initialization**

In [serving/storage/database.py](serving/storage/database.py), find the `provider_hourly_stats` `CREATE TABLE IF NOT EXISTS` block (around line 735). Immediately AFTER its `CREATE INDEX IF NOT EXISTS idx_phs_provider_hour` block (around line 772), insert:

```python
            # Migration: token totals + cost on provider_hourly_stats.
            # Added by per-provider Token Usage tab. Nullable so the
            # change is metadata-only on existing tables; rows pre-dating
            # this migration are filled by backfill_token_columns at
            # startup and by subsequent hourly rollups.
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_prompt_tokens BIGINT
            """)
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_cache_read_tokens BIGINT
            """)
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_reasoning_tokens BIGINT
            """)
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_cost_usd DECIMAL(14, 8)
            """)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py::test_provider_hourly_stats_has_token_total_columns -v
```

Expected: PASS.

- [ ] **Step 5: Run the full rollup integration suite to confirm no regression**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py -v
```

Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add serving/storage/database.py test/integration/test_provider_stats_rollup.py
git commit -m "feat(stats): add token-total columns to provider_hourly_stats

Adds nullable total_prompt_tokens / total_cache_read_tokens /
total_reasoning_tokens / total_cost_usd to provider_hourly_stats so
the upcoming Token Usage admin tab can read pre-aggregated values.
ALTER TABLE is metadata-only (nullable, no default rewrite)."
```

---

## Task 2: Extend `ROLLUP_SQL` to compute the 4 new totals

**Files:**
- Modify: [serving/admin/provider_stats_rollup.py](serving/admin/provider_stats_rollup.py)
- Test: [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py)

The change to `ROLLUP_SQL` is purely additive — new columns in the INSERT list, new SUM expressions in the outer SELECT, three new columns added to the inner-subquery SELECT (`cache_read_tokens`, `reasoning_tokens`, `cost_usd`), and matching `EXCLUDED.col = …` lines in the `ON CONFLICT DO UPDATE`. Token totals **include errored requests** because we still attempted to send those tokens upstream.

- [ ] **Step 1: Update `_insert_api_log` helper to accept the new columns**

In [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py), find `_insert_api_log` (around line 107). Replace the entire function with:

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
    cache_read_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cost_usd: float | None = None,
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
                cache_read_tokens, reasoning_tokens, cost_usd,
                status_code, error
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            request_id,
            model_id,
            provider,
            timestamp,
            stream,
            ttft_ms,
            latency_ms,
            prompt_tokens,
            completion_tokens,
            cache_read_tokens,
            reasoning_tokens,
            cost_usd,
            status_code,
            error,
        )
```

- [ ] **Step 2: Write the failing test**

Append to [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py):

```python
@pytest.mark.asyncio
async def test_run_rollup_populates_token_totals(db_logger: DatabaseLogger):
    """Rollup sums prompt/cache_read/reasoning/cost across the hour, including errors."""
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc)
    # Two successes
    await _insert_api_log(
        pool,
        request_id="tok-1",
        provider="anthropic",
        model_id="claude-opus-4-7",
        timestamp=hour + timedelta(minutes=5),
        stream=True,
        ttft_ms=300,
        latency_ms=2300,
        completion_tokens=120,
        prompt_tokens=800,
        cache_read_tokens=500,
        reasoning_tokens=40,
        cost_usd=0.01230000,
    )
    await _insert_api_log(
        pool,
        request_id="tok-2",
        provider="anthropic",
        model_id="claude-opus-4-7",
        timestamp=hour + timedelta(minutes=10),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        completion_tokens=240,
        prompt_tokens=1200,
        cache_read_tokens=None,  # NULL → COALESCE'd to 0
        reasoning_tokens=60,
        cost_usd=0.02500000,
    )
    # One error — still counts toward token + cost totals (we paid to send)
    await _insert_api_log(
        pool,
        request_id="tok-err",
        provider="anthropic",
        model_id="claude-opus-4-7",
        timestamp=hour + timedelta(minutes=15),
        stream=True,
        ttft_ms=None,
        latency_ms=500,
        completion_tokens=None,
        prompt_tokens=300,
        cache_read_tokens=200,
        reasoning_tokens=None,
        cost_usd=0.00100000,
        status_code=500,
        error="upstream_5xx",
    )

    written = await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    assert written == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT total_prompt_tokens, total_cache_read_tokens,
                   total_reasoning_tokens, total_cost_usd
            FROM provider_hourly_stats
            WHERE provider = 'anthropic' AND model_id = 'claude-opus-4-7'
            """
        )

    assert row is not None
    assert row["total_prompt_tokens"] == 800 + 1200 + 300
    # NULL cache_read on tok-2 -> 0; tok-1 contributes 500, tok-err 200
    assert row["total_cache_read_tokens"] == 500 + 200
    # NULL reasoning on tok-err -> 0
    assert row["total_reasoning_tokens"] == 40 + 60
    # Decimal sum
    assert float(row["total_cost_usd"]) == pytest.approx(0.0123 + 0.025 + 0.001, abs=1e-9)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py::test_run_rollup_populates_token_totals -v
```

Expected: FAIL with `KeyError: 'total_prompt_tokens'` or `assert None == 2300` — `ROLLUP_SQL` does not yet write these columns.

- [ ] **Step 4: Update `ROLLUP_SQL` in `provider_stats_rollup.py`**

In [serving/admin/provider_stats_rollup.py](serving/admin/provider_stats_rollup.py), replace the `ROLLUP_SQL` constant (lines 22–104) with:

```python
ROLLUP_SQL = """
INSERT INTO provider_hourly_stats AS p (
    hour_bucket, provider, model_id,
    request_count, error_count, stream_count,
    ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
    latency_p50_ms, latency_p95_ms, latency_p99_ms,
    throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
    prompt_tokens_avg, completion_tokens_avg, total_completion_tokens,
    total_prompt_tokens, total_cache_read_tokens,
    total_reasoning_tokens, total_cost_usd
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
    COALESCE(SUM(completion_tokens), 0)::BIGINT                             AS total_completion_tokens,

    COALESCE(SUM(prompt_tokens), 0)::BIGINT                                 AS total_prompt_tokens,
    COALESCE(SUM(cache_read_tokens), 0)::BIGINT                             AS total_cache_read_tokens,
    COALESCE(SUM(reasoning_tokens), 0)::BIGINT                              AS total_reasoning_tokens,
    COALESCE(SUM(cost_usd), 0)::DECIMAL(14, 8)                              AS total_cost_usd
FROM (
    SELECT
        timestamp, provider, model_id, status_code, error,
        stream, ttft_ms, latency_ms, prompt_tokens, completion_tokens,
        cache_read_tokens, reasoning_tokens, cost_usd,
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
    total_completion_tokens = EXCLUDED.total_completion_tokens,
    total_prompt_tokens     = EXCLUDED.total_prompt_tokens,
    total_cache_read_tokens = EXCLUDED.total_cache_read_tokens,
    total_reasoning_tokens  = EXCLUDED.total_reasoning_tokens,
    total_cost_usd          = EXCLUDED.total_cost_usd
"""
```

- [ ] **Step 5: Run test to verify it passes**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py::test_run_rollup_populates_token_totals -v
```

Expected: PASS.

- [ ] **Step 6: Run the full rollup suite to confirm no regression on existing tests**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py -v
```

Expected: ALL PASS (including pre-existing `test_run_rollup_aggregates_one_hour`, `test_run_rollup_is_idempotent`, etc.).

- [ ] **Step 7: Commit**

```bash
git add serving/admin/provider_stats_rollup.py test/integration/test_provider_stats_rollup.py
git commit -m "feat(stats): roll up token totals + cost into provider_hourly_stats

Extends ROLLUP_SQL to SUM(prompt_tokens), SUM(cache_read_tokens),
SUM(reasoning_tokens), and SUM(cost_usd) per (provider, model_id, hour).
Errored requests are included in the totals — we still attempted (and
in many cases paid for) those tokens. UPSERT updates the new columns."
```

---

## Task 3: Add `backfill_token_columns` helper

**Files:**
- Modify: [serving/admin/provider_stats_rollup.py](serving/admin/provider_stats_rollup.py)
- Test: [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py)

The helper detects rows pre-dating the new columns (where `total_prompt_tokens IS NULL`) and re-runs the existing rollup hour-by-hour for the last `days` days. Idempotent — the `ON CONFLICT DO UPDATE` from Task 2 fills the new columns on every existing row.

- [ ] **Step 1: Write the failing test**

Append to [test/integration/test_provider_stats_rollup.py](test/integration/test_provider_stats_rollup.py):

```python
@pytest.mark.asyncio
async def test_backfill_token_columns_fills_null_rows(db_logger: DatabaseLogger):
    """Helper detects NULL token totals and re-runs rollup hour-by-hour."""
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    h = now - timedelta(hours=2)

    # Seed an api_logs row that the backfill should aggregate
    await _insert_api_log(
        pool,
        request_id="bf-tok-1",
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        timestamp=h + timedelta(minutes=5),
        stream=True,
        ttft_ms=300,
        latency_ms=3300,
        completion_tokens=200,
        prompt_tokens=900,
        cache_read_tokens=400,
        reasoning_tokens=50,
        cost_usd=0.0150000,
    )

    # Insert a stats row with the legacy schema (NULL token totals).
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens
            ) VALUES ($1, 'openrouter', 'qwen/qwen3-coder', 1, 0, 1, 200)
            """,
            h,
        )

    processed = await backfill_token_columns(pool, days=1)
    assert processed > 0

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT total_prompt_tokens, total_cache_read_tokens,
                   total_reasoning_tokens, total_cost_usd
            FROM provider_hourly_stats
            WHERE hour_bucket = $1
              AND provider = 'openrouter' AND model_id = 'qwen/qwen3-coder'
            """,
            h,
        )
    assert row is not None
    assert row["total_prompt_tokens"] == 900
    assert row["total_cache_read_tokens"] == 400
    assert row["total_reasoning_tokens"] == 50
    assert float(row["total_cost_usd"]) == pytest.approx(0.015, abs=1e-9)


@pytest.mark.asyncio
async def test_backfill_token_columns_no_op_when_already_populated(
    db_logger: DatabaseLogger,
):
    """Returns 0 without iterating when no NULL rows exist."""
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens, total_prompt_tokens
            ) VALUES ($1, 'p', 'm', 1, 0, 1, 100, 100)
            """,
            now - timedelta(hours=1),
        )

    processed = await backfill_token_columns(pool, days=1)
    assert processed == 0


@pytest.mark.asyncio
async def test_backfill_token_columns_is_idempotent(db_logger: DatabaseLogger):
    """Running twice does not change values."""
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    h = now - timedelta(hours=2)

    await _insert_api_log(
        pool,
        request_id="bf-idem-1",
        provider="zai",
        model_id="zai/glm-4.6",
        timestamp=h + timedelta(minutes=5),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        completion_tokens=180,
        prompt_tokens=700,
        cache_read_tokens=0,
        reasoning_tokens=0,
        cost_usd=0.005,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens
            ) VALUES ($1, 'zai', 'zai/glm-4.6', 1, 0, 0, 180)
            """,
            h,
        )

    await backfill_token_columns(pool, days=1)
    await backfill_token_columns(pool, days=1)  # second pass is a no-op (no NULL rows)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_prompt_tokens FROM provider_hourly_stats WHERE provider='zai'"
        )
    assert row["total_prompt_tokens"] == 700
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py::test_backfill_token_columns_fills_null_rows \
                test/integration/test_provider_stats_rollup.py::test_backfill_token_columns_no_op_when_already_populated \
                test/integration/test_provider_stats_rollup.py::test_backfill_token_columns_is_idempotent -v
```

Expected: FAIL with `ImportError: cannot import name 'backfill_token_columns'`.

- [ ] **Step 3: Add the helper to `provider_stats_rollup.py`**

In [serving/admin/provider_stats_rollup.py](serving/admin/provider_stats_rollup.py), append AFTER the existing `backfill_if_empty` function (after line 263):

```python
async def backfill_token_columns(
    pool: asyncpg.Pool,
    *,
    days: int = 30,
) -> int:
    """Re-run the rollup hour-by-hour to fill NULL token-total columns.

    Used once after the migration that adds total_prompt_tokens /
    total_cache_read_tokens / total_reasoning_tokens / total_cost_usd
    to provider_hourly_stats. No-op when no NULL rows are detected.

    Returns the number of hour buckets processed (0 when skipped).
    Idempotent: the same UPSERT runs as the hourly job, so calling
    repeatedly is safe.
    """
    async with pool.acquire() as conn:
        any_null = await conn.fetchval(
            "SELECT 1 FROM provider_hourly_stats WHERE total_prompt_tokens IS NULL LIMIT 1"
        )
    if any_null is None:
        logger.info("backfill_token_columns: no NULL rows, skipping")
        return 0

    processed = 0

    async def _do(conn) -> None:
        nonlocal processed
        any_null = await conn.fetchval(
            "SELECT 1 FROM provider_hourly_stats WHERE total_prompt_tokens IS NULL LIMIT 1"
        )
        if any_null is None:
            logger.info("backfill_token_columns: filled by another replica, skipping")
            return

        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        h = start
        while h < end:
            next_h = h + timedelta(hours=1)
            await conn.execute(ROLLUP_SQL, h, next_h)
            processed += 1
            h = next_h
        logger.info(
            "backfill_token_columns: window=[%s, %s) hours=%d",
            start.isoformat(),
            end.isoformat(),
            processed,
        )

    ran = await _try_lock_run(pool, _do)
    if not ran:
        logger.info("backfill_token_columns: lock held by another replica, skipping")
    return processed
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py::test_backfill_token_columns_fills_null_rows \
                test/integration/test_provider_stats_rollup.py::test_backfill_token_columns_no_op_when_already_populated \
                test/integration/test_provider_stats_rollup.py::test_backfill_token_columns_is_idempotent -v
```

Expected: PASS.

- [ ] **Step 5: Run the full rollup suite to confirm no regression**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py -v
```

Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add serving/admin/provider_stats_rollup.py test/integration/test_provider_stats_rollup.py
git commit -m "feat(stats): backfill_token_columns to seed pre-migration rows

Fills NULL token totals on provider_hourly_stats rows that pre-date
the new columns by re-running ROLLUP_SQL hour-by-hour for the last
\`days\` days. Multi-replica safe via the existing advisory lock.
Idempotent. Used by app startup; manual invocation safe too."
```

---

## Task 4: Wire `backfill_token_columns` into bootstrap

**Files:**
- Modify: [serving/servers/bootstrap.py](serving/servers/bootstrap.py)

The existing `_run_backfill` async helper already runs `backfill_if_empty` on startup as a fire-and-forget task. Extend it to also call `backfill_token_columns(pool, days=30)` afterwards.

- [ ] **Step 1: Find the existing `_run_backfill` block**

Open [serving/servers/bootstrap.py](serving/servers/bootstrap.py) and locate the `_run_backfill` async function nested inside the database initialization block (around line 305). The current shape is:

```python
                                from serving.admin.provider_stats_rollup import (
                                    backfill_if_empty,
                                    register_rollup_job,
                                )

                                sched = email_scheduler.get_scheduler()
                                if sched is not None:
                                    register_rollup_job(sched, db_logger.pool)

                                    async def _run_backfill(pool=db_logger.pool):
                                        try:
                                            await backfill_if_empty(pool, days=30)
                                        except Exception as bf_exc:
                                            logger.warning(
                                                f"provider-stats backfill failed (non-fatal): {bf_exc}"
                                            )

                                    asyncio.create_task(_run_backfill())
```

- [ ] **Step 2: Update the import and the helper body**

Edit [serving/servers/bootstrap.py](serving/servers/bootstrap.py): change the import to also pull `backfill_token_columns`, and extend `_run_backfill` to call it after `backfill_if_empty`.

Replace the import line:

```python
                            from serving.admin.provider_stats_rollup import (
                                backfill_if_empty,
                                register_rollup_job,
                            )
```

with:

```python
                            from serving.admin.provider_stats_rollup import (
                                backfill_if_empty,
                                backfill_token_columns,
                                register_rollup_job,
                            )
```

Replace the `_run_backfill` body (the `try` block) with:

```python
                                async def _run_backfill(pool=db_logger.pool):
                                    try:
                                        await backfill_if_empty(pool, days=30)
                                    except Exception as bf_exc:
                                        logger.warning(
                                            f"provider-stats backfill failed (non-fatal): {bf_exc}"
                                        )
                                    try:
                                        await backfill_token_columns(pool, days=30)
                                    except Exception as bf_exc:
                                        logger.warning(
                                            f"provider-stats token backfill failed (non-fatal): {bf_exc}"
                                        )
```

(Two separate `try/except` blocks so a failure in the empty-table backfill does not skip the token-column backfill, and vice versa.)

- [ ] **Step 3: Verify lint passes**

```bash
uv run ruff format --check serving/servers/bootstrap.py
uv run ruff check serving/servers/bootstrap.py
```

Expected: no errors.

- [ ] **Step 4: Verify the existing bootstrap test suite still passes**

```bash
uv run pytest test/servers/test_bootstrap.py -v
```

Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/bootstrap.py
git commit -m "feat(bootstrap): run backfill_token_columns at startup

Extends the existing fire-and-forget _run_backfill task to also fill
the new token-total columns on provider_hourly_stats after the empty-
table backfill completes. Non-fatal: failure is logged and ignored
so a slow backfill cannot trip readiness checks."
```

---

## Task 5: Add Pydantic schemas for the new endpoint

**Files:**
- Modify: [serving/schemas_admin.py](serving/schemas_admin.py)

- [ ] **Step 1: Append the new models**

Open [serving/schemas_admin.py](serving/schemas_admin.py). After the `ProviderStatsResponse` class (which currently ends around line 682), append:

```python
# ============================================================
# Provider Token Usage (per-provider, per-model token totals over a
# selectable hourly window). Powers the admin "Token Usage" tab.
# ============================================================


class ProviderTokenUsageRow(BaseModel):  # type: ignore[no-any-unimported]
    provider: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    request_count: int


class ProviderTokenUsageTotals(BaseModel):  # type: ignore[no-any-unimported]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    request_count: int


class ProviderTokenUsageWindow(BaseModel):  # type: ignore[no-any-unimported]
    from_: datetime = Field(alias="from")
    to: datetime

    model_config = {"populate_by_name": True}


class ProviderTokenUsageResponse(BaseModel):  # type: ignore[no-any-unimported]
    range: Literal["1h", "24h", "7d", "30d"]
    window: ProviderTokenUsageWindow
    refreshed_at: datetime
    rows: list[ProviderTokenUsageRow]
    totals: ProviderTokenUsageTotals
```

If `Literal` and `Field` are not yet imported in this file, add them. Check the existing imports at the top of [serving/schemas_admin.py](serving/schemas_admin.py); typical Pydantic imports look like:

```python
from typing import Literal
from pydantic import BaseModel, Field
```

If `datetime` is not imported, add `from datetime import datetime`.

- [ ] **Step 2: Verify lint and that the file imports cleanly**

```bash
uv run ruff format --check serving/schemas_admin.py
uv run ruff check serving/schemas_admin.py
uv run python -c "from serving.schemas_admin import ProviderTokenUsageResponse, ProviderTokenUsageRow, ProviderTokenUsageTotals; print('ok')"
```

Expected: `ok` printed; no lint errors.

- [ ] **Step 3: Commit**

```bash
git add serving/schemas_admin.py
git commit -m "feat(schemas): add ProviderTokenUsageResponse models"
```

---

## Task 6: Add the admin endpoint `GET /admin/api/provider-token-usage`

**Files:**
- Modify: [serving/servers/routers/admin.py](serving/servers/routers/admin.py)

- [ ] **Step 1: Update the imports**

In [serving/servers/routers/admin.py](serving/servers/routers/admin.py), find the `from serving.schemas_admin import (` block at line 17 and add the three new symbols. The new entries are:

```python
    ProviderTokenUsageResponse,
    ProviderTokenUsageRow,
    ProviderTokenUsageTotals,
    ProviderTokenUsageWindow,
```

(Place them in alphabetical order in the existing import block.)

- [ ] **Step 2: Append the new endpoint at the end of the file**

Append AFTER the existing `admin_provider_stats` function (after line 2347). The constants and helpers `_truncate_hour`, `_require_aware_utc`, `verify_admin_access`, `get_db_logger` are already defined and reused.

```python
# ============================================================
# Token Usage tab — per (provider, model_id) totals over a fixed-window
# selector (1h | 24h | 7d | 30d). Reads pre-aggregated rows from
# provider_hourly_stats; no scan of api_logs.
# ============================================================

_TOKEN_USAGE_RANGES: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@router.get(
    "/admin/api/provider-token-usage", response_model=ProviderTokenUsageResponse
)
async def admin_provider_token_usage(
    request: Request,
    range: Literal["1h", "24h", "7d", "30d"] = "24h",
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ProviderTokenUsageResponse:
    """Per-(provider, model_id) token totals + cost over a fixed window.

    Query parameters:
        range: one of "1h", "24h", "7d", "30d". Defaults to "24h".

    The window is hour-truncated; `from = floor(now, hour) - <range>`,
    `to = floor(now, hour)`. Rows are sorted by total token sum
    (input + output + cached + reasoning) descending. Totals are
    summed in Python from the same rows to avoid a second DB hit.
    """
    del request
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=503, detail="database unavailable")

    delta = _TOKEN_USAGE_RANGES[range]
    end = _truncate_hour(datetime.now(timezone.utc))
    start = end - delta

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                provider,
                model_id,
                COALESCE(SUM(total_prompt_tokens), 0)::BIGINT      AS input_tokens,
                COALESCE(SUM(total_completion_tokens), 0)::BIGINT  AS output_tokens,
                COALESCE(SUM(total_cache_read_tokens), 0)::BIGINT  AS cached_tokens,
                COALESCE(SUM(total_reasoning_tokens), 0)::BIGINT   AS reasoning_tokens,
                COALESCE(SUM(total_cost_usd), 0)::FLOAT            AS cost_usd,
                COALESCE(SUM(request_count), 0)::BIGINT            AS request_count
            FROM provider_hourly_stats
            WHERE hour_bucket >= $1 AND hour_bucket < $2
            GROUP BY provider, model_id
            ORDER BY (
                  COALESCE(SUM(total_prompt_tokens), 0)
                + COALESCE(SUM(total_completion_tokens), 0)
                + COALESCE(SUM(total_cache_read_tokens), 0)
                + COALESCE(SUM(total_reasoning_tokens), 0)
            ) DESC
            """,
            start,
            end,
        )

    out_rows = [ProviderTokenUsageRow(**dict(r)) for r in rows]
    totals = ProviderTokenUsageTotals(
        input_tokens=sum(r.input_tokens for r in out_rows),
        output_tokens=sum(r.output_tokens for r in out_rows),
        cached_tokens=sum(r.cached_tokens for r in out_rows),
        reasoning_tokens=sum(r.reasoning_tokens for r in out_rows),
        cost_usd=sum(r.cost_usd for r in out_rows),
        request_count=sum(r.request_count for r in out_rows),
    )

    return ProviderTokenUsageResponse(
        range=range,
        window=ProviderTokenUsageWindow.model_validate({"from": start, "to": end}),
        refreshed_at=end,
        rows=out_rows,
        totals=totals,
    )
```

- [ ] **Step 3: Lint the file**

```bash
uv run ruff format --check serving/servers/routers/admin.py
uv run ruff check serving/servers/routers/admin.py
```

Expected: no errors.

- [ ] **Step 4: Quick smoke import check**

```bash
uv run python -c "from serving.servers.routers.admin import admin_provider_token_usage; print('ok')"
```

Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/routers/admin.py
git commit -m "feat(admin): GET /admin/api/provider-token-usage endpoint

Returns per-(provider, model_id) totals of input/output/cached/
reasoning tokens, cost, and request count over a fixed window
(1h | 24h | 7d | 30d). Reads pre-aggregated rows from
provider_hourly_stats; sums totals in Python."
```

---

## Task 7: Add API tests for the new endpoint

**Files:**
- Create: `test/servers/test_admin_token_usage.py`

Mirror the structure of [test/servers/test_admin_provider_stats.py](test/servers/test_admin_provider_stats.py) — auth tests with mocked DB, then a real-Postgres happy-path test gated on `TEST_PG_DSN`.

- [ ] **Step 1: Create the test file**

Write the file:

```python
"""Tests for the admin /admin/api/provider-token-usage route."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import (
    AppServices,
    get_db_logger,
    verify_admin_access,
)
from serving.servers.routers import admin as admin_router


def _build_admin_app(db_logger=None) -> FastAPI:
    app = FastAPI(title="Admin Token Usage Test")
    services = AppServices(
        router=MagicMock(),
        db_logger=db_logger,
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin_router.router)
    return app


def _override_admin(app: FastAPI) -> None:
    async def _fake_admin() -> str:
        return "admin@test"

    app.dependency_overrides[verify_admin_access] = _fake_admin


# ---------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------


class TestTokenUsageAuth:
    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self):
        app = _build_admin_app(db_logger=None)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/api/provider-token-usage")
        assert resp.status_code == 401


class TestTokenUsageValidation:
    @pytest.mark.asyncio
    async def test_invalid_range_rejected(self):
        fake_db_logger = MagicMock()
        fake_db_logger.pool = MagicMock()

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/api/provider-token-usage",
                params={"range": "5m"},
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_db_returns_503(self):
        fake_db_logger = MagicMock()
        fake_db_logger.pool = None

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/api/provider-token-usage")
        app.dependency_overrides.clear()
        assert resp.status_code == 503


# ---------------------------------------------------------------------
# Real Postgres happy-path tests (TEST_PG_DSN required)
# ---------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str):
    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    async with logger.pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE provider_hourly_stats")
    try:
        yield logger
    finally:
        assert logger.pool is not None
        async with logger.pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE provider_hourly_stats")
        await logger.cleanup()


async def _seed(
    pool,
    *,
    provider: str,
    model_id: str,
    bucket: datetime,
    input_tokens: int,
    output_tokens: int,
    cached: int,
    reasoning: int,
    cost: float,
    requests: int = 1,
):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens,
                total_prompt_tokens, total_cache_read_tokens,
                total_reasoning_tokens, total_cost_usd
            )
            VALUES ($1, $2, $3, $4, 0, 0, $5, $6, $7, $8, $9)
            ON CONFLICT (provider, model_id, hour_bucket) DO UPDATE SET
                total_prompt_tokens     = EXCLUDED.total_prompt_tokens,
                total_completion_tokens = EXCLUDED.total_completion_tokens,
                total_cache_read_tokens = EXCLUDED.total_cache_read_tokens,
                total_reasoning_tokens  = EXCLUDED.total_reasoning_tokens,
                total_cost_usd          = EXCLUDED.total_cost_usd,
                request_count           = EXCLUDED.request_count
            """,
            bucket,
            provider,
            model_id,
            requests,
            output_tokens,
            input_tokens,
            cached,
            reasoning,
            cost,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_24h_happy_path(db_logger):
    """One row per (provider, model_id), sorted DESC by total tokens, totals correct."""
    pool = db_logger.pool
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    bucket = now - timedelta(hours=2)

    # Heavy provider/model
    await _seed(
        pool,
        provider="anthropic",
        model_id="claude-opus-4-7",
        bucket=bucket,
        input_tokens=1_000_000,
        output_tokens=200_000,
        cached=80_000,
        reasoning=10_000,
        cost=12.345,
        requests=400,
    )
    # Lighter provider/model
    await _seed(
        pool,
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        bucket=bucket,
        input_tokens=100_000,
        output_tokens=20_000,
        cached=5_000,
        reasoning=0,
        cost=0.50,
        requests=50,
    )

    app = _build_admin_app(db_logger=db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-token-usage",
            params={"range": "24h"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["range"] == "24h"
    assert len(body["rows"]) == 2
    # Sorted DESC: anthropic first (heavier total)
    assert body["rows"][0]["provider"] == "anthropic"
    assert body["rows"][0]["input_tokens"] == 1_000_000
    assert body["rows"][0]["output_tokens"] == 200_000
    assert body["rows"][0]["cached_tokens"] == 80_000
    assert body["rows"][0]["reasoning_tokens"] == 10_000
    assert body["rows"][0]["request_count"] == 400
    assert body["rows"][0]["cost_usd"] == pytest.approx(12.345, abs=1e-6)

    assert body["rows"][1]["provider"] == "openrouter"

    # Totals = sum of rows
    assert body["totals"]["input_tokens"] == 1_100_000
    assert body["totals"]["output_tokens"] == 220_000
    assert body["totals"]["cached_tokens"] == 85_000
    assert body["totals"]["reasoning_tokens"] == 10_000
    assert body["totals"]["request_count"] == 450
    assert body["totals"]["cost_usd"] == pytest.approx(12.845, abs=1e-6)

    # refreshed_at is hour-truncated (no subhour, no minutes/seconds)
    refreshed = datetime.fromisoformat(body["refreshed_at"].replace("Z", "+00:00"))
    assert refreshed.minute == 0 and refreshed.second == 0 and refreshed.microsecond == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_empty_window_returns_zero_totals(db_logger):
    """No rows in window -> 200 with rows=[] and zero totals."""
    pool = db_logger.pool

    # Insert a row 60 days ago — falls outside any selectable range.
    old = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=60)
    await _seed(
        pool,
        provider="zai",
        model_id="zai/glm-4.6",
        bucket=old,
        input_tokens=10,
        output_tokens=10,
        cached=0,
        reasoning=0,
        cost=0.001,
    )

    app = _build_admin_app(db_logger=db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-token-usage",
            params={"range": "30d"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0,
        "request_count": 0,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_window_inclusion(db_logger):
    """Rows inside [from, to) included; rows outside excluded."""
    pool = db_logger.pool
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # 2h ago: inside the 24h window
    await _seed(
        pool,
        provider="p1",
        model_id="m1",
        bucket=now - timedelta(hours=2),
        input_tokens=100,
        output_tokens=10,
        cached=0,
        reasoning=0,
        cost=0.001,
    )
    # 25h ago: outside the 24h window
    await _seed(
        pool,
        provider="p2",
        model_id="m2",
        bucket=now - timedelta(hours=25),
        input_tokens=999,
        output_tokens=999,
        cached=999,
        reasoning=999,
        cost=9.99,
    )

    app = _build_admin_app(db_logger=db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-token-usage",
            params={"range": "24h"},
        )
    app.dependency_overrides.clear()

    body = resp.json()
    providers = [r["provider"] for r in body["rows"]]
    assert providers == ["p1"]
    assert body["totals"]["input_tokens"] == 100
```

- [ ] **Step 2: Run the auth + validation tests (mocked DB; do not require Postgres)**

```bash
uv run pytest test/servers/test_admin_token_usage.py::TestTokenUsageAuth \
              test/servers/test_admin_token_usage.py::TestTokenUsageValidation -v
```

Expected: ALL PASS.

- [ ] **Step 3: Run the integration tests against Postgres**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/servers/test_admin_token_usage.py -v
```

Expected: ALL PASS (5 tests).

- [ ] **Step 4: Run lint**

```bash
uv run ruff format --check test/servers/test_admin_token_usage.py
uv run ruff check test/servers/test_admin_token_usage.py
```

Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add test/servers/test_admin_token_usage.py
git commit -m "test(admin): cover provider-token-usage route

Auth (401 without admin), validation (422 invalid range, 503 no db),
happy path (rows sorted DESC + totals match), empty window
(rows=[] and zero totals), window inclusion ([from, to))."
```

---

## Task 8: Add the frontend API client

**Files:**
- Modify: [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts)

- [ ] **Step 1: Append the types and client function**

Open [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts) and append AFTER the `getProviderStats` function (after line 684):

```typescript
// ========================================
// Provider Token Usage
// ========================================

export type TokenUsageRange = '1h' | '24h' | '7d' | '30d';

export interface ProviderTokenUsageRow {
  provider: string;
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
  request_count: number;
}

export interface ProviderTokenUsageTotals {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
  request_count: number;
}

export interface ProviderTokenUsageResponse {
  range: TokenUsageRange;
  window: { from: string; to: string };
  refreshed_at: string;
  rows: ProviderTokenUsageRow[];
  totals: ProviderTokenUsageTotals;
}

export async function getProviderTokenUsage(
  range: TokenUsageRange,
): Promise<ProviderTokenUsageResponse> {
  const search = new URLSearchParams({ range });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/api/provider-token-usage?${search.toString()}`,
  );
  return jsonOrThrow<ProviderTokenUsageResponse>(resp);
}
```

- [ ] **Step 2: Type-check the frontend**

```bash
cd frontend && npm run type-check
```

Expected: no errors. (If the script is named differently in `package.json`, e.g. `tsc`, run that instead. Look for `"type-check"` or `"tsc"` in `frontend/package.json`.)

- [ ] **Step 3: Lint the frontend**

```bash
cd frontend && npm run lint
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api/admin.ts
git commit -m "feat(frontend): admin client for provider-token-usage"
```

---

## Task 9: Build `TokenUsageTab.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/TokenUsageTab.tsx`

Component owns: range selector, data fetch, KPI strip, grouped per-provider tables. Uses the same Tailwind class conventions and `getErrorMessage` helper as the existing [ProviderPerformanceTab.tsx](frontend/src/app/dashboard/admin/ProviderPerformanceTab.tsx). No charts — tables only.

- [ ] **Step 1: Create the component**

Write `frontend/src/app/dashboard/admin/TokenUsageTab.tsx`:

```tsx
'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ProviderTokenUsageResponse,
  ProviderTokenUsageRow,
  TokenUsageRange,
  getProviderTokenUsage,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const RANGES: { key: TokenUsageRange; label: string }[] = [
  { key: '1h', label: 'Last 1h' },
  { key: '24h', label: 'Last 24h' },
  { key: '7d', label: 'Last 7d' },
  { key: '30d', label: 'Last 30d' },
];

const compact = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

function fmtCount(n: number): string {
  return n >= 10_000 ? compact.format(n) : n.toLocaleString();
}

function fmtCost(usd: number): string {
  if (usd === 0) return '$0';
  if (Math.abs(usd) < 1) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

function fmtUtcHour(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getUTCHours()).padStart(2, '0')}:00 UTC`;
}

function fmtUtcRange(fromIso: string, toIso: string): string {
  const from = new Date(fromIso);
  const to = new Date(toIso);
  const fmt = (d: Date) =>
    `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
  return `${fmt(from)}–${fmt(to)} UTC`;
}

function rowTotal(r: ProviderTokenUsageRow): number {
  return r.input_tokens + r.output_tokens + r.cached_tokens + r.reasoning_tokens;
}

function groupByProvider(
  rows: ProviderTokenUsageRow[],
): { provider: string; rows: ProviderTokenUsageRow[]; total: number }[] {
  const buckets = new Map<string, ProviderTokenUsageRow[]>();
  for (const r of rows) {
    const arr = buckets.get(r.provider) ?? [];
    arr.push(r);
    buckets.set(r.provider, arr);
  }
  const out: { provider: string; rows: ProviderTokenUsageRow[]; total: number }[] = [];
  for (const [provider, providerRows] of buckets) {
    const total = providerRows.reduce((acc, r) => acc + rowTotal(r), 0);
    out.push({ provider, rows: providerRows, total });
  }
  out.sort((a, b) => b.total - a.total);
  return out;
}

export function TokenUsageTab() {
  const [range, setRange] = useState<TokenUsageRange>('24h');
  const [data, setData] = useState<ProviderTokenUsageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await getProviderTokenUsage(range);
      setData(resp);
    } catch (exc) {
      setError(getErrorMessage(exc));
    } finally {
      setLoading(false);
    }
  }, [range]);

  useEffect(() => {
    void load();
  }, [load]);

  const groups = useMemo(() => groupByProvider(data?.rows ?? []), [data]);

  return (
    <div className="space-y-6 mt-6">
      <div className="flex flex-wrap items-end gap-4">
        <label className="text-sm">
          <span className="block text-gray-500 mb-1">Range</span>
          <select
            className="border rounded px-2 py-1"
            value={range}
            onChange={(e) => setRange(e.target.value as TokenUsageRange)}
          >
            {RANGES.map((r) => (
              <option key={r.key} value={r.key}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        {data ? (
          <div className="text-[12px] text-gray-500 pb-1">
            <div>
              Updated at {fmtUtcHour(data.refreshed_at)} (hourly refresh)
            </div>
            {range === '1h' ? (
              <div>showing hour {fmtUtcRange(data.window.from, data.window.to)}</div>
            ) : null}
          </div>
        ) : null}
      </div>

      {error ? <div className="text-red-600 text-sm">{error}</div> : null}
      {loading ? <div className="text-gray-500 text-sm">Loading…</div> : null}

      {data ? (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            <Kpi label="Input" value={fmtCount(data.totals.input_tokens)} />
            <Kpi label="Output" value={fmtCount(data.totals.output_tokens)} />
            <Kpi label="Cached" value={fmtCount(data.totals.cached_tokens)} />
            <Kpi label="Reasoning" value={fmtCount(data.totals.reasoning_tokens)} />
            <Kpi label="Requests" value={fmtCount(data.totals.request_count)} />
            <Kpi label="Cost USD" value={fmtCost(data.totals.cost_usd)} />
          </div>

          {groups.length === 0 ? (
            <div className="rounded-xl border p-6 text-center text-[13px] text-gray-400">
              No token usage recorded in this window.
            </div>
          ) : (
            <div className="space-y-4">
              {groups.map((g) => (
                <ProviderTable key={g.provider} provider={g.provider} rows={g.rows} />
              ))}
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border p-3">
      <p className="text-[11px] uppercase tracking-wide text-gray-400">{label}</p>
      <p className="mt-1 text-lg font-bold text-gray-900 tabular-nums">{value}</p>
    </div>
  );
}

function ProviderTable({
  provider,
  rows,
}: {
  provider: string;
  rows: ProviderTokenUsageRow[];
}) {
  return (
    <div className="rounded-xl border overflow-hidden">
      <div className="bg-gray-50 px-4 py-2 text-[13px] font-semibold text-gray-900">
        {provider}
      </div>
      <table className="w-full text-[12px]">
        <thead className="text-gray-500">
          <tr className="border-t">
            <th className="text-left px-4 py-2">Model</th>
            <th className="text-right px-3 py-2">Input</th>
            <th className="text-right px-3 py-2">Output</th>
            <th className="text-right px-3 py-2">Cached</th>
            <th className="text-right px-3 py-2">Reasoning</th>
            <th className="text-right px-3 py-2">Requests</th>
            <th className="text-right px-4 py-2">Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.model_id} className="border-t">
              <td className="px-4 py-2 text-gray-900">{r.model_id}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.input_tokens)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.output_tokens)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.cached_tokens)}</td>
              <td className="px-3 py-2 text-right tabular-nums">
                {fmtCount(r.reasoning_tokens)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCount(r.request_count)}</td>
              <td className="px-4 py-2 text-right tabular-nums">{fmtCost(r.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Type-check + lint**

```bash
cd frontend && npm run type-check && npm run lint
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/TokenUsageTab.tsx
git commit -m "feat(frontend): TokenUsageTab component

Range selector (1h | 24h | 7d | 30d), KPI strip (input / output /
cached / reasoning / requests / cost), and grouped per-provider
tables sorted by total token sum DESC. Reads /admin/api/provider-
token-usage."
```

---

## Task 10: Wire the new tab into `page.tsx`

**Files:**
- Modify: [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx)

Add `'token-usage'` to the `activeTab` union (3 places: state init, query-string parser, `onTabChange` parameter), to the tab-button array + label switch, and to the tab-render section.

- [ ] **Step 1: Add the import**

Near the top of [page.tsx](frontend/src/app/dashboard/admin/page.tsx) where `ProviderPerformanceTab` is imported (line 41), add:

```tsx
import { TokenUsageTab } from './TokenUsageTab';
```

- [ ] **Step 2: Extend the `activeTab` union (3 places)**

Replace the `useState` declaration (lines 425–434):

```tsx
  const [activeTab, setActiveTab] = useState<
    | 'users'
    | 'audit'
    | 'requests'
    | 'broadcast'
    | 'providers'
    | 'provider-perf'
    | 'analytics'
    | 'performance'
    | 'token-usage'
  >('users');
```

Replace the query-string parser (lines 436–460):

```tsx
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get('tab');
    if (
      tab === 'users' ||
      tab === 'audit' ||
      tab === 'requests' ||
      tab === 'broadcast' ||
      tab === 'providers' ||
      tab === 'provider-perf' ||
      tab === 'analytics' ||
      tab === 'performance' ||
      tab === 'token-usage'
    ) {
      setActiveTab(
        tab as
          | 'users'
          | 'audit'
          | 'requests'
          | 'broadcast'
          | 'providers'
          | 'provider-perf'
          | 'analytics'
          | 'performance'
          | 'token-usage',
      );
```

Replace the `onTabChange` declaration (lines 832–842):

```tsx
  const onTabChange = (
    tab:
      | 'users'
      | 'audit'
      | 'requests'
      | 'broadcast'
      | 'providers'
      | 'provider-perf'
      | 'analytics'
      | 'performance'
      | 'token-usage',
  ) => {
```

- [ ] **Step 3: Add `'token-usage'` to the tab-button array and label switch**

Replace the tab-button array literal at line 936–947:

```tsx
          {(
            [
              'users',
              'requests',
              'providers',
              'provider-perf',
              'token-usage',
              'audit',
              'broadcast',
              'analytics',
              'performance',
            ] as const
          ).map((tab) => (
```

Replace the label-switch chain at lines 957–971:

```tsx
              {tab === 'users'
                ? 'Users'
                : tab === 'requests'
                  ? 'Recent Requests'
                  : tab === 'providers'
                    ? 'Providers'
                    : tab === 'provider-perf'
                      ? 'Provider Performance'
                      : tab === 'token-usage'
                        ? 'Token Usage'
                        : tab === 'audit'
                          ? 'Audit Log'
                          : tab === 'broadcast'
                            ? 'Broadcast Email'
                            : tab === 'analytics'
                              ? 'Analytics'
                              : 'Performance'}
```

- [ ] **Step 4: Render the tab**

In the tab-render section near line 2048, immediately after `{activeTab === 'provider-perf' && <ProviderPerformanceTab />}`, add:

```tsx
        {activeTab === 'token-usage' && <TokenUsageTab />}
```

- [ ] **Step 5: Type-check + lint**

```bash
cd frontend && npm run type-check && npm run lint
```

Expected: clean.

- [ ] **Step 6: Manual smoke (locally if dev server is running, otherwise rely on staging deploy after merge)**

Visit `http://localhost:<port>/dashboard/admin?tab=token-usage` and confirm: the tab button "Token Usage" appears, clicking it switches to the new tab, the range selector defaults to 24h, the KPI strip and tables render. If no `provider_hourly_stats` rows exist locally for the window, the empty-state message appears.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/app/dashboard/admin/page.tsx
git commit -m "feat(admin): wire Token Usage tab into admin dashboard"
```

---

## Task 11: Run all checks before opening the PR

These mirror the CLAUDE.md gate ("always run lint and check before create PR").

- [ ] **Step 1: Backend lint + format**

```bash
uv run ruff format --check .
uv run ruff check .
```

Expected: clean.

- [ ] **Step 2: Backend tests (rollup + admin route)**

```bash
TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" \
  uv run pytest test/integration/test_provider_stats_rollup.py \
                test/servers/test_admin_token_usage.py \
                test/servers/test_admin_provider_stats.py \
                test/servers/test_bootstrap.py -v
```

Expected: ALL PASS.

- [ ] **Step 3: Frontend type-check + lint**

```bash
cd frontend && npm run type-check && npm run lint
```

Expected: clean.

- [ ] **Step 4: If any check fails, fix in place and re-run; do not proceed to PR until clean**

- [ ] **Step 5: Push the branch and open the PR to `dev`**

```bash
git push -u origin jason/claude/provider-token-usage
gh pr create --base dev --title "feat(admin): per-provider token usage tab" --body "$(cat <<'EOF'
## Summary
- Adds new admin tab "Token Usage" showing per-(provider, model) totals of input / output / cached_read / reasoning tokens, request count, and cost over a window selector (1h | 24h | 7d | 30d, default 24h).
- Extends `provider_hourly_stats` with four nullable totals; the existing hourly rollup now populates them.
- One-shot `backfill_token_columns` re-runs the rollup hour-by-hour at startup to fill rows pre-dating the migration. Multi-replica safe via the existing advisory lock; idempotent.
- New endpoint `GET /admin/api/provider-token-usage?range=…` returns aggregated rows + totals; sorts rows DESC by total token sum.

## Spec / Plan
- Spec: `docs/agents/specs/2026-05-02-provider-token-usage-design.md`
- Plan: `docs/agents/plans/2026-05-02-provider-token-usage.md`

## Test plan
- [x] `uv run ruff format --check .`
- [x] `uv run ruff check .`
- [x] `uv run pytest test/integration/test_provider_stats_rollup.py -v`
- [x] `uv run pytest test/servers/test_admin_token_usage.py -v`
- [x] Frontend `npm run type-check` and `npm run lint`
- [ ] Manual: open the new "Token Usage" admin tab on staging, verify each range option loads data, KPI strip and per-provider tables render, and the "Updated at HH:00 UTC" caveat is shown.
EOF
)"
```

- [ ] **Step 6: Capture the PR URL and report back**

The `gh pr create` command prints the PR URL on success. Report it to the user.

---

## Self-Review Checklist (run after writing the plan)

**Spec coverage** — every section in the spec maps to a task:

| Spec section | Tasks |
|---|---|
| Schema change (4 nullable cols) | Task 1 |
| Rollup query change (totals incl. errored) | Task 2 |
| Backfill helper | Task 3 |
| Backfill wiring at startup | Task 4 |
| Pydantic schemas | Task 5 |
| Admin API (`GET /admin/api/provider-token-usage`) | Task 6 |
| API tests (auth, validation, happy path, empty, window inclusion) | Task 7 |
| Frontend API client | Task 8 |
| `TokenUsageTab.tsx` (range selector, KPI strip, grouped tables, hourly-refresh label) | Task 9 |
| Tab wiring in `page.tsx` | Task 10 |
| Lint / tests / PR gate | Task 11 |

**Placeholder scan** — no "TBD", "TODO", "implement later", "add error handling", "similar to Task N", or empty steps. All code blocks are concrete.

**Type consistency** — names used across tasks match:
- Pydantic: `ProviderTokenUsageRow`, `ProviderTokenUsageTotals`, `ProviderTokenUsageWindow`, `ProviderTokenUsageResponse` (Task 5) → imported and used in Task 6 → asserted against the JSON in Task 7.
- Endpoint path: `/admin/api/provider-token-usage` consistent across Tasks 6, 7, 8.
- Range enum values: `'1h' | '24h' | '7d' | '30d'` consistent across Pydantic (Task 5), endpoint (Task 6), API tests (Task 7), TS types (Task 8), tab UI (Task 9).
- Helper function: `backfill_token_columns(pool, days=…)` — same signature in Task 3 (definition) and Task 4 (call site).
- New SQL columns: `total_prompt_tokens`, `total_cache_read_tokens`, `total_reasoning_tokens`, `total_cost_usd` — same names in Task 1 (schema), Task 2 (rollup), Task 3 (backfill), Task 6 (endpoint SELECT), Task 7 (test seed).
