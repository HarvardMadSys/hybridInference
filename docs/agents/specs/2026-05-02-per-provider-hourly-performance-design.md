# Per-Provider Hourly Performance Tracking

**Status:** Design
**Date:** 2026-05-02
**Author:** brainstorming session

## Summary

Track upstream provider performance — TTFT, throughput, latency, error rate — by aggregating `api_logs` into a new `provider_hourly_stats` rollup table once per hour, grouped by `(provider, model_id, hour_bucket)`. Surface the time series in a new "Provider Performance" tab on the admin dashboard so we can see when a provider degrades or a model gets slower over time.

## Goals

- Give admins a per-provider, per-model time-series view of TTFT (p50/p95/p99), end-to-end latency (p50/p95/p99), and decode throughput (avg/p50/p95).
- Run cheaply: one aggregation query per hour against an already-indexed table.
- Make charts load fast: dashboard reads pre-aggregated rows, not raw `api_logs`.
- Survive replica scale-out: only one replica computes each hour's rollup.
- Backfill on first deploy so charts have history immediately.

## Non-goals

- Synthetic probing of providers when no real traffic flows (the empty `llm-prober/` dir is out of scope here; could be a follow-up).
- Sub-hour granularity (minute/5-minute buckets).
- Cross-provider routing optimization (consumer of this data, not part of this spec).
- Cost tracking or quota tracking (covered elsewhere — see [admin-provider-quotas](2026-04-30-admin-provider-quotas-design.md)).
- Emailing/Slacking alerts when a provider degrades. Out of scope; consumed via Prometheus/alertmanager already.

## Context

`api_logs` (defined in [serving/storage/database.py](serving/storage/database.py)) already records per-request `provider`, `model_id`, `ttft_ms`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `status_code`, `stream`, `timestamp`, and is indexed on `(provider, timestamp DESC)` and `(model_id, timestamp DESC)`. Hourly aggregation by `(provider, model_id)` is well-served by these indexes.

APScheduler is already wired in via [serving/utils/email_scheduler.py](serving/utils/email_scheduler.py) (`AsyncIOScheduler`, UTC). Same scheduler instance hosts the hourly rollup job.

The admin dashboard at [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) already has an analytics tab using Recharts, and admin API routes live under [serving/servers/routers/admin.py](serving/servers/routers/admin.py).

## Architecture

```
[serving process]
  └── AsyncIOScheduler (existing)
       └── job: rollup_provider_stats  (CronTrigger minute=5, hourly)
            ├── pg_try_advisory_lock(0x70726F76737473)   # multi-replica safety
            ├── INSERT ... SELECT ... FROM api_logs
            │     WHERE timestamp >= prev_hour AND timestamp < this_hour
            │     GROUP BY hour_bucket, provider, model_id
            │     ON CONFLICT (provider, model_id, hour_bucket) DO UPDATE
            ├── DELETE FROM provider_hourly_stats
            │     WHERE hour_bucket < NOW() - INTERVAL '30 days'
            └── pg_advisory_unlock

  └── On startup: backfill_if_empty(days=30) — one-shot, idempotent

[Postgres]
  ├── api_logs                  (source, existing)
  └── provider_hourly_stats     (new, aggregated rollup)

[admin frontend]
  └── /dashboard/admin → tab "Provider Performance"
       ├── filters: provider, model, range (24h | 7d | 30d)
       ├── chart 1: TTFT p50/p95/p99 over time
       └── chart 2: Throughput avg/p50/p95 over time
       backed by GET /admin/api/provider-stats
```

The aggregation runs at minute 5 of each hour to give the previous hour's writes time to flush. The rollup is idempotent (UPSERT on the natural primary key), so a missed run is recovered the next hour by extending the lookback window — but the standard path covers exactly one hour.

## Data flow

1. Hourly tick (`:05` UTC) fires `hourly_job(pool)`.
2. Job acquires `pg_try_advisory_lock` on a fixed integer key. If another replica holds it, the job logs and returns.
3. Job runs `ROLLUP_SQL` for the just-completed hour `[prev_hour, this_hour)`.
4. Job runs purge `DELETE` for rows older than 30 days.
5. Job records Prometheus counter + last-success gauge, releases the lock.

On serving-process startup, `backfill_if_empty(pool, days=30)` checks whether `provider_hourly_stats` has any rows; if empty, it runs the same aggregation SQL with a 30-day window. Idempotent on conflict.

## Schema

```sql
CREATE TABLE IF NOT EXISTS provider_hourly_stats (
    hour_bucket             TIMESTAMPTZ NOT NULL,
    provider                TEXT        NOT NULL,
    model_id                TEXT        NOT NULL,

    -- counts
    request_count           INTEGER     NOT NULL,
    error_count             INTEGER     NOT NULL,
    stream_count            INTEGER     NOT NULL,

    -- TTFT (streaming, status<400, ttft_ms not null)
    ttft_p50_ms             INTEGER,
    ttft_p95_ms             INTEGER,
    ttft_p99_ms             INTEGER,

    -- end-to-end latency (status<400)
    latency_p50_ms          INTEGER,
    latency_p95_ms          INTEGER,
    latency_p99_ms          INTEGER,

    -- decode throughput tokens/sec
    throughput_avg_tps      FLOAT,
    throughput_p50_tps      FLOAT,
    throughput_p95_tps      FLOAT,

    -- token totals/averages
    prompt_tokens_avg       FLOAT,
    completion_tokens_avg   FLOAT,
    total_completion_tokens BIGINT      NOT NULL,

    PRIMARY KEY (provider, model_id, hour_bucket)
);

CREATE INDEX IF NOT EXISTS idx_phs_hour
    ON provider_hourly_stats(hour_bucket DESC);
CREATE INDEX IF NOT EXISTS idx_phs_provider_hour
    ON provider_hourly_stats(provider, hour_bucket DESC);
```

Definitions:
- **Throughput per request** (decode-only, in tokens/sec):
  - For successful streaming requests with `latency_ms > ttft_ms`: `completion_tokens / ((latency_ms - ttft_ms) / 1000.0)`.
  - For successful non-stream with `latency_ms > 0`: `completion_tokens / (latency_ms / 1000.0)`.
  - Otherwise (errors, missing/zero values): NULL — excluded from AVG/percentile.
- **Successful**: `status_code < 400 AND error IS NULL`. Used for TTFT, latency, throughput percentiles.
- **Error count**: `status_code >= 400 OR error IS NOT NULL`.
- **Stream count**: `stream = TRUE AND ttft_ms IS NOT NULL AND <successful>`.

The PRIMARY KEY is ordered `(provider, model_id, hour_bucket)` to match the dashboard's query pattern: filter by provider+model, range scan over time.

Empty groups are skipped (`HAVING COUNT(*) > 0`); we don't insert no-traffic rows.

## Rollup query

```sql
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
    total_completion_tokens = EXCLUDED.total_completion_tokens;
```

## Job runner

New module `serving/admin/provider_stats_rollup.py`:

```python
ADVISORY_LOCK_KEY = 0x70726F76737473  # constant; one lock for this job

async def run_rollup(pool, *, start, end) -> int:
    """Aggregate api_logs in [start, end) into provider_hourly_stats. Returns rows written."""

async def hourly_job(pool) -> None:
    """Roll up the last completed hour, then purge >30d rows."""

async def backfill_if_empty(pool, days: int = 30) -> None:
    """If table empty, run aggregation over last `days` days. One-shot, idempotent."""

async def purge_old(pool, retention_days: int = 30) -> int:
    """DELETE FROM provider_hourly_stats WHERE hour_bucket < NOW() - INTERVAL '$1 days'."""
```

Schedule registration (called from existing scheduler bootstrap during app startup):

```python
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
```

`misfire_grace_time=600` means a brief deploy/restart that delays the trigger by up to 10 min still runs once. `coalesce=True` collapses missed runs (idempotent UPSERT covers the gap on the next run).

## Admin API

`GET /admin/api/provider-stats` (registered in [serving/servers/routers/admin.py](serving/servers/routers/admin.py)):

| Param | Type | Default | Notes |
|---|---|---|---|
| `provider` | str | required | exact match on `provider` |
| `model_id` | str | required | exact match on `model_id` |
| `from` | ISO8601 | `now - 7d` | hour-truncated server-side |
| `to` | ISO8601 | `now` | hour-truncated server-side |

Range capped at 90 days; over-range → 400. Bad timestamp → 400. Empty result → 200 with `rows: []`.

Response schema (defined in `serving/schemas_admin.py`):

```json
{
  "rows": [
    {
      "hour_bucket": "2026-05-02T13:00:00Z",
      "provider": "openrouter",
      "model_id": "qwen/qwen3-coder",
      "request_count": 412,
      "error_count": 3,
      "stream_count": 380,
      "ttft_p50_ms": 410, "ttft_p95_ms": 980, "ttft_p99_ms": 1620,
      "latency_p50_ms": 5200, "latency_p95_ms": 18400, "latency_p99_ms": 31000,
      "throughput_avg_tps": 42.1,
      "throughput_p50_tps": 39.8,
      "throughput_p95_tps": 71.2,
      "prompt_tokens_avg": 812.4,
      "completion_tokens_avg": 187.3,
      "total_completion_tokens": 77168
    }
  ],
  "providers": ["openrouter", "anthropic", "chutes"],
  "models":    ["qwen/qwen3-coder", "anthropic/claude-opus-4-7"]
}
```

`providers` and `models` are the distinct lists from `provider_hourly_stats` over the requested window — used to populate the dashboard dropdowns. Cheap query: `SELECT DISTINCT provider FROM provider_hourly_stats WHERE hour_bucket BETWEEN ... AND ...`.

Auth: existing admin guard (same as other `/admin/*` routes).

## Frontend

New tab "Provider Performance" in the admin dashboard (`frontend/src/app/dashboard/admin/`):

- Layout: filter row on top — `<Select provider>`, `<Select model>`, `<Select range: 24h | 7d | 30d>`.
- KPI strip (small text row): `request_count`, `error_rate = error_count / request_count`, `total_completion_tokens` summed over the range.
- Chart 1 — TTFT (ms) line chart: lines `p50`, `p95`, `p99`. Shared time x-axis.
- Chart 2 — Throughput (tokens/sec) line chart: lines `avg`, `p50`, `p95`.

Tab default state: first provider + first model in the response, range 7d. If no data: empty-state copy ("no traffic recorded for this provider+model in this window").

For the first cut, both `provider` and `model_id` are required filters — no "All" rollup. We can add a request_count-weighted re-aggregation later if needed; YAGNI for now.

## Error handling

- Job: top-level `try/except` logs and swallows. APScheduler keeps running.
- Lock not acquired: log info, return; next hour retries.
- DB connection error during rollup: log error, return; next hour's run picks up the missed window if SQL is widened — but for now, an hour's gap is acceptable and the gauge alert below will surface persistent failures.
- Backfill failure on startup: log warning; do not block app startup; the next hourly run will eventually populate forward.
- API 4xx: bad/oversize range → 400 with structured error; missing required filter → 422 (FastAPI default).
- Throughput edge cases (`latency_ms <= ttft_ms`, `completion_tokens=0`, errors): NULL per-row, excluded from AVG and percentile.

## Testing

New file `test/admin/test_provider_stats.py`:

- **Rollup correctness**: seed `api_logs` with synthetic rows covering stream/non-stream, errors, missing TTFT, varied throughput. Run rollup; assert percentiles within 1ms of `numpy.percentile`, throughput averages correct, error_count/stream_count match filters.
- **Idempotency**: run rollup twice over the same window; row count and values unchanged.
- **Concurrent run**: open a second connection holding the advisory lock; run hourly_job; assert it logs+returns without insert.
- **Purge**: rows older than 30 days deleted, recent rows kept.
- **Backfill**: empty table → run `backfill_if_empty(days=30)` → expected number of distinct `(provider, model_id, hour)` triples populated.
- **API**: `GET /admin/api/provider-stats` — 200 happy path with filters, 401 unauth, 400 bad range, 400 oversize range.
- **Frontend**: smoke test the new tab renders with mocked response (matches existing admin tab test pattern).

## Observability

Prometheus, exposed via existing `/metrics` endpoint:

- `provider_stats_rollup_runs_total{outcome}` — counter, `outcome ∈ {ok, locked, error}`.
- `provider_stats_rollup_duration_seconds` — histogram of job duration.
- `provider_stats_last_success_unixtime` — gauge of last successful rollup wall-clock; alert via existing alertmanager if `time() - provider_stats_last_success_unixtime > 3 * 3600`.

Logging: per run, INFO line with `hours=[start..end] rows_written=N duration_ms=D outcome=ok|locked|error`.

## Files touched

| File | Change |
|---|---|
| [serving/storage/database.py](serving/storage/database.py) | Add `provider_hourly_stats` DDL + indexes in init. |
| `serving/admin/provider_stats_rollup.py` | New: SQL constants, `run_rollup`, `hourly_job`, `backfill_if_empty`, `purge_old`. |
| [serving/utils/email_scheduler.py](serving/utils/email_scheduler.py) **or** new `serving/utils/scheduler.py` | Register hourly job on the same `AsyncIOScheduler`; consider hoisting the scheduler module if multi-job ownership grows awkward. |
| [serving/servers/routers/admin.py](serving/servers/routers/admin.py) | Add `GET /admin/api/provider-stats`. |
| [serving/schemas_admin.py](serving/schemas_admin.py) | Add `ProviderStatsRow`, `ProviderStatsResponse`. |
| [serving/observability/metrics.py](serving/observability/metrics.py) | Add counter, histogram, gauge for the rollup job. |
| `frontend/src/app/dashboard/admin/...` | New "Provider Performance" tab with two Recharts line charts and a KPI strip. |
| `frontend/src/lib/api/admin.ts` (or equivalent) | Add `getProviderStats(...)` client. |
| `test/admin/test_provider_stats.py` | New tests (see Testing). |
| `deploy/prometheus/rules/` | New alert rule for stale `provider_stats_last_success_unixtime`. |

## Open questions

None blocking. Possible follow-ups: synthetic provider probing (populate `llm-prober/`); per-region/per-key-pool grouping if relevant; "All providers/All models" weighted-rollup view in the API.
