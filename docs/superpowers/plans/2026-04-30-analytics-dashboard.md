# Analytics Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Analytics tab to the existing admin dashboard showing active users, top users by request share, requests by model, and requests by provider — selectable for the past hour, day, week, or month.

**Architecture:** A new `AnalyticsTab.tsx` component handles all analytics UI and owns its own state and data fetching (matching the existing `useState`/`useCallback`/`useEffect` pattern in the admin page). A single `GET /admin/analytics?period=hour|day|week|month` endpoint runs 5 queries in parallel via `asyncio.gather` and returns all chart data in one response. The admin page gains a 4th tab that mounts `AnalyticsTab`.

**Tech Stack:** FastAPI + asyncpg (backend), Next.js 15 + TypeScript + Tailwind (frontend), Recharts 3.x (charts)

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Modify | `serving/schemas_admin.py` | Add `AdminAnalyticsResponse` + sub-schemas |
| Modify | `serving/servers/routers/admin.py` | Add `GET /admin/analytics` endpoint |
| Modify | `frontend/src/lib/api/admin.ts` | Add TS types + `getAnalytics()` function |
| Create | `frontend/src/app/dashboard/admin/AnalyticsTab.tsx` | All analytics UI (period selector + 4 charts) |
| Modify | `frontend/src/app/dashboard/admin/page.tsx` | Add 'analytics' tab type, button, panel, load trigger |
| Modify | `frontend/package.json` | Add `recharts` dependency |
| Modify | `test/unit/test_analytics_schemas.py` | Unit tests for the new schemas |

---

## Task 1: Install Recharts

**Files:**
- Modify: `frontend/package.json`

- [ ] **Step 1: Install recharts**

```bash
cd /srv/hybridInference/frontend && npm install recharts
```

Expected output: `added N packages` with `recharts` listed.

- [ ] **Step 2: Verify import works**

```bash
cd /srv/hybridInference/frontend && node -e "require('recharts'); console.log('ok')" 2>/dev/null || npx tsc --noEmit 2>&1 | head -5
```

Expected: no error about recharts missing.

- [ ] **Step 3: Commit**

```bash
cd /srv/hybridInference/frontend && git add package.json package-lock.json
git commit -m "chore(frontend): add recharts for analytics charts"
```

---

## Task 2: Add backend Pydantic schemas

**Files:**
- Modify: `serving/schemas_admin.py`
- Create: `test/unit/test_analytics_schemas.py`

- [ ] **Step 1: Write the failing tests**

Create `test/unit/test_analytics_schemas.py`:

```python
"""Unit tests for AdminAnalyticsResponse schema."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from serving.schemas_admin import (
    AdminAnalyticsResponse,
    AnalyticsBreakdownEntry,
    AnalyticsUserEntry,
    SparklineBucket,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_sparkline_bucket_valid():
    b = SparklineBucket(start_time=_now(), request_count=42)
    assert b.request_count == 42


def test_analytics_user_entry_fraction_range():
    e = AnalyticsUserEntry(
        email="alice@example.com",
        user_id="u1",
        requests=100,
        fraction=0.38,
    )
    assert e.fraction == pytest.approx(0.38)


def test_analytics_breakdown_entry_others():
    e = AnalyticsBreakdownEntry(name="others", requests=50, fraction=0.12)
    assert e.name == "others"


def test_admin_analytics_response_full():
    resp = AdminAnalyticsResponse(
        period="day",
        active_users=47,
        sparkline=[SparklineBucket(start_time=_now(), request_count=10)],
        top_users=[
            AnalyticsUserEntry(
                email="alice@example.com", user_id="u1", requests=200, fraction=0.5
            )
        ],
        by_model=[AnalyticsBreakdownEntry(name="claude-sonnet-4-6", requests=200, fraction=0.5)],
        by_provider=[AnalyticsBreakdownEntry(name="anthropic", requests=200, fraction=0.5)],
        generated_at=_now(),
    )
    assert resp.period == "day"
    assert resp.active_users == 47


def test_admin_analytics_response_invalid_period():
    with pytest.raises(ValidationError):
        AdminAnalyticsResponse(
            period="invalid",
            active_users=0,
            sparkline=[],
            top_users=[],
            by_model=[],
            by_provider=[],
            generated_at=_now(),
        )
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /srv/hybridInference && python -m pytest test/unit/test_analytics_schemas.py -v 2>&1 | tail -20
```

Expected: `ImportError` or `FAILED` — schemas don't exist yet.

- [ ] **Step 3: Add schemas to `serving/schemas_admin.py`**

Append to the end of `serving/schemas_admin.py`:

```python
# ── Analytics Dashboard ──────────────────────────────────────────────────────

class SparklineBucket(BaseModel):
    """One time bucket for the active-users sparkline."""

    start_time: datetime
    request_count: int


class AnalyticsUserEntry(BaseModel):
    """One row in the top-users horizontal bar chart."""

    email: str
    user_id: str
    requests: int
    fraction: float  # share of ALL requests in the period (0.0–1.0)


class AnalyticsBreakdownEntry(BaseModel):
    """One slice in a model or provider donut chart."""

    name: str  # model_id / provider name; "others" for the collapsed remainder
    requests: int
    fraction: float  # share of total requests in the period


class AdminAnalyticsResponse(BaseModel):
    """Response for GET /admin/analytics."""

    period: str = Field(..., pattern="^(hour|day|week|month)$")
    active_users: int
    sparkline: list[SparklineBucket]
    top_users: list[AnalyticsUserEntry]
    by_model: list[AnalyticsBreakdownEntry]
    by_provider: list[AnalyticsBreakdownEntry]
    generated_at: datetime
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd /srv/hybridInference && python -m pytest test/unit/test_analytics_schemas.py -v 2>&1 | tail -20
```

Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add serving/schemas_admin.py test/unit/test_analytics_schemas.py
git commit -m "feat(admin): add AdminAnalyticsResponse pydantic schemas"
```

---

## Task 3: Add `GET /admin/analytics` backend endpoint

**Files:**
- Modify: `serving/servers/routers/admin.py`

- [ ] **Step 1: Add imports to `admin.py`**

Near the top of `serving/servers/routers/admin.py`, add `AdminAnalyticsResponse` to the existing import block from `serving.schemas_admin`:

```python
# Find the existing import block that starts with:
# from serving.schemas_admin import (
# Add AdminAnalyticsResponse to that list.
```

The import block currently starts around line 11. Add `AdminAnalyticsResponse,` to it.

- [ ] **Step 2: Add the endpoint**

Append the following to `serving/servers/routers/admin.py` (before the last line, after the `admin_list_recent_requests` function):

```python
# Period → (lookback minutes, sparkline bucket minutes, sparkline bucket count)
_ANALYTICS_PERIODS: dict[str, tuple[int, int, int]] = {
    "hour":  (60,    5,  12),
    "day":   (1440,  60, 24),
    "week":  (10080, 1440, 7),
    "month": (43200, 1440, 30),
}


@router.get("/admin/analytics", response_model=AdminAnalyticsResponse)
async def admin_get_analytics(
    period: str = "day",
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminAnalyticsResponse:
    """Return analytics summary for the admin analytics tab."""
    import asyncio

    if period not in _ANALYTICS_PERIODS:
        raise HTTPException(400, f"period must be one of: {', '.join(_ANALYTICS_PERIODS)}")

    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    lookback_minutes, bucket_minutes, _bucket_count = _ANALYTICS_PERIODS[period]

    async with db_logger.pool.acquire() as conn:

        async def q_active_users() -> int:
            row = await conn.fetchrow(
                """
                SELECT COUNT(DISTINCT user_id) AS cnt
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                  AND user_id IS NOT NULL
                """,
                lookback_minutes,
            )
            return int(row["cnt"] or 0)

        async def q_top_users() -> list[dict]:
            rows = await conn.fetch(
                """
                WITH totals AS (
                    SELECT COUNT(*) AS grand_total
                    FROM api_logs
                    WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                      AND user_id IS NOT NULL
                ),
                ranked AS (
                    SELECT
                        l.user_id,
                        COALESCE(u.email, l.user_id) AS email,
                        COUNT(*) AS req_count
                    FROM api_logs l
                    LEFT JOIN users u ON u.id = l.user_id
                    WHERE l.timestamp >= NOW() - ($1 * interval '1 minute')
                      AND l.user_id IS NOT NULL
                    GROUP BY l.user_id, u.email
                    ORDER BY req_count DESC
                    LIMIT 10
                )
                SELECT
                    r.user_id,
                    r.email,
                    r.req_count,
                    CASE WHEN t.grand_total > 0
                         THEN r.req_count::float / t.grand_total
                         ELSE 0 END AS fraction
                FROM ranked r, totals t
                """,
                lookback_minutes,
            )
            return [dict(r) for r in rows]

        async def q_by_model() -> list[dict]:
            rows = await conn.fetch(
                """
                WITH totals AS (
                    SELECT COUNT(*) AS grand_total
                    FROM api_logs
                    WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                ),
                ranked AS (
                    SELECT model_id AS name, COUNT(*) AS req_count
                    FROM api_logs
                    WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                    GROUP BY model_id
                    ORDER BY req_count DESC
                    LIMIT 5
                )
                SELECT
                    r.name,
                    r.req_count,
                    CASE WHEN t.grand_total > 0
                         THEN r.req_count::float / t.grand_total
                         ELSE 0 END AS fraction
                FROM ranked r, totals t
                """,
                lookback_minutes,
            )
            return [dict(r) for r in rows]

        async def q_by_provider() -> list[dict]:
            rows = await conn.fetch(
                """
                WITH totals AS (
                    SELECT COUNT(*) AS grand_total
                    FROM api_logs
                    WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                ),
                ranked AS (
                    SELECT provider AS name, COUNT(*) AS req_count
                    FROM api_logs
                    WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                    GROUP BY provider
                    ORDER BY req_count DESC
                    LIMIT 4
                )
                SELECT
                    r.name,
                    r.req_count,
                    CASE WHEN t.grand_total > 0
                         THEN r.req_count::float / t.grand_total
                         ELSE 0 END AS fraction
                FROM ranked r, totals t
                """,
                lookback_minutes,
            )
            return [dict(r) for r in rows]

        async def q_sparkline() -> list[dict]:
            rows = await conn.fetch(
                """
                WITH series AS (
                    SELECT generate_series(
                        date_trunc('minute', NOW() - ($1 * interval '1 minute')),
                        date_trunc('minute', NOW()),
                        $2 * interval '1 minute'
                    ) AS bucket_start
                ),
                bucketed AS (
                    SELECT
                        to_timestamp(
                            floor(extract(epoch FROM timestamp) / ($2 * 60)) * ($2 * 60)
                        ) AS bucket_start,
                        COUNT(*) AS request_count
                    FROM api_logs
                    WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                    GROUP BY 1
                )
                SELECT
                    series.bucket_start,
                    COALESCE(bucketed.request_count, 0) AS request_count
                FROM series
                LEFT JOIN bucketed ON bucketed.bucket_start = series.bucket_start
                ORDER BY series.bucket_start ASC
                """,
                lookback_minutes,
                bucket_minutes,
            )
            return [dict(r) for r in rows]

        (
            active_users,
            top_users_rows,
            by_model_rows,
            by_provider_rows,
            sparkline_rows,
        ) = await asyncio.gather(
            q_active_users(),
            q_top_users(),
            q_by_model(),
            q_by_provider(),
            q_sparkline(),
        )

    from serving.schemas_admin import (
        AdminAnalyticsResponse,
        AnalyticsBreakdownEntry,
        AnalyticsUserEntry,
        SparklineBucket,
    )

    return AdminAnalyticsResponse(
        period=period,
        active_users=active_users,
        sparkline=[
            SparklineBucket(
                start_time=row["bucket_start"],
                request_count=int(row["request_count"] or 0),
            )
            for row in sparkline_rows
        ],
        top_users=[
            AnalyticsUserEntry(
                email=str(row["email"]),
                user_id=str(row["user_id"]),
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in top_users_rows
        ],
        by_model=[
            AnalyticsBreakdownEntry(
                name=str(row["name"]) if row["name"] else "unknown",
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in by_model_rows
        ],
        by_provider=[
            AnalyticsBreakdownEntry(
                name=str(row["name"]) if row["name"] else "unknown",
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in by_provider_rows
        ],
        generated_at=datetime.now(timezone.utc),
    )
```

- [ ] **Step 3: Move imports to top of file**

Move the `from serving.schemas_admin import (AdminAnalyticsResponse, AnalyticsBreakdownEntry, AnalyticsUserEntry, SparklineBucket)` block from inside the function body to the existing `from serving.schemas_admin import (...)` block at the top of the file (around line 11), adding the four new names to that list.

- [ ] **Step 4: Verify the server starts without errors**

```bash
cd /srv/hybridInference && python -c "from serving.servers.routers.admin import router; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Manual smoke test against running server**

```bash
# Replace TOKEN with a valid admin JWT
curl -s "http://localhost:8000/admin/analytics?period=day" \
  -H "Authorization: Bearer TOKEN" | python3 -m json.tool | head -40
```

Expected: JSON with keys `period`, `active_users`, `sparkline`, `top_users`, `by_model`, `by_provider`, `generated_at`.

- [ ] **Step 6: Commit**

```bash
git add serving/servers/routers/admin.py serving/schemas_admin.py
git commit -m "feat(admin): add GET /admin/analytics endpoint with parallel queries"
```

---

## Task 4: Add TypeScript API client function

**Files:**
- Modify: `frontend/src/lib/api/admin.ts`

- [ ] **Step 1: Append types and function to `admin.ts`**

Add to the end of `frontend/src/lib/api/admin.ts`:

```typescript
// ========================================
// Analytics
// ========================================

export type AnalyticsPeriod = 'hour' | 'day' | 'week' | 'month';

export interface SparklineBucket {
  start_time: string;
  request_count: number;
}

export interface AnalyticsUserEntry {
  email: string;
  user_id: string;
  requests: number;
  fraction: number; // 0.0–1.0 share of all requests in period
}

export interface AnalyticsBreakdownEntry {
  name: string; // model_id, provider, or "others"
  requests: number;
  fraction: number;
}

export interface AdminAnalyticsResponse {
  period: AnalyticsPeriod;
  active_users: number;
  sparkline: SparklineBucket[];
  top_users: AnalyticsUserEntry[];
  by_model: AnalyticsBreakdownEntry[];
  by_provider: AnalyticsBreakdownEntry[];
  generated_at: string;
}

export async function getAnalytics(period: AnalyticsPeriod): Promise<AdminAnalyticsResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/analytics?period=${period}`);
  return jsonOrThrow<AdminAnalyticsResponse>(resp);
}
```

- [ ] **Step 2: Type-check**

```bash
cd /srv/hybridInference/frontend && npx tsc --noEmit 2>&1 | head -20
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api/admin.ts
git commit -m "feat(admin): add getAnalytics API client function and TS types"
```

---

## Task 5: Create `AnalyticsTab.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/AnalyticsTab.tsx`

- [ ] **Step 1: Create the component**

Create `frontend/src/app/dashboard/admin/AnalyticsTab.tsx`:

```tsx
'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  AdminAnalyticsResponse,
  AnalyticsPeriod,
  getAnalytics,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const PERIODS: { key: AnalyticsPeriod; label: string }[] = [
  { key: 'hour', label: 'Hour' },
  { key: 'day', label: 'Day' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
];

const CHART_COLORS = [
  '#3b82f6', '#f59e0b', '#10b981', '#8b5cf6',
  '#ec4899', '#06b6d4', '#f97316', '#84cc16',
  '#e11d48', '#7c3aed',
];
const OTHERS_COLOR = '#9ca3af';

function colorFor(name: string, index: number): string {
  if (name === 'others' || name === 'unknown') return OTHERS_COLOR;
  return CHART_COLORS[index % CHART_COLORS.length];
}

function pct(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function ActiveUsersCard({
  data,
  period,
}: {
  data: AdminAnalyticsResponse;
  period: AnalyticsPeriod;
}) {
  const sparklineData = data.sparkline.map((b) => ({
    t: b.start_time,
    v: b.request_count,
  }));
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        Active Users
      </p>
      <p className="mt-1 text-[40px] font-bold leading-none text-gray-900">
        {data.active_users}
      </p>
      <p className="mt-1 text-[12px] text-gray-400">
        unique users · past {period}
      </p>
      <div className="mt-4 h-10">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={sparklineData} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
            <Bar dataKey="v" fill="#93c5fd" radius={[2, 2, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-[10px] text-gray-300">total requests over time</p>
    </div>
  );
}

function DonutCard({
  title,
  entries,
}: {
  title: string;
  entries: AdminAnalyticsResponse['by_model'];
}) {
  const data = entries.map((e, i) => ({
    name: e.name,
    value: e.requests,
    fraction: e.fraction,
    color: colorFor(e.name, i),
  }));
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <p className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        {title}
      </p>
      {data.length === 0 ? (
        <p className="text-[13px] text-gray-300">No data</p>
      ) : (
        <div className="flex items-center gap-4">
          <div className="h-[90px] w-[90px] shrink-0">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={data}
                  cx="50%"
                  cy="50%"
                  innerRadius={28}
                  outerRadius={44}
                  dataKey="value"
                  isAnimationActive={false}
                  stroke="none"
                >
                  {data.map((entry, i) => (
                    <Cell key={i} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value: number, name: string) => [value, name]}
                  contentStyle={{ fontSize: 11 }}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="flex flex-col gap-1.5 overflow-hidden">
            {data.map((entry, i) => (
              <div key={i} className="flex items-center gap-1.5 text-[11px]">
                <div
                  className="h-2.5 w-2.5 shrink-0 rounded-sm"
                  style={{ background: entry.color }}
                />
                <span className="truncate text-gray-700">{entry.name}</span>
                <span className="ml-auto shrink-0 text-gray-400">{pct(entry.fraction)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TopUsersCard({ entries }: { entries: AdminAnalyticsResponse['top_users'] }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <p className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        Top Users by Requests
      </p>
      {entries.length === 0 ? (
        <p className="text-[13px] text-gray-300">No data</p>
      ) : (
        <div className="h-[160px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              layout="vertical"
              data={entries.map((e) => ({
                email: e.email.length > 22 ? e.email.slice(0, 20) + '…' : e.email,
                requests: e.requests,
                pct: parseFloat((e.fraction * 100).toFixed(1)),
              }))}
              margin={{ top: 0, right: 40, left: 0, bottom: 0 }}
            >
              <XAxis type="number" hide />
              <YAxis
                type="category"
                dataKey="email"
                width={110}
                tick={{ fontSize: 10, fill: '#6b7280' }}
              />
              <Tooltip
                formatter={(v: number) => [`${v} reqs`, 'Requests']}
                contentStyle={{ fontSize: 11 }}
              />
              <Bar dataKey="requests" fill="#3b82f6" radius={[0, 3, 3, 0]} isAnimationActive={false}>
                {entries.map((_, i) => (
                  <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ── Skeleton ─────────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <div className="rounded-xl border border-gray-100 bg-gray-50 p-5">
      <div className="mb-3 h-2.5 w-24 animate-pulse rounded bg-gray-200" />
      <div className="h-8 w-16 animate-pulse rounded bg-gray-200" />
      <div className="mt-4 h-10 w-full animate-pulse rounded bg-gray-200" />
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function AnalyticsTab() {
  const [period, setPeriod] = useState<AnalyticsPeriod>('day');
  const [data, setData] = useState<AdminAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (p: AnalyticsPeriod) => {
    setLoading(true);
    setError(null);
    try {
      const d = await getAnalytics(p);
      setData(d);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(period);
  }, [load, period]);

  const handlePeriod = (p: AnalyticsPeriod) => {
    setPeriod(p);
    load(p);
  };

  return (
    <div className="mt-6">
      {/* Period selector */}
      <div className="mb-5 flex gap-2">
        {PERIODS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => handlePeriod(key)}
            className={`rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
              period === key
                ? 'bg-gray-900 text-white'
                : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 rounded-lg bg-red-50 px-4 py-2.5 text-[13px] text-red-600">
          {error}
        </div>
      )}

      {/* 2×2 grid */}
      <div className="grid grid-cols-2 gap-4">
        {loading || !data ? (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </>
        ) : (
          <>
            <ActiveUsersCard data={data} period={period} />
            <DonutCard title="Requests by Model" entries={data.by_model} />
            <TopUsersCard entries={data.top_users} />
            <DonutCard title="Requests by Provider" entries={data.by_provider} />
          </>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

```bash
cd /srv/hybridInference/frontend && npx tsc --noEmit 2>&1 | head -20
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/AnalyticsTab.tsx
git commit -m "feat(admin): add AnalyticsTab component with Recharts charts"
```

---

## Task 6: Wire Analytics tab into the admin page

**Files:**
- Modify: `frontend/src/app/dashboard/admin/page.tsx`

- [ ] **Step 1: Add the import**

At the top of `frontend/src/app/dashboard/admin/page.tsx`, add after the existing imports:

```tsx
import { AnalyticsTab } from './AnalyticsTab';
```

- [ ] **Step 2: Expand the tab type**

Find this line (around line 154):

```tsx
const [activeTab, setActiveTab] = useState<'users' | 'audit' | 'requests'>('users');
```

Replace with:

```tsx
const [activeTab, setActiveTab] = useState<'users' | 'audit' | 'requests' | 'analytics'>('users');
```

- [ ] **Step 3: Update `onTabChange`**

Find (around line 447):

```tsx
const onTabChange = (tab: 'users' | 'audit' | 'requests') => {
```

Replace with:

```tsx
const onTabChange = (tab: 'users' | 'audit' | 'requests' | 'analytics') => {
```

- [ ] **Step 4: Update URL restore logic**

Find (around line 158):

```tsx
if (tab === 'users' || tab === 'audit' || tab === 'requests') {
```

Replace with:

```tsx
if (tab === 'users' || tab === 'audit' || tab === 'requests' || tab === 'analytics') {
```

- [ ] **Step 5: Add analytics to the tab bar**

Find the tab bar mapping (around line 509):

```tsx
{(['users', 'requests', 'audit'] as const).map((tab) => (
```

Replace with:

```tsx
{(['users', 'requests', 'audit', 'analytics'] as const).map((tab) => (
```

Find the tab label inside the map:

```tsx
{tab === 'users' ? 'Users' : tab === 'requests' ? 'Recent Requests' : 'Audit Log'}
```

Replace with:

```tsx
{tab === 'users'
  ? 'Users'
  : tab === 'requests'
    ? 'Recent Requests'
    : tab === 'audit'
      ? 'Audit Log'
      : 'Analytics'}
```

- [ ] **Step 6: Add the tab panel**

After the closing brace of the `{activeTab === 'requests' && ( ... )}` block (around line 1150), add:

```tsx
{activeTab === 'analytics' && <AnalyticsTab />}
```

- [ ] **Step 7: Type-check**

```bash
cd /srv/hybridInference/frontend && npx tsc --noEmit 2>&1 | head -20
```

Expected: no errors.

- [ ] **Step 8: Run the dev server and verify manually**

```bash
cd /srv/hybridInference/frontend && npm run dev
```

Open `http://localhost:3001/dashboard/admin?tab=analytics` in a browser. Verify:
- "Analytics" tab button appears and is selectable
- Period buttons (Hour / Day / Week / Month) appear
- Skeleton cards show while loading
- All 4 charts render with real data after load
- Switching periods refetches and re-renders

- [ ] **Step 9: Commit**

```bash
git add frontend/src/app/dashboard/admin/page.tsx
git commit -m "feat(admin): wire Analytics tab into admin dashboard"
```

---

## Task 7: Pull request

- [ ] **Step 1: Push branch and open PR**

```bash
git push origin HEAD
gh pr create \
  --base dev \
  --title "feat(admin): analytics dashboard tab with Recharts charts" \
  --body "$(cat <<'EOF'
## Summary
- Adds Analytics tab to the admin dashboard with 4 charts: active users stat, requests by model (donut), top users by request % (horizontal bar), requests by provider (donut)
- Period selector: Hour / Day / Week / Month
- New `GET /admin/analytics?period=` endpoint backed by 5 parallel asyncpg queries
- Recharts added as a frontend dependency

## Test plan
- [ ] Unit tests pass: `pytest test/unit/test_analytics_schemas.py -v`
- [ ] Server imports cleanly: `python -c "from serving.servers.routers.admin import router"`
- [ ] TypeScript type-checks: `cd frontend && npx tsc --noEmit`
- [ ] Analytics tab renders in browser at `/dashboard/admin?tab=analytics`
- [ ] All 4 period buttons switch data correctly
- [ ] Skeleton shown during load; error message shown on fetch failure
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- [x] New "Analytics" tab in existing admin page → Task 6
- [x] Period selector: Hour / Day / Week / Month → Task 5 (`AnalyticsTab`)
- [x] Active Users stat + sparkline → Task 5 (`ActiveUsersCard`)
- [x] Requests by Model donut → Task 5 (`DonutCard`)
- [x] Top Users horizontal bar, up to 10 capped at active users → Task 5 (`TopUsersCard`) + Task 3 (`LIMIT 10`)
- [x] Requests by Provider donut → Task 5 (`DonutCard`)
- [x] Single backend endpoint → Task 3
- [x] Recharts → Task 1 + Task 5
- [x] Pydantic schema → Task 2
- [x] TypeScript types + API client → Task 4

**Placeholder scan:** No TBDs, TODOs, or vague steps. All code blocks are complete.

**Type consistency:**
- `AnalyticsPeriod` defined in `admin.ts` Task 4, used in `AnalyticsTab.tsx` Task 5 ✓
- `AdminAnalyticsResponse` defined in Task 4, consumed in Task 5 ✓
- `getAnalytics(p: AnalyticsPeriod)` called in Task 5 with correct signature ✓
- Pydantic `AdminAnalyticsResponse` in Task 2 matches JSON shape consumed by Task 4 TS types ✓
- `colorFor(name, index)` defined and used within Task 5 only ✓
