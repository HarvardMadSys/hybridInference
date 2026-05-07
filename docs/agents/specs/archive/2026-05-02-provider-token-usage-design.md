# Provider Token Usage Tracking

**Status:** Design
**Date:** 2026-05-02
**Author:** brainstorming session

## Summary

Track input, output, cached-read, and reasoning tokens, plus cost, that we send to each upstream model. Extend the existing `provider_hourly_stats` rollup table (added in #268) with four nullable columns; surface the data in a new admin tab "Token Usage" grouped by provider, with rows per model and a window selector (1h | 24h | 7d | 30d).

## Goals

- Show admins how many tokens (input / output / cache-read / reasoning) and how much cost we are sending to each upstream model over a chosen window.
- Reuse the existing hourly rollup machinery so we add no new cron jobs, no new advisory locks, no new background loops.
- Cheap reads — dashboard scans pre-aggregated `provider_hourly_stats` rows, never `api_logs`.
- Backfill historical hours so the new columns are populated for the prior 30 days on first deploy.
- Make stale data explicit — UI labels reads with the rollup hour boundary so users understand they are not seeing live data.

## Non-goals

- Per-user token attribution (this is provider/model level only).
- Live / sub-hour granularity (rollup is hourly; the `1h` selector shows the most recent completed hour).
- Cache **write** token tracking — explicitly dropped per design discussion. Only `cache_read_tokens` shown.
- CSV / JSON export from the dashboard.
- Cross-provider routing optimisation or alerting on token-usage thresholds.
- New rollup job — we extend the existing `rollup_provider_stats` job, not add a parallel one.

## Context

`api_logs` (in [serving/storage/database.py](serving/storage/database.py)) records per-request `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `cost_usd`, plus the `(provider, model_id, timestamp)` we group on.

`provider_hourly_stats` (added in #268) already aggregates `api_logs` hourly via `serving/admin/provider_stats_rollup.py` (`AsyncIOScheduler`, `CronTrigger(minute=5)` UTC). The job uses `pg_try_advisory_lock` for multi-replica safety and an idempotent `INSERT ... ON CONFLICT (provider, model_id, hour_bucket) DO UPDATE`. Existing columns include `prompt_tokens_avg`, `completion_tokens_avg`, `total_completion_tokens` — but no input/cache/reasoning **totals** and no cost total. That is the gap this spec closes.

Existing admin endpoint `GET /admin/api/provider-stats` requires `provider` + `model_id`; it powers the "Provider Performance" tab. We **do not** modify that endpoint — we add a separate one that returns all `(provider, model_id)` pairs over a window for the new tab.

## Architecture

```
[serving process]
  └── existing rollup_provider_stats job (no scheduler change)
       └── ROLLUP_SQL extended:
              + SUM(prompt_tokens)         AS total_prompt_tokens
              + SUM(cache_read_tokens)     AS total_cache_read_tokens
              + SUM(reasoning_tokens)      AS total_reasoning_tokens
              + SUM(cost_usd)              AS total_cost_usd
            (existing total_completion_tokens reused as output total)

  └── On startup: backfill_token_columns(pool, days=30)
        - runs only if at least one row in provider_hourly_stats has
          total_prompt_tokens IS NULL (first deploy after this change)
        - re-runs ROLLUP_SQL hour-by-hour for the last 30 days
        - idempotent UPSERT — safe to re-run

[Postgres]
  └── provider_hourly_stats   (existing, +4 nullable columns)

[admin frontend]
  └── /dashboard/admin → new tab "Token Usage"
       ├── range selector: 1h | 24h | 7d | 30d (default 24h)
       ├── KPI strip: input / output / cached / reasoning / requests / cost
       └── grouped tables: one section per provider, rows per model
       backed by GET /admin/api/provider-token-usage
```

## Data flow

1. Hourly tick at `:05` UTC fires the existing `hourly_job(pool)`. Same lock, same window.
2. ROLLUP_SQL now also computes the four new totals from `api_logs` and UPSERTs them.
3. On first app startup after this change: `backfill_token_columns` detects NULL columns and re-runs the rollup over the last 30 days, hour-by-hour.
4. Frontend calls `GET /admin/api/provider-token-usage?range=…` on mount and on range change. Endpoint sums hourly rows over the resolved window, groups by `(provider, model_id)`, returns the rows + a `totals` block.

## Schema change

Additive ALTER on the existing table — all new columns nullable so the change is metadata-only, no table rewrite.

```sql
ALTER TABLE provider_hourly_stats
    ADD COLUMN IF NOT EXISTS total_prompt_tokens     BIGINT,
    ADD COLUMN IF NOT EXISTS total_cache_read_tokens BIGINT,
    ADD COLUMN IF NOT EXISTS total_reasoning_tokens  BIGINT,
    ADD COLUMN IF NOT EXISTS total_cost_usd          DECIMAL(14, 8);
```

Existing `total_completion_tokens BIGINT NOT NULL` is reused as the output total (no new column needed for that).

Reads `COALESCE(col, 0)` everywhere so NULL on un-backfilled rows behaves as zero. Indexes unchanged — the dashboard query uses the existing `idx_phs_hour` (range scan on `hour_bucket DESC`) and groups in memory.

## Rollup query change

In `serving/admin/provider_stats_rollup.py`, extend `ROLLUP_SQL`:

- INSERT column list: append `total_prompt_tokens, total_cache_read_tokens, total_reasoning_tokens, total_cost_usd`.
- SELECT clause: append
  ```sql
  COALESCE(SUM(prompt_tokens), 0)::BIGINT       AS total_prompt_tokens,
  COALESCE(SUM(cache_read_tokens), 0)::BIGINT   AS total_cache_read_tokens,
  COALESCE(SUM(reasoning_tokens), 0)::BIGINT    AS total_reasoning_tokens,
  COALESCE(SUM(cost_usd), 0)::DECIMAL(14,8)     AS total_cost_usd
  ```
  No `FILTER (WHERE status_code < 400)` — token totals **include errored requests** because we paid (or attempted to send) those tokens regardless of upstream error. Latency/throughput percentiles continue to filter errors as before; that filtering is unchanged.
- `ON CONFLICT ... DO UPDATE SET`: append the same four columns (`= EXCLUDED.col`).

## Backfill helper

New function in `serving/admin/provider_stats_rollup.py`:

```python
async def backfill_token_columns(pool, days: int = 30) -> int:
    """Re-run rollup hour-by-hour over the last `days` days if any row has NULL token totals.

    Idempotent. Returns number of hours processed (0 if nothing to do).
    """
```

Logic:
1. `SELECT 1 FROM provider_hourly_stats WHERE total_prompt_tokens IS NULL LIMIT 1`. If empty → return 0.
2. Otherwise iterate `start = floor(now - days, hour)` to `end = floor(now, hour)` in 1-hour increments and call existing `run_rollup(pool, start=h, end=h+1h)` for each. UPSERT updates all columns including the new ones.
3. Log INFO `backfilled hours=N`.

Called once from app startup after the existing scheduler bootstrap. The hook lives in [serving/servers/bootstrap.py](serving/servers/bootstrap.py) — extend the existing `_run_backfill` async helper (currently calls `backfill_if_empty`) to also `await backfill_token_columns(pool, days=30)` after it. Same fire-and-forget pattern (`asyncio.create_task(_run_backfill())`) so a slow backfill cannot block readiness; failure logged as warning, non-fatal.

## Admin API

`GET /admin/api/provider-token-usage` (registered in [serving/servers/routers/admin.py](serving/servers/routers/admin.py)):

| Param   | Type | Default | Notes |
|---------|------|---------|-------|
| `range` | enum | `24h`   | one of `1h`, `24h`, `7d`, `30d`. Other → 422. |

Range → `[from, to)` resolution (UTC, hour-truncated):

| range | from                                                 | to                          |
|-------|------------------------------------------------------|-----------------------------|
| `1h`  | `date_trunc('hour', now()) - INTERVAL '1 hour'`      | `date_trunc('hour', now())` |
| `24h` | `date_trunc('hour', now()) - INTERVAL '24 hours'`    | `date_trunc('hour', now())` |
| `7d`  | `date_trunc('hour', now()) - INTERVAL '7 days'`      | `date_trunc('hour', now())` |
| `30d` | `date_trunc('hour', now()) - INTERVAL '30 days'`     | `date_trunc('hour', now())` |

Auth: existing admin guard (same as other `/admin/*` routes).

Query:

```sql
SELECT
    provider,
    model_id,
    COALESCE(SUM(total_prompt_tokens), 0)      AS input_tokens,
    COALESCE(SUM(total_completion_tokens), 0)  AS output_tokens,
    COALESCE(SUM(total_cache_read_tokens), 0)  AS cached_tokens,
    COALESCE(SUM(total_reasoning_tokens), 0)   AS reasoning_tokens,
    COALESCE(SUM(total_cost_usd), 0)::FLOAT    AS cost_usd,
    COALESCE(SUM(request_count), 0)            AS request_count
FROM provider_hourly_stats
WHERE hour_bucket >= $1 AND hour_bucket < $2
GROUP BY provider, model_id
ORDER BY (
      COALESCE(SUM(total_prompt_tokens), 0)
    + COALESCE(SUM(total_completion_tokens), 0)
    + COALESCE(SUM(total_cache_read_tokens), 0)
    + COALESCE(SUM(total_reasoning_tokens), 0)
) DESC;
```

`totals` is computed in Python by summing the returned rows (cheap; rows are small). Avoids a second DB round-trip.

Response schema (added to `serving/schemas_admin.py`):

```json
{
  "range": "24h",
  "window": {"from": "2026-05-01T15:00:00Z", "to": "2026-05-02T15:00:00Z"},
  "refreshed_at": "2026-05-02T15:00:00Z",
  "rows": [
    {
      "provider": "anthropic",
      "model_id": "claude-opus-4-7",
      "input_tokens": 1234567,
      "output_tokens": 234567,
      "cached_tokens": 89012,
      "reasoning_tokens": 12345,
      "cost_usd": 12.3456,
      "request_count": 412
    }
  ],
  "totals": {
    "input_tokens": 9876543,
    "output_tokens": 1234567,
    "cached_tokens": 234567,
    "reasoning_tokens": 12345,
    "cost_usd": 56.78,
    "request_count": 4321
  }
}
```

`refreshed_at` equals `to` — the most recent hour boundary the rollup is guaranteed to have covered. Frontend uses it to render the "Updated at HH:00 UTC" caveat.

Pydantic models added to `serving/schemas_admin.py`:

```python
class ProviderTokenUsageRow(BaseModel):
    provider: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    request_count: int

class ProviderTokenUsageTotals(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    request_count: int

class ProviderTokenUsageResponse(BaseModel):
    range: Literal["1h", "24h", "7d", "30d"]
    window: dict[str, datetime]   # from, to (ISO8601 UTC)
    refreshed_at: datetime
    rows: list[ProviderTokenUsageRow]
    totals: ProviderTokenUsageTotals
```

## Frontend

New tab "Token Usage" in the admin dashboard. Tab key `'token-usage'` added to the `activeTab` union in [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx); button placed next to the existing tabs; rendering delegated to a new component.

New file `frontend/src/app/dashboard/admin/TokenUsageTab.tsx`:

**Layout**

```
┌─────────────────────────────────────────────────────────┐
│ [Range: 1h | 24h | 7d | 30d ▼]   Updated at 14:00 UTC   │
│                                  (hourly refresh)       │
├─────────────────────────────────────────────────────────┤
│ KPI strip (6 tiles, tabular-nums):                      │
│ Input  Output  Cached  Reasoning  Requests  Cost USD    │
├─────────────────────────────────────────────────────────┤
│ ┌── anthropic ──────────────────────────────────────┐   │
│ │ Model            Input  Output  Cached  Reason    │   │
│ │                                  Reqs   Cost      │   │
│ │ claude-opus-4-7  1.2M   234K    89K     12K       │   │
│ │                                  412    $12.35    │   │
│ │ claude-sonnet-…   …     …        …      …         │   │
│ └───────────────────────────────────────────────────┘   │
│ ┌── openrouter ─────────────────────────────────────┐   │
│ │ ...                                               │   │
│ └───────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**Behaviour**

- On mount and on range change: `GET /admin/api/provider-token-usage?range=<range>`. No auto-refresh (data only changes hourly).
- Provider sections are sorted by sum of (input + output + cached + reasoning) DESC. Inside each provider, model rows are pre-sorted by the API.
- Number formatting reuses the existing `formatNum` helper at [page.tsx:244](frontend/src/app/dashboard/admin/page.tsx#L244); for large counts use `Intl.NumberFormat` `notation: "compact"` (e.g. `1.2M`, `234K`). Cost formatted as `$12.35` (2 decimals; for sub-dollar use 4 decimals).
- "Updated at HH:00 UTC" label shown next to the range selector (always visible) — explicitly conveys hourly cadence. For `range=1h` additionally show "showing hour [12:00–13:00 UTC]" so the user understands they are looking at one specific hour bucket.
- Loading: spinner mirroring existing tabs.
- Empty per-provider section: skip the section entirely (do not render an empty table).
- Empty overall (no rows): single empty-state message "No token usage recorded in this window."
- Error from API: same error banner pattern used by other admin tabs.

**API client**

Add `getProviderTokenUsage(range)` to [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts) (alongside the existing `/admin/api/provider-stats` client). Returns the typed response above.

## Error handling

- Backfill failure on startup: log warning, do not block app startup. The next hourly run will populate forward; admin can re-trigger by restarting (the NULL check re-runs).
- Rollup SQL change is purely additive (new SELECT/INSERT/ON CONFLICT columns) — existing rollup behaviour unchanged.
- API: bad `range` → 422 (FastAPI default). Empty result → 200 with `rows: []` and zero totals. DB error → 500 with structured error (existing handler).
- Frontend: failed fetch → error banner; range selector remains usable so the user can retry.

## Testing

Extend `test/integration/test_provider_stats_rollup.py`:
- Seed `api_logs` rows with non-null `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `reasoning_tokens`, `cost_usd` (mix of stream/non-stream, success/error, mix of NULLs in some columns to confirm `COALESCE(SUM(...), 0)` behaviour). Run rollup. Assert each new total equals the seeded `SUM`.
- Backfill: insert rows directly into `provider_hourly_stats` with NULL token totals (simulate post-deploy state). Call `backfill_token_columns(pool, days=2)`. Assert all rows now non-NULL with values matching the underlying `api_logs`.
- Backfill idempotency: run `backfill_token_columns` twice; row values unchanged on the second run.
- Backfill no-op: when no NULL rows exist, returns 0 without iterating.

New file `test/servers/test_admin_token_usage.py`:
- 200 happy path for each `range` (`1h`, `24h`, `7d`, `30d`); response shape matches schema; rows sorted DESC by total token sum; `totals` equals row sums.
- `range=invalid` → 422.
- 401 unauth (no admin token).
- Empty `provider_hourly_stats` → 200 with `rows: []` and all-zero `totals`.
- Window inclusion: insert rows just before `from` and just after `to`; assert excluded; insert rows exactly at `from`; assert included (`>=`).

New file `frontend/src/app/dashboard/admin/__tests__/TokenUsageTab.test.tsx`:
- Mocks `/admin/api/provider-token-usage`. Asserts: KPI strip renders six tiles with totals from the mock; provider sections rendered in DESC order of total tokens; range change re-fetches with the new param; "Updated at" label shows the mocked `refreshed_at` hour.

## Files touched

| File | Change |
|------|--------|
| [serving/storage/database.py](serving/storage/database.py) | ALTER TABLE: add 4 nullable columns to `provider_hourly_stats` |
| [serving/admin/provider_stats_rollup.py](serving/admin/provider_stats_rollup.py) | Extend `ROLLUP_SQL` SELECT/INSERT/ON CONFLICT with 4 new totals; add `backfill_token_columns(pool, days=30)` |
| [serving/servers/bootstrap.py](serving/servers/bootstrap.py) | Extend the existing `_run_backfill` helper to also call `backfill_token_columns(pool, days=30)` |
| [serving/servers/routers/admin.py](serving/servers/routers/admin.py) | Add `GET /admin/api/provider-token-usage` route |
| [serving/schemas_admin.py](serving/schemas_admin.py) | Add `ProviderTokenUsageRow`, `ProviderTokenUsageTotals`, `ProviderTokenUsageResponse` |
| [frontend/src/app/dashboard/admin/page.tsx](frontend/src/app/dashboard/admin/page.tsx) | Add `'token-usage'` to `activeTab` union, add tab button, lazy-render `<TokenUsageTab/>` |
| frontend/src/app/dashboard/admin/TokenUsageTab.tsx | New component (range selector + KPI strip + grouped provider tables) |
| [frontend/src/lib/api/admin.ts](frontend/src/lib/api/admin.ts) | Add `getProviderTokenUsage(range)` client + types |
| test/integration/test_provider_stats_rollup.py | Extend with token-totals + backfill tests |
| test/servers/test_admin_token_usage.py | New: API tests |
| frontend/src/app/dashboard/admin/__tests__/TokenUsageTab.test.tsx | New: frontend smoke test |

## Open questions

None blocking. Possible follow-ups:
- Per-API-key token attribution (would need a separate rollup keyed on `api_key_id`).
- CSV export from the tab.
- Charting tokens-over-time per model (a dedicated time-series view distinct from this aggregate-over-window view).
