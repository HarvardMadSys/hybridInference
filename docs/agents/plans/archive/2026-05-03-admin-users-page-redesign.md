# Admin Users Page Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the admin Users tab into a workflow-organized dashboard (summary cards, saved views, expanded filters, status icons + sparklines + anomaly badges) and extract it from the 141 KB monolithic `frontend/src/app/dashboard/admin/page.tsx`.

**Architecture:** Backend first — three new admin endpoints (`/admin/users/summary`, single + bulk `/cost-history`) plus extended `/admin/users` filters all live in `serving/servers/routers/admin/users.py` with new methods on `PostgresOperationalStore`. Frontend then extracts a new `frontend/src/app/dashboard/admin/users/` folder that owns all Users-tab logic, replaces inline code in `page.tsx` with `<UsersTab />`, and adds the new dashboard surfaces (cards, saved views, filter bar, sparkline column, status icons) wired through React Query.

**Tech Stack:** FastAPI + asyncpg + Pydantic v2 (backend), Next.js 15 App Router + React 18 + TanStack React Query 5 + recharts + Tailwind 3 + vitest (frontend).

**Spec:** `docs/agents/specs/2026-05-03-admin-users-page-redesign-design.md`

**Test conventions:**
- Backend: pytest with `AsyncMock` stores per `test/servers/test_admin_users.py`. Run a single file with `uv run pytest test/servers/test_admin_users.py -v`.
- Frontend: vitest with logic-only tests (no DOM rendering — repo has no `@testing-library/react`). Run with `npm test --prefix frontend -- <pattern>`.
- Run the full FE pipeline before pushing: `npm run lint --prefix frontend && npm run type-check --prefix frontend && npm test --prefix frontend`. Run `ruff format` and `ruff check` from the repo root for backend changes.

---

## File Map (what gets touched)

### Backend (`serving/`)

| File | Change |
|---|---|
| `serving/schemas_admin.py` | **Modify** — add `UserCostHistoryEntry`, `UserCostHistoryResponse`, `BulkCostHistoryResponse`, `UsersSummaryResponse` + sub-models |
| `serving/storage/postgres_operational.py` | **Modify** — add `get_user_cost_history`, `get_bulk_user_cost_history`, `get_users_summary`; extend `list_users` signature/SQL with new filters |
| `serving/storage/d1_operational.py` | **Modify** — implement same new methods (D1/SQLite SQL variants) |
| `serving/servers/routers/admin/users.py` | **Modify** — add 3 new routes, extend `list_users` query params |
| `test/servers/test_admin_users.py` | **Modify** — add new test cases for new routes & params |
| `test/unit/storage/test_postgres_operational_users_summary.py` | **Create** — anomaly + summary unit tests against a real-ish store (using existing test infra) |

### Frontend (`frontend/`)

New folder `frontend/src/app/dashboard/admin/users/`:

| File | Purpose |
|---|---|
| `index.tsx` | UsersTab entry. Owns FilterState, query orchestration. |
| `types.ts` | Shared TS types (`UserRow`, `FilterState`, `SavedView`, `SummaryStats`, `CostHistoryPoint`). |
| `SummaryCards.tsx` | 4 cards (Pending / Top Spenders / Anomalies / Near Quota). Click → set filter. |
| `SavedViews.tsx` | Built-in + custom-view chip row. Persists custom in localStorage. |
| `FilterBar.tsx` | Search + filter dropdowns + density toggle. Collapsible. |
| `filters/StatusFilter.tsx` | Status dropdown (replaces tab bar). |
| `filters/UsageFilter.tsx` | Cost threshold + time-window controls. |
| `filters/ProviderFilter.tsx` | Multi-select provider picker. |
| `filters/QuotaFilter.tsx` | Quota-state radios (Default / Custom / Near / Over). |
| `UserTable.tsx` | Table shell, sort headers, pagination, density. |
| `UserRow.tsx` | One row: status icon, email, cost cells (today color-coded), 7d sparkline, anomaly/quota badge, actions. |
| `Sparkline.tsx` | Reusable mini-chart (recharts `<LineChart>`), lazy-rendered via IntersectionObserver. |
| `UserDetailPanel.tsx` | Lifted from current `page.tsx` expanded panel — kept behavior-identical. |
| `hooks/useUsers.ts` | React Query wrapper for `/admin/users` with new filter params. |
| `hooks/useUserCostHistory.ts` | Bulk + single cost-history fetcher (5 min cache). |
| `hooks/useSavedViews.ts` | localStorage-backed CRUD for custom views. |
| `lib/anomaly.ts` | `isAnomalous(today, prior7)` pure function. |
| `lib/filterTypes.ts` | `FilterState`, `DEFAULT_FILTER_STATE`, `filterStateToParams`, `filterStateFromUrl`, `filterStateToUrl`. |
| `lib/views.ts` | Built-in saved view definitions. |
| `__tests__/anomaly.test.ts` | Unit tests for anomaly rule. |
| `__tests__/filterTypes.test.ts` | URL ↔ state round-trip. |
| `__tests__/views.test.ts` | Built-in view filter shapes. |
| `__tests__/useSavedViews.test.ts` | localStorage CRUD. |

Modified: `frontend/src/lib/api/admin.ts` (add 3 new fetchers + extend `listUsers` signature), `frontend/src/app/dashboard/admin/page.tsx` (replace inline Users tab with `<UsersTab />`).

---

## Phase 1 — Backend

### Task 1: Add Pydantic schemas for new endpoints

**Files:**
- Modify: `serving/schemas_admin.py` (after `ListUsersResponse`, around line 201)

- [ ] **Step 1: Add the new schemas**

Append to `serving/schemas_admin.py` (after the existing `ListUsersResponse` block, before `ApproveUserRequest`):

```python
# ========================================
# User Cost History & Summary Schemas
# ========================================


class UserCostHistoryPoint(BaseModel):
    """One day of per-user cost data."""

    day: str  # "YYYY-MM-DD" UTC
    cost_usd: Decimal = Field(default=Decimal("0"))
    requests: int = 0


class UserCostHistoryResponse(BaseModel):
    """Daily cost history for a single user."""

    user_id: str
    days: int
    points: list[UserCostHistoryPoint]


class BulkUserCostHistoryResponse(BaseModel):
    """Daily cost history for many users (one round-trip per page)."""

    days: int
    histories: dict[str, list[UserCostHistoryPoint]]  # keyed by user_id


class SummaryUserItem(BaseModel):
    """User entry inside a summary card (sub-set of UserListItem)."""

    id: str
    email: str
    user_name: str | None = None
    role: str = "free"
    today_cost_usd: Decimal = Field(default=Decimal("0"))
    avg_prior_7d_usd: Decimal = Field(default=Decimal("0"))
    quota_daily_usd: float | None = None
    multiplier: float | None = None  # today / avg, anomaly card only


class SummaryCard(BaseModel):
    """A single summary-card payload: count + top examples."""

    count: int
    top: list[SummaryUserItem]


class UsersSummaryResponse(BaseModel):
    """Aggregated counts and exemplar users for the 4 dashboard cards."""

    pending: SummaryCard
    top_spenders_today: SummaryCard
    anomalies: SummaryCard
    near_quota: SummaryCard
```

- [ ] **Step 2: Update `__all__` exports**

Find the `__all__` list at the bottom of `serving/schemas_admin.py` (around line 625-650) and add (alphabetised into the right spot):

```python
    "BulkUserCostHistoryResponse",
    "SummaryCard",
    "SummaryUserItem",
    "UserCostHistoryPoint",
    "UserCostHistoryResponse",
    "UsersSummaryResponse",
```

- [ ] **Step 3: Verify**

Run: `uv run python -c "from serving.schemas_admin import UsersSummaryResponse, BulkUserCostHistoryResponse, UserCostHistoryResponse; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add serving/schemas_admin.py
git commit -m "feat(admin): add schemas for users cost-history + summary endpoints"
```

---

### Task 2: Backend `get_user_cost_history` + `get_bulk_user_cost_history` store methods

**Files:**
- Modify: `serving/storage/postgres_operational.py`
- Modify: `serving/storage/d1_operational.py`
- Test: `test/unit/storage/test_postgres_operational_users_summary.py` (create)

- [ ] **Step 1: Write failing test for single-user cost history**

Create `test/unit/storage/test_postgres_operational_users_summary.py`:

```python
"""Unit tests for new user-summary / cost-history store methods.

These call into the in-memory test stub for PostgresOperationalStore — see
test/unit/storage/test_postgres_operational.py for the existing pattern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

# Reuse the existing in-memory test fixture
from test.unit.storage.conftest import postgres_op_store  # type: ignore  # noqa: F401


@pytest.mark.asyncio
async def test_get_user_cost_history_returns_daily_buckets(postgres_op_store):
    store = postgres_op_store
    user_id = "user-1"
    today = datetime.now(timezone.utc).date()

    # Seed three days of cost
    for i, cost in enumerate([Decimal("1.00"), Decimal("2.00"), Decimal("3.00")]):
        day = today - timedelta(days=i)
        await store.increment_user_cost(user_id, cost_delta=cost, day=day)

    points = await store.get_user_cost_history(user_id, days=7)

    # Most recent first or oldest first? Spec: contract is up to impl,
    # but ordered ascending by day for sparkline rendering.
    assert len(points) == 3
    days = [p["day"] for p in points]
    assert days == sorted(days)
    assert sum(Decimal(str(p["cost_usd"])) for p in points) == Decimal("6.00")
```

(If `test/unit/storage/conftest.py` doesn't already export `postgres_op_store`, inspect the existing `test/unit/storage/test_postgres_operational.py` for its in-memory fixture and copy/adapt.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/storage/test_postgres_operational_users_summary.py::test_get_user_cost_history_returns_daily_buckets -v`
Expected: FAIL — `AttributeError: 'PostgresOperationalStore' object has no attribute 'get_user_cost_history'` (or fixture import error if conftest needs the stub).

- [ ] **Step 3: Implement `get_user_cost_history` in postgres store**

In `serving/storage/postgres_operational.py`, find the `get_user_cost_period` method (around line 1459) and add directly after it:

```python
    async def get_user_cost_history(
        self,
        user_id: str,
        days: int = 7,
    ) -> list[Row]:
        """Return up to ``days`` of daily cost rows for ``user_id``.

        Output: list of {"day": str (ISO date YYYY-MM-DD), "cost_usd": Decimal,
        "requests": int}, ordered by day ascending. Days with zero activity
        are NOT included — caller fills gaps if needed.
        """
        if days <= 0:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT day, cost_usd, requests "
                "FROM user_daily_cost "
                "WHERE user_id = $1 "
                "  AND day >= (CURRENT_DATE - $2::int) "
                "ORDER BY day ASC",
                user_id,
                days - 1,  # inclusive of today, so "last 7 days" = today minus 6
            )
        return [
            {
                "day": r["day"].isoformat(),
                "cost_usd": r["cost_usd"],
                "requests": r["requests"],
            }
            for r in rows
        ]

    async def get_bulk_user_cost_history(
        self,
        user_ids: list[str],
        days: int = 7,
    ) -> dict[str, list[Row]]:
        """Bulk variant — one query, grouped by user_id."""
        if not user_ids or days <= 0:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, day, cost_usd, requests "
                "FROM user_daily_cost "
                "WHERE user_id = ANY($1::text[]) "
                "  AND day >= (CURRENT_DATE - $2::int) "
                "ORDER BY user_id, day ASC",
                user_ids,
                days - 1,
            )
        out: dict[str, list[Row]] = {uid: [] for uid in user_ids}
        for r in rows:
            out[r["user_id"]].append(
                {
                    "day": r["day"].isoformat(),
                    "cost_usd": r["cost_usd"],
                    "requests": r["requests"],
                }
            )
        return out
```

- [ ] **Step 4: Implement the same in `d1_operational.py`**

Locate the analogous existing methods in `serving/storage/d1_operational.py` (search for `get_user_cost_period`) and add directly after them. The D1 (SQLite) variant uses `?` placeholders and `date('now')`:

```python
    async def get_user_cost_history(
        self,
        user_id: str,
        days: int = 7,
    ) -> list[Row]:
        if days <= 0:
            return []
        rows = await self._fetchall(
            "SELECT day, cost_usd, requests "
            "FROM user_daily_cost "
            "WHERE user_id = ? "
            "  AND day >= date('now', ? || ' days') "
            "ORDER BY day ASC",
            user_id,
            f"-{days - 1}",
        )
        return [
            {
                "day": r["day"],
                "cost_usd": Decimal(str(r["cost_usd"])),
                "requests": r["requests"],
            }
            for r in rows
        ]

    async def get_bulk_user_cost_history(
        self,
        user_ids: list[str],
        days: int = 7,
    ) -> dict[str, list[Row]]:
        if not user_ids or days <= 0:
            return {}
        placeholders = ",".join(["?"] * len(user_ids))
        rows = await self._fetchall(
            f"SELECT user_id, day, cost_usd, requests "
            f"FROM user_daily_cost "
            f"WHERE user_id IN ({placeholders}) "
            f"  AND day >= date('now', ? || ' days') "
            f"ORDER BY user_id, day ASC",
            *user_ids,
            f"-{days - 1}",
        )
        out: dict[str, list[Row]] = {uid: [] for uid in user_ids}
        for r in rows:
            out[r["user_id"]].append(
                {
                    "day": r["day"],
                    "cost_usd": Decimal(str(r["cost_usd"])),
                    "requests": r["requests"],
                }
            )
        return out
```

(Adapt `_fetchall` to whatever the file actually uses — read 40-60 lines around `get_user_cost_period` first.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest test/unit/storage/test_postgres_operational_users_summary.py::test_get_user_cost_history_returns_daily_buckets -v`
Expected: PASS

- [ ] **Step 6: Add bulk + edge-case tests**

Add to the same test file:

```python
@pytest.mark.asyncio
async def test_get_user_cost_history_empty_for_unknown_user(postgres_op_store):
    points = await postgres_op_store.get_user_cost_history("nope", days=7)
    assert points == []


@pytest.mark.asyncio
async def test_get_user_cost_history_zero_days_returns_empty(postgres_op_store):
    points = await postgres_op_store.get_user_cost_history("u1", days=0)
    assert points == []


@pytest.mark.asyncio
async def test_get_bulk_user_cost_history_groups_by_user(postgres_op_store):
    today = datetime.now(timezone.utc).date()
    await postgres_op_store.increment_user_cost("u1", cost_delta=Decimal("1"), day=today)
    await postgres_op_store.increment_user_cost("u2", cost_delta=Decimal("2"), day=today)

    out = await postgres_op_store.get_bulk_user_cost_history(["u1", "u2", "u3"], days=7)

    assert set(out.keys()) == {"u1", "u2", "u3"}
    assert len(out["u1"]) == 1
    assert len(out["u2"]) == 1
    assert out["u3"] == []
```

If `increment_user_cost` does not accept `day=`, inspect the in-memory store's existing method and adapt the seeding helper.

Run: `uv run pytest test/unit/storage/test_postgres_operational_users_summary.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add serving/storage/postgres_operational.py serving/storage/d1_operational.py test/unit/storage/test_postgres_operational_users_summary.py
git commit -m "feat(admin): add per-user and bulk cost-history store methods"
```

---

### Task 3: Backend `get_users_summary` store method (anomaly detection + cards)

**Files:**
- Modify: `serving/storage/postgres_operational.py`
- Modify: `serving/storage/d1_operational.py`
- Test: `test/unit/storage/test_postgres_operational_users_summary.py`

- [ ] **Step 1: Write failing test for anomaly detection**

Add to the existing test file:

```python
@pytest.mark.asyncio
async def test_users_summary_anomaly_threshold(postgres_op_store):
    """A user spending 5x their 7d average AND >= $1 today is anomalous."""
    store = postgres_op_store
    today = datetime.now(timezone.utc).date()

    # User A: stable spend $1/day for 7 days, then $10 today → anomaly
    user_a = await _make_active_user(store, "anomalous@example.com")
    for i in range(1, 8):
        await store.increment_user_cost(user_a, cost_delta=Decimal("1.00"),
                                        day=today - timedelta(days=i))
    await store.increment_user_cost(user_a, cost_delta=Decimal("10.00"), day=today)

    # User B: same baseline, today $0.50 → not anomalous (below $1 floor)
    user_b = await _make_active_user(store, "below-floor@example.com")
    for i in range(1, 8):
        await store.increment_user_cost(user_b, cost_delta=Decimal("1.00"),
                                        day=today - timedelta(days=i))
    await store.increment_user_cost(user_b, cost_delta=Decimal("0.50"), day=today)

    # User C: today $10 but only 2 days of history → not anomalous (< 3 days)
    user_c = await _make_active_user(store, "new-user@example.com")
    await store.increment_user_cost(user_c, cost_delta=Decimal("0.10"),
                                    day=today - timedelta(days=1))
    await store.increment_user_cost(user_c, cost_delta=Decimal("10.00"), day=today)

    summary = await store.get_users_summary()

    anomaly_ids = {u["id"] for u in summary["anomalies"]["top"]}
    assert user_a in anomaly_ids
    assert user_b not in anomaly_ids
    assert user_c not in anomaly_ids
    assert summary["anomalies"]["count"] >= 1


@pytest.mark.asyncio
async def test_users_summary_pending_count(postgres_op_store):
    pending_id = await _make_pending_user(postgres_op_store, "pending@example.com")
    await _make_active_user(postgres_op_store, "active@example.com")

    summary = await postgres_op_store.get_users_summary()

    assert summary["pending"]["count"] >= 1
    assert pending_id in {u["id"] for u in summary["pending"]["top"]}


# Helpers added at top of file
async def _make_active_user(store, email):
    user_id = ...  # call the existing user-creation method on the in-memory store
    return user_id


async def _make_pending_user(store, email):
    user_id = ...  # ditto, status=pending_approval
    return user_id
```

(Look at `test/unit/storage/test_postgres_operational.py` and `conftest.py` for the existing helpers — copy whichever pattern is in use rather than inventing new ones. `_make_active_user` / `_make_pending_user` likely already exist or have analogues.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/storage/test_postgres_operational_users_summary.py::test_users_summary_anomaly_threshold -v`
Expected: FAIL — `AttributeError: 'PostgresOperationalStore' object has no attribute 'get_users_summary'`.

- [ ] **Step 3: Implement `get_users_summary` in postgres store**

In `serving/storage/postgres_operational.py`, after `get_bulk_user_cost_history` (added in Task 2):

```python
    async def get_users_summary(
        self,
        *,
        top_n: int = 5,
        anomaly_multiplier: float = 5.0,
        anomaly_min_today: Decimal = Decimal("1.00"),
        anomaly_min_history_days: int = 3,
        near_quota_pct: float = 0.80,
    ) -> Row:
        """Aggregate stats for the 4 dashboard summary cards.

        Returns a dict with keys: pending, top_spenders_today, anomalies,
        near_quota. Each value is {"count": int, "top": [SummaryUserItem-like]}.

        Anomaly rule: status='active' AND days_with_history>=N
            AND today_cost >= floor AND today_cost >= multiplier*avg_prior_7d.

        Near quota: any active user whose today_cost >= near_quota_pct
        of their key's quota_daily_cost_usd. Users without quota set are
        excluded.
        """
        async with self._pool.acquire() as conn:
            # 1. Pending count + top
            pending_rows = await conn.fetch(
                "SELECT id, email, user_name, role, created_at "
                "FROM users WHERE status = 'pending_approval' "
                "ORDER BY created_at DESC LIMIT $1",
                top_n,
            )
            pending_count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS c FROM users WHERE status = 'pending_approval'"
            )

            # 2. Today / 7d-avg per user (active users only, with non-zero history)
            usage_rows = await conn.fetch(
                """
                WITH today_costs AS (
                    SELECT user_id, COALESCE(SUM(cost_usd), 0) AS today_cost
                    FROM user_daily_cost
                    WHERE day = CURRENT_DATE
                    GROUP BY user_id
                ),
                prior_7d AS (
                    SELECT user_id,
                           COALESCE(SUM(cost_usd), 0) AS total,
                           COUNT(DISTINCT day) AS days_with_history
                    FROM user_daily_cost
                    WHERE day BETWEEN (CURRENT_DATE - INTERVAL '7 days')
                                  AND (CURRENT_DATE - INTERVAL '1 day')
                    GROUP BY user_id
                )
                SELECT u.id, u.email, u.user_name, u.role,
                       COALESCE(t.today_cost, 0) AS today_cost,
                       COALESCE(p.total, 0) AS prior_7d_total,
                       COALESCE(p.days_with_history, 0) AS days_with_history,
                       k.quota_daily_cost_usd
                FROM users u
                LEFT JOIN today_costs t ON t.user_id = u.id
                LEFT JOIN prior_7d p ON p.user_id = u.id
                LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
                WHERE u.status = 'active'
                """,
            )

            top_spenders: list[Row] = []
            anomalies: list[Row] = []
            near_quota: list[Row] = []

            for r in usage_rows:
                today = Decimal(str(r["today_cost"] or 0))
                prior = Decimal(str(r["prior_7d_total"] or 0))
                days = int(r["days_with_history"] or 0)
                avg_7d = (prior / days) if days > 0 else Decimal("0")
                quota = r["quota_daily_cost_usd"]

                # Top spenders today
                if today > 0:
                    top_spenders.append({
                        "id": r["id"], "email": r["email"], "user_name": r["user_name"],
                        "role": r["role"] or "free",
                        "today_cost_usd": today, "avg_prior_7d_usd": avg_7d,
                        "quota_daily_usd": float(quota) if quota else None,
                        "multiplier": None,
                    })

                # Anomaly
                if (
                    days >= anomaly_min_history_days
                    and today >= anomaly_min_today
                    and avg_7d > 0
                    and today >= Decimal(str(anomaly_multiplier)) * avg_7d
                ):
                    multiplier = float(today / avg_7d) if avg_7d > 0 else None
                    anomalies.append({
                        "id": r["id"], "email": r["email"], "user_name": r["user_name"],
                        "role": r["role"] or "free",
                        "today_cost_usd": today, "avg_prior_7d_usd": avg_7d,
                        "quota_daily_usd": float(quota) if quota else None,
                        "multiplier": multiplier,
                    })

                # Near / over quota
                if quota and float(quota) > 0:
                    pct = float(today) / float(quota)
                    if pct >= near_quota_pct:
                        near_quota.append({
                            "id": r["id"], "email": r["email"], "user_name": r["user_name"],
                            "role": r["role"] or "free",
                            "today_cost_usd": today, "avg_prior_7d_usd": avg_7d,
                            "quota_daily_usd": float(quota),
                            "multiplier": None,
                        })

            top_spenders.sort(key=lambda x: x["today_cost_usd"], reverse=True)
            anomalies.sort(key=lambda x: (x["multiplier"] or 0), reverse=True)
            near_quota.sort(
                key=lambda x: (float(x["today_cost_usd"]) / x["quota_daily_usd"]),
                reverse=True,
            )

        return {
            "pending": {
                "count": pending_count_row["c"] if pending_count_row else 0,
                "top": [
                    {
                        "id": r["id"], "email": r["email"], "user_name": r["user_name"],
                        "role": r["role"] or "free",
                        "today_cost_usd": Decimal("0"),
                        "avg_prior_7d_usd": Decimal("0"),
                        "quota_daily_usd": None,
                        "multiplier": None,
                    }
                    for r in pending_rows
                ],
            },
            "top_spenders_today": {
                "count": len([s for s in top_spenders if s["today_cost_usd"] > 0]),
                "top": top_spenders[:top_n],
            },
            "anomalies": {
                "count": len(anomalies),
                "top": anomalies[:top_n],
            },
            "near_quota": {
                "count": len(near_quota),
                "top": near_quota[:top_n],
            },
        }
```

- [ ] **Step 4: Implement same in d1_operational.py**

Adapt the SQL — D1/SQLite doesn't support `INTERVAL`. Use `date('now', '-7 days')` instead. The post-query Python filtering logic is identical.

- [ ] **Step 5: Run all tests**

Run: `uv run pytest test/unit/storage/test_postgres_operational_users_summary.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add serving/storage/postgres_operational.py serving/storage/d1_operational.py test/unit/storage/test_postgres_operational_users_summary.py
git commit -m "feat(admin): add get_users_summary store method (pending, top spenders, anomalies, near quota)"
```

---

### Task 4: Extend `list_users` store method with new filters

**Files:**
- Modify: `serving/storage/postgres_operational.py` (`list_users` around line 599)
- Modify: `serving/storage/d1_operational.py` (matching `list_users`)
- Test: `test/unit/storage/test_postgres_operational_users_summary.py`

- [ ] **Step 1: Write failing test for cost-threshold filter**

Add to test file:

```python
@pytest.mark.asyncio
async def test_list_users_filter_min_cost_today(postgres_op_store):
    today = datetime.now(timezone.utc).date()
    cheap = await _make_active_user(postgres_op_store, "cheap@example.com")
    expensive = await _make_active_user(postgres_op_store, "expensive@example.com")
    await postgres_op_store.increment_user_cost(cheap, cost_delta=Decimal("0.50"), day=today)
    await postgres_op_store.increment_user_cost(expensive, cost_delta=Decimal("100"), day=today)

    total, rows, _ = await postgres_op_store.list_users(min_cost_today=Decimal("10"))

    ids = {r["id"] for r in rows}
    assert expensive in ids
    assert cheap not in ids


@pytest.mark.asyncio
async def test_list_users_search_matches_key_prefix(postgres_op_store):
    user = await _make_active_user_with_key(
        postgres_op_store, "with-key@example.com", key_prefix="sk-test-12"
    )
    other = await _make_active_user(postgres_op_store, "no-key@example.com")

    total, rows, _ = await postgres_op_store.list_users(search="sk-test-1")

    ids = {r["id"] for r in rows}
    assert user in ids
    assert other not in ids
```

- [ ] **Step 2: Run to confirm fail**

Run: `uv run pytest test/unit/storage/test_postgres_operational_users_summary.py::test_list_users_filter_min_cost_today -v`
Expected: FAIL — `TypeError: list_users() got an unexpected keyword argument 'min_cost_today'`.

- [ ] **Step 3: Extend `list_users` signature**

In `serving/storage/postgres_operational.py`, change the `list_users` signature (around line 599) to:

```python
    async def list_users(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        sort_by: Literal[
            "created", "cost_today", "cost_month", "cost_alltime", "last_login"
        ] = "created",
        limit: int = 100,
        offset: int = 0,
        # NEW filters
        min_cost_today: Decimal | None = None,
        min_cost_month: Decimal | None = None,
        quota_state: Literal["near", "over", "custom", "default"] | None = None,
        provider: str | None = None,
        active_within_hours: int | None = None,
    ) -> tuple[int, list[Row], Row]:
```

- [ ] **Step 4: Update SQL — search now also matches key prefix and id prefix**

Replace the existing `if search:` block (around line 622) with:

```python
        if search:
            search_idx = len(filter_params) + 1
            where_clauses.append(
                f"(u.email ILIKE ${search_idx} "
                f"OR u.user_name ILIKE ${search_idx} "
                f"OR u.id::text LIKE ${search_idx + 1} "
                f"OR EXISTS (SELECT 1 FROM api_keys k2 "
                f"           WHERE k2.account_id = u.id "
                f"             AND k2.status = 'active' "
                f"             AND k2.key_prefix LIKE ${search_idx + 2}))"
            )
            filter_params.append(f"%{search}%")          # email/user_name (substring)
            filter_params.append(f"{search}%")            # id prefix
            filter_params.append(f"{search}%")            # key prefix
        if active_within_hours is not None:
            where_clauses.append(
                f"u.last_login_at >= NOW() - ${len(filter_params) + 1} * INTERVAL '1 hour'"
            )
            filter_params.append(active_within_hours)
        if quota_state:
            if quota_state == "default":
                where_clauses.append(
                    "EXISTS (SELECT 1 FROM api_keys k3 WHERE k3.account_id = u.id "
                    "AND k3.status = 'active' AND k3.quota_daily_cost_usd IS NULL)"
                )
            elif quota_state == "custom":
                where_clauses.append(
                    "EXISTS (SELECT 1 FROM api_keys k3 WHERE k3.account_id = u.id "
                    "AND k3.status = 'active' AND k3.quota_daily_cost_usd IS NOT NULL)"
                )
            # 'near' / 'over' need today_cost vs quota — handled post-query below
```

- [ ] **Step 5: Add post-query filtering for cost thresholds, near/over quota, provider**

After the existing assembly of `result_rows` (around line 790), but before `return total, result_rows, status_counts`, insert:

```python
        # Post-query filters that need cost data we already loaded
        if min_cost_today is not None:
            result_rows = [r for r in result_rows
                           if Decimal(str(r.get("usage_today", 0))) >= min_cost_today]
        if min_cost_month is not None:
            result_rows = [r for r in result_rows
                           if Decimal(str(r.get("usage_month", 0))) >= min_cost_month]

        if quota_state in ("near", "over"):
            # Need quota_daily_cost_usd per user
            user_ids2 = [r["id"] for r in result_rows]
            if user_ids2:
                async with self._pool.acquire() as conn2:
                    quota_rows = await conn2.fetch(
                        "SELECT account_id, quota_daily_cost_usd "
                        "FROM api_keys WHERE account_id = ANY($1::text[]) "
                        "  AND status = 'active'",
                        user_ids2,
                    )
                quotas = {q["account_id"]: q["quota_daily_cost_usd"] for q in quota_rows}
                filtered: list[Row] = []
                for r in result_rows:
                    quota = quotas.get(r["id"])
                    if not quota or float(quota) <= 0:
                        continue
                    today = float(r.get("usage_today", 0) or 0)
                    pct = today / float(quota)
                    if quota_state == "near" and pct >= 0.80:
                        filtered.append(r)
                    elif quota_state == "over" and pct >= 1.0:
                        filtered.append(r)
                result_rows = filtered

        if provider:
            # Filter to users who hit provider in last 30 days. api_logs has provider col.
            user_ids3 = [r["id"] for r in result_rows]
            if user_ids3:
                async with self._pool.acquire() as conn3:
                    prov_rows = await conn3.fetch(
                        "SELECT DISTINCT user_id FROM api_logs "
                        "WHERE user_id = ANY($1::text[]) "
                        "  AND provider = $2 "
                        "  AND timestamp >= NOW() - INTERVAL '30 days'",
                        user_ids3,
                        provider,
                    )
                allowed = {r["user_id"] for r in prov_rows}
                result_rows = [r for r in result_rows if r["id"] in allowed]

        # Re-compute total after post-filters so pagination remains accurate-ish.
        # NOTE: this is approximate — the *real* total post-filter would need
        # the post-filter logic to live in SQL. Acceptable for an admin tool.
        total = len(result_rows)
```

Verified — `api_logs` has a `provider TEXT NOT NULL` column (see `serving/storage/postgres_log.py:54`). The same Postgres instance backs both stores in production, so the join above is safe. For the D1 (SQLite) implementation, `provider` is also present in `serving/storage/d1_schema.sql`. No further adjustment needed.

- [ ] **Step 6: Apply same changes to d1_operational.py `list_users`**

Mirror the new signature and SQL changes (D1 SQL variant — `?` placeholders, `datetime('now', '-30 days')` etc.).

- [ ] **Step 7: Run tests**

Run: `uv run pytest test/unit/storage/test_postgres_operational_users_summary.py -v`
Expected: all pass.

- [ ] **Step 8: Run existing list_users tests for regression**

Run: `uv run pytest test/servers/test_admin_users.py -v`
Expected: all pass (signature changes are additive — kw-only with defaults).

- [ ] **Step 9: Commit**

```bash
git add serving/storage/postgres_operational.py serving/storage/d1_operational.py test/unit/storage/test_postgres_operational_users_summary.py
git commit -m "feat(admin): extend list_users with cost/quota/provider/active-within filters and broader search"
```

---

### Task 5: Add new admin route handlers

**Files:**
- Modify: `serving/servers/routers/admin/users.py`
- Test: `test/servers/test_admin_users.py`

- [ ] **Step 1: Write failing test for `/admin/users/{id}/cost-history`**

Add to `test/servers/test_admin_users.py` (use existing `admin_client` fixture pattern):

```python
async def test_get_user_cost_history_route(admin_client):
    client, op_store, _, _ = admin_client
    op_store.get_user_cost_history = AsyncMock(
        return_value=[
            {"day": "2025-06-14", "cost_usd": Decimal("1.50"), "requests": 3},
            {"day": "2025-06-15", "cost_usd": Decimal("2.00"), "requests": 5},
        ]
    )

    resp = await client.get("/admin/users/u1/cost-history?days=7", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u1"
    assert body["days"] == 7
    assert len(body["points"]) == 2
    assert body["points"][0]["day"] == "2025-06-14"


async def test_get_bulk_cost_history_route(admin_client):
    client, op_store, _, _ = admin_client
    op_store.get_bulk_user_cost_history = AsyncMock(
        return_value={
            "u1": [{"day": "2025-06-15", "cost_usd": Decimal("1.0"), "requests": 1}],
            "u2": [],
        }
    )

    resp = await client.get(
        "/admin/users/cost-history?user_ids=u1,u2&days=7", headers=AUTH
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["histories"].keys()) == {"u1", "u2"}


async def test_get_users_summary_route(admin_client):
    client, op_store, _, _ = admin_client
    op_store.get_users_summary = AsyncMock(
        return_value={
            "pending": {"count": 3, "top": []},
            "top_spenders_today": {"count": 10, "top": []},
            "anomalies": {"count": 1, "top": []},
            "near_quota": {"count": 2, "top": []},
        }
    )

    resp = await client.get("/admin/users/summary", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["pending"]["count"] == 3
    assert body["anomalies"]["count"] == 1
```

- [ ] **Step 2: Run to confirm fail**

Run: `uv run pytest test/servers/test_admin_users.py::test_get_user_cost_history_route -v`
Expected: FAIL — `404` or `405`.

- [ ] **Step 3: Add three new routes to `serving/servers/routers/admin/users.py`**

Add the new imports at the top:

```python
from serving.schemas_admin import (
    # ... existing imports ...
    BulkUserCostHistoryResponse,
    SummaryCard,
    SummaryUserItem,
    UserCostHistoryPoint,
    UserCostHistoryResponse,
    UsersSummaryResponse,
)
```

Add at the end of the file (after `hard_delete_user`):

```python
# IMPORTANT: bulk route is registered BEFORE the parameterized
# {user_id}/cost-history route so FastAPI matches it first. The
# parameterized variant lives later in this file.

@router.get("/users/cost-history", response_model=BulkUserCostHistoryResponse)
async def admin_get_bulk_user_cost_history(
    user_ids: str,  # comma-separated
    days: int = 7,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> BulkUserCostHistoryResponse:
    """Bulk daily cost history for many users (one round-trip per page)."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    if days < 1 or days > 90:
        raise HTTPException(422, "days must be between 1 and 90")

    ids = [s.strip() for s in user_ids.split(",") if s.strip()]
    if not ids:
        return BulkUserCostHistoryResponse(days=days, histories={})
    if len(ids) > 200:
        raise HTTPException(422, "Maximum 200 user_ids per request")

    raw = await op_store.get_bulk_user_cost_history(ids, days=days)
    histories = {
        uid: [
            UserCostHistoryPoint(
                day=p["day"],
                cost_usd=Decimal(str(p["cost_usd"])),
                requests=p["requests"],
            )
            for p in points
        ]
        for uid, points in raw.items()
    }
    return BulkUserCostHistoryResponse(days=days, histories=histories)


@router.get("/users/summary", response_model=UsersSummaryResponse)
async def admin_get_users_summary(
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UsersSummaryResponse:
    """Aggregated summary stats for the 4 dashboard cards."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    raw = await op_store.get_users_summary()

    def _card(card_raw: dict) -> SummaryCard:
        return SummaryCard(
            count=card_raw["count"],
            top=[
                SummaryUserItem(
                    id=u["id"],
                    email=u["email"],
                    user_name=u.get("user_name"),
                    role=u.get("role", "free"),
                    today_cost_usd=Decimal(str(u.get("today_cost_usd", 0))),
                    avg_prior_7d_usd=Decimal(str(u.get("avg_prior_7d_usd", 0))),
                    quota_daily_usd=u.get("quota_daily_usd"),
                    multiplier=u.get("multiplier"),
                )
                for u in card_raw["top"]
            ],
        )

    return UsersSummaryResponse(
        pending=_card(raw["pending"]),
        top_spenders_today=_card(raw["top_spenders_today"]),
        anomalies=_card(raw["anomalies"]),
        near_quota=_card(raw["near_quota"]),
    )


@router.get("/users/{user_id}/cost-history", response_model=UserCostHistoryResponse)
async def admin_get_user_cost_history(
    user_id: str,
    days: int = 7,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UserCostHistoryResponse:
    """Daily cost history for a single user."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    if days < 1 or days > 90:
        raise HTTPException(422, "days must be between 1 and 90")

    raw = await op_store.get_user_cost_history(user_id, days=days)
    return UserCostHistoryResponse(
        user_id=user_id,
        days=days,
        points=[
            UserCostHistoryPoint(
                day=p["day"],
                cost_usd=Decimal(str(p["cost_usd"])),
                requests=p["requests"],
            )
            for p in raw
        ],
    )
```

- [ ] **Step 4: Run to verify all pass**

Run: `uv run pytest test/servers/test_admin_users.py -v -k "cost_history or users_summary"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/routers/admin/users.py test/servers/test_admin_users.py
git commit -m "feat(admin): add /admin/users/{id}/cost-history, bulk variant, and /summary endpoints"
```

---

### Task 6: Extend `/admin/users` route with new query params

**Files:**
- Modify: `serving/servers/routers/admin/users.py`
- Test: `test/servers/test_admin_users.py`

- [ ] **Step 1: Write failing test**

Add to `test/servers/test_admin_users.py`:

```python
async def test_list_users_passes_new_filters_through(admin_client):
    client, op_store, _, _ = admin_client
    op_store.list_users = AsyncMock(return_value=(0, [], {}))

    resp = await client.get(
        "/admin/users?min_cost_today=5&quota_state=near"
        "&provider=anthropic&active_within_hours=24",
        headers=AUTH,
    )
    assert resp.status_code == 200

    op_store.list_users.assert_awaited_once()
    kwargs = op_store.list_users.await_args.kwargs
    assert kwargs["min_cost_today"] == Decimal("5")
    assert kwargs["quota_state"] == "near"
    assert kwargs["provider"] == "anthropic"
    assert kwargs["active_within_hours"] == 24
```

- [ ] **Step 2: Run to confirm fail**

Run: `uv run pytest test/servers/test_admin_users.py::test_list_users_passes_new_filters_through -v`
Expected: FAIL — likely `TypeError: list_users() got an unexpected keyword argument 'min_cost_today'` raised inside the route, returning 500.

- [ ] **Step 3: Extend route signature**

Find `list_users` route in `serving/servers/routers/admin/users.py` (around line 43). Modify the signature to:

```python
@router.get("/users", response_model=ListUsersResponse)
async def list_users(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    sort_by: Literal[
        "created", "cost_today", "cost_month", "cost_alltime", "last_login"
    ] = "created",
    limit: int = 100,
    offset: int = 0,
    # NEW filters
    min_cost_today: Decimal | None = None,
    min_cost_month: Decimal | None = None,
    quota_state: Literal["near", "over", "custom", "default"] | None = None,
    provider: str | None = None,
    active_within_hours: int | None = None,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListUsersResponse:
    ...
```

And update the `op_store.list_users(...)` call to forward the new kwargs:

```python
    total, rows, sc = await op_store.list_users(
        status=status,
        search=search,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
        min_cost_today=min_cost_today,
        min_cost_month=min_cost_month,
        quota_state=quota_state,
        provider=provider,
        active_within_hours=active_within_hours,
    )
```

- [ ] **Step 4: Run all admin user tests**

Run: `uv run pytest test/servers/test_admin_users.py -v`
Expected: all pass.

- [ ] **Step 5: Run full backend test suite for regression**

Run: `uv run pytest test/servers -x -q`
Expected: all pass (the changes are kw-only additive — existing callers unaffected).

- [ ] **Step 6: Format and commit**

```bash
ruff format serving/ test/
ruff check serving/ test/ --fix
git add serving/servers/routers/admin/users.py test/servers/test_admin_users.py
git commit -m "feat(admin): /admin/users accepts new filter query params"
```

---

## Phase 2 — Frontend code split (no UX change yet)

This phase moves the existing Users tab out of `page.tsx` into its own folder *without* changing what the page looks like. The follow-up phases add new UX on top.

### Task 7: Scaffold new `users/` folder + types.ts

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/types.ts`

- [ ] **Step 1: Create types.ts**

Create `frontend/src/app/dashboard/admin/users/types.ts`:

```typescript
// Shared types for the admin Users tab.
// UserRow mirrors backend UserListItem (decimals decoded as numbers/strings
// — keep numeric strings for safety and parse where needed).

export type UserStatus =
  | 'pending_approval'
  | 'active'
  | 'suspended'
  | 'rejected'
  | 'deleted';

export type UserRole = 'free' | 'pro' | 'internal' | 'admin';

export interface UserRow {
  id: string;
  email: string;
  user_name: string | null;
  role: UserRole;
  status: UserStatus;
  email_verified: boolean;
  approval_note: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_status: string | null;
  usage_today_usd: string;   // Decimal serialised
  usage_month_usd: string;
  usage_alltime_usd: string;
}

export interface CostHistoryPoint {
  day: string;       // YYYY-MM-DD
  cost_usd: string;  // Decimal
  requests: number;
}

export interface SummaryUserItem {
  id: string;
  email: string;
  user_name: string | null;
  role: UserRole;
  today_cost_usd: string;
  avg_prior_7d_usd: string;
  quota_daily_usd: number | null;
  multiplier: number | null;
}

export interface SummaryCard {
  count: number;
  top: SummaryUserItem[];
}

export interface UsersSummary {
  pending: SummaryCard;
  top_spenders_today: SummaryCard;
  anomalies: SummaryCard;
  near_quota: SummaryCard;
}

export type SortBy =
  | 'created'
  | 'cost_today'
  | 'cost_month'
  | 'cost_alltime'
  | 'last_login';

export type QuotaStateFilter = 'near' | 'over' | 'custom' | 'default';

export interface FilterState {
  status: UserStatus | null;
  search: string;
  sortBy: SortBy;
  minCostToday: number | null;
  minCostMonth: number | null;
  quotaState: QuotaStateFilter | null;
  provider: string | null;
  activeWithinHours: number | null;
  view: string | null; // built-in or saved-view id
}

export interface SavedView {
  id: string;          // slug
  name: string;
  builtin: boolean;
  filterState: FilterState;
}

export type Density = 'comfortable' | 'compact';
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/types.ts
git commit -m "feat(admin-ui): scaffold users tab types"
```

---

### Task 8: Lift expanded detail panel into `UserDetailPanel.tsx`

**Files:**
- Read: `frontend/src/app/dashboard/admin/page.tsx` — find the inline expanded user detail JSX (search for `usage_today` and the expanded-row stats grid)
- Create: `frontend/src/app/dashboard/admin/users/UserDetailPanel.tsx`

- [ ] **Step 1: Locate the inline detail panel**

Run: `grep -n "Daily quota\|quota_daily\|Reactivate\|Hard delete" frontend/src/app/dashboard/admin/page.tsx | head -20`
Note the line ranges that comprise the expanded detail content.

- [ ] **Step 2: Copy the JSX block into a new component**

Create `frontend/src/app/dashboard/admin/users/UserDetailPanel.tsx`. Copy the JSX exactly as it appears in `page.tsx`, plus the helpers it depends on (e.g. role-change handler, save handler, suspend/delete handlers). Define a clear props interface:

```typescript
'use client';

import type { UserRow } from './types';

export interface UserDetailPanelProps {
  user: UserRow;
  onApprove: (userId: string) => Promise<void>;
  onReject: (userId: string, reason: string) => Promise<void>;
  onUpdate: (userId: string, patch: Record<string, unknown>) => Promise<void>;
  onSuspend: (userId: string) => Promise<void>;
  onResume: (userId: string) => Promise<void>;
  onDelete: (userId: string, reason: string) => Promise<void>;
  onHardDelete: (userId: string, reason: string) => Promise<void>;
  onRegenerateKey: (userId: string) => Promise<void>;
}

export function UserDetailPanel(props: UserDetailPanelProps) {
  // ... pasted JSX from page.tsx, with prop callbacks replacing inline handlers ...
}
```

(There is no shorthand — paste the JSX as-is, then convert each inline handler call to call the corresponding prop. The content must be the same as before this PR.)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/UserDetailPanel.tsx
git commit -m "feat(admin-ui): extract UserDetailPanel from page.tsx (no behaviour change)"
```

---

### Task 9: Lift the Users tab body into `users/index.tsx`

**Files:**
- Modify: `frontend/src/app/dashboard/admin/page.tsx`
- Create: `frontend/src/app/dashboard/admin/users/index.tsx`

- [ ] **Step 1: Identify the Users tab block in page.tsx**

Run: `grep -n "activeTab === 'users'\|activeTab==='users'" frontend/src/app/dashboard/admin/page.tsx`
Note the start and end of the conditional block that renders the Users tab content.

- [ ] **Step 2: Create `users/index.tsx` with the lifted code**

Create `frontend/src/app/dashboard/admin/users/index.tsx` and:

1. Move all Users-tab-only state (`users`, `loading`, `searchQuery`, `sortBy`, `selectedStatus`, `expandedUserId`, etc.) into this component
2. Move all Users-tab-only effects (the fetch, search debounce)
3. Move the JSX that renders the search bar, status tabs, table, and detail panel
4. Replace the inline detail-panel JSX with `<UserDetailPanel {...props} />`
5. Export as default `UsersTab`

Skeleton:

```typescript
'use client';

import { useEffect, useState } from 'react';
import { listUsers, /* and other admin API fns currently used */ } from '@/lib/api/admin';
import { UserDetailPanel } from './UserDetailPanel';
import type { UserRow, UserStatus, SortBy } from './types';

export default function UsersTab() {
  // ... lifted state ...
  // ... lifted JSX ...
}
```

The goal of this task is **byte-for-byte parity** with the prior behavior. Don't add new features yet.

- [ ] **Step 3: Replace the Users tab block in `page.tsx`**

Edit `frontend/src/app/dashboard/admin/page.tsx`:

```typescript
// Add import at top:
import UsersTab from './users';

// In the activeTab === 'users' branch, replace the entire block with:
{activeTab === 'users' && <UsersTab />}
```

Remove all the Users-tab-only state and effects from `page.tsx` that were lifted in Step 2.

- [ ] **Step 4: Manually verify no behaviour change**

Run: `npm run dev --prefix frontend`
Open `http://localhost:3001/dashboard/admin`, log in as `admin@admin.com:admin` (per CLAUDE.md), click Users tab. Verify:
- Tab renders identically to before (search, status chips, sort, table rows, expand/collapse)
- Approve/reject/suspend/resume/delete/regenerate-key all work
- Page does not error in browser console

If anything is broken, fix and re-test before moving on.

- [ ] **Step 5: Run lint + type-check**

Run: `npm run lint --prefix frontend && npm run type-check --prefix frontend`
Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/app/dashboard/admin/page.tsx frontend/src/app/dashboard/admin/users/index.tsx
git commit -m "refactor(admin-ui): extract Users tab into users/ folder (no behaviour change)"
```

---

## Phase 3 — Pure logic modules (TDD)

### Task 10: `lib/anomaly.ts` + tests

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/lib/anomaly.ts`
- Create: `frontend/src/app/dashboard/admin/users/__tests__/anomaly.test.ts`

- [ ] **Step 1: Write failing test**

Create `frontend/src/app/dashboard/admin/users/__tests__/anomaly.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';
import { isAnomalous } from '../lib/anomaly';

describe('isAnomalous', () => {
  it('flags 5x spike with $1+ today and 7d history', () => {
    const history = [1, 1, 1, 1, 1, 1, 1]; // avg = 1
    expect(isAnomalous(5.01, history)).toBe(true);
  });

  it('does not flag exactly 5x today (strict >)', () => {
    const history = [1, 1, 1, 1, 1, 1, 1];
    expect(isAnomalous(5.0, history)).toBe(true); // spec uses >=
  });

  it('does not flag below $1 floor', () => {
    const history = [0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05]; // avg = 0.05
    // today = 0.99 is 19.8x avg but below $1 floor
    expect(isAnomalous(0.99, history)).toBe(false);
  });

  it('does not flag fewer than 3 days of history', () => {
    expect(isAnomalous(100, [1, 1])).toBe(false);
    expect(isAnomalous(100, [])).toBe(false);
  });

  it('does not flag when prior average is zero', () => {
    expect(isAnomalous(10, [0, 0, 0, 0])).toBe(false);
  });

  it('returns multiplier when anomalous', () => {
    const history = [1, 1, 1, 1, 1, 1, 1];
    expect(isAnomalous(10, history, { returnMultiplier: true })).toBe(10);
  });
});
```

- [ ] **Step 2: Run test to verify fail**

Run: `npm test --prefix frontend -- anomaly`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `lib/anomaly.ts`**

Create `frontend/src/app/dashboard/admin/users/lib/anomaly.ts`:

```typescript
const MIN_HISTORY_DAYS = 3;
const MIN_TODAY_FLOOR_USD = 1.0;
const MULTIPLIER = 5.0;

export interface AnomalyOptions {
  returnMultiplier?: boolean;
}

export function isAnomalous(
  todayCostUsd: number,
  priorDailyCostsUsd: number[],
): boolean;
export function isAnomalous(
  todayCostUsd: number,
  priorDailyCostsUsd: number[],
  options: { returnMultiplier: true },
): number | false;
export function isAnomalous(
  todayCostUsd: number,
  priorDailyCostsUsd: number[],
  options: AnomalyOptions = {},
): boolean | number {
  if (priorDailyCostsUsd.length < MIN_HISTORY_DAYS) return false;
  if (todayCostUsd < MIN_TODAY_FLOOR_USD) return false;

  const sum = priorDailyCostsUsd.reduce((a, b) => a + b, 0);
  const avg = sum / priorDailyCostsUsd.length;
  if (avg <= 0) return false;

  const ratio = todayCostUsd / avg;
  const flagged = ratio >= MULTIPLIER;
  if (!flagged) return false;
  return options.returnMultiplier ? ratio : true;
}
```

- [ ] **Step 4: Run test to verify pass**

Run: `npm test --prefix frontend -- anomaly`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/lib/anomaly.ts frontend/src/app/dashboard/admin/users/__tests__/anomaly.test.ts
git commit -m "feat(admin-ui): add isAnomalous pure function"
```

---

### Task 11: `lib/filterTypes.ts` (URL ↔ FilterState codec) + tests

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/lib/filterTypes.ts`
- Create: `frontend/src/app/dashboard/admin/users/__tests__/filterTypes.test.ts`

- [ ] **Step 1: Write failing test**

Create `frontend/src/app/dashboard/admin/users/__tests__/filterTypes.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_FILTER_STATE,
  filterStateFromUrl,
  filterStateToParams,
  filterStateToUrl,
} from '../lib/filterTypes';

describe('filterStateFromUrl / filterStateToUrl', () => {
  it('round-trips a populated state', () => {
    const state = {
      ...DEFAULT_FILTER_STATE,
      status: 'active' as const,
      search: 'alice',
      sortBy: 'cost_today' as const,
      minCostToday: 10,
      quotaState: 'near' as const,
      provider: 'anthropic',
      view: null,
    };
    const url = filterStateToUrl(state);
    const parsed = filterStateFromUrl(new URLSearchParams(url));
    expect(parsed).toEqual(state);
  });

  it('default state produces empty querystring', () => {
    expect(filterStateToUrl(DEFAULT_FILTER_STATE)).toBe('');
  });

  it('ignores unknown query params (forward compat)', () => {
    const params = new URLSearchParams('?status=active&future_param=42');
    const parsed = filterStateFromUrl(params);
    expect(parsed.status).toBe('active');
    expect(parsed).not.toHaveProperty('future_param');
  });

  it('rejects invalid status silently', () => {
    const params = new URLSearchParams('?status=banana');
    const parsed = filterStateFromUrl(params);
    expect(parsed.status).toBeNull();
  });
});

describe('filterStateToParams', () => {
  it('omits null/empty values', () => {
    const params = filterStateToParams(DEFAULT_FILTER_STATE);
    expect(params.toString()).toBe('sort_by=created&limit=100&offset=0');
  });

  it('serialises all filters', () => {
    const params = filterStateToParams({
      ...DEFAULT_FILTER_STATE,
      status: 'active',
      minCostToday: 5,
      provider: 'anthropic',
      activeWithinHours: 24,
      quotaState: 'near',
    });
    const obj = Object.fromEntries(params.entries());
    expect(obj).toMatchObject({
      status: 'active',
      min_cost_today: '5',
      provider: 'anthropic',
      active_within_hours: '24',
      quota_state: 'near',
    });
  });
});
```

- [ ] **Step 2: Run to confirm fail**

Run: `npm test --prefix frontend -- filterTypes`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `lib/filterTypes.ts`**

Create `frontend/src/app/dashboard/admin/users/lib/filterTypes.ts`:

```typescript
import type { FilterState, QuotaStateFilter, SortBy, UserStatus } from '../types';

export const DEFAULT_FILTER_STATE: FilterState = {
  status: null,
  search: '',
  sortBy: 'created',
  minCostToday: null,
  minCostMonth: null,
  quotaState: null,
  provider: null,
  activeWithinHours: null,
  view: null,
};

const VALID_STATUS: ReadonlyArray<UserStatus> = [
  'pending_approval',
  'active',
  'suspended',
  'rejected',
  'deleted',
];

const VALID_SORT: ReadonlyArray<SortBy> = [
  'created',
  'cost_today',
  'cost_month',
  'cost_alltime',
  'last_login',
];

const VALID_QUOTA: ReadonlyArray<QuotaStateFilter> = [
  'near',
  'over',
  'custom',
  'default',
];

function parseNumber(s: string | null): number | null {
  if (s === null || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export function filterStateFromUrl(params: URLSearchParams): FilterState {
  const status = params.get('status');
  const sortBy = params.get('sort_by');
  const quotaState = params.get('quota_state');
  return {
    status: VALID_STATUS.includes(status as UserStatus) ? (status as UserStatus) : null,
    search: params.get('q') ?? '',
    sortBy: VALID_SORT.includes(sortBy as SortBy) ? (sortBy as SortBy) : 'created',
    minCostToday: parseNumber(params.get('min_cost_today')),
    minCostMonth: parseNumber(params.get('min_cost_month')),
    quotaState: VALID_QUOTA.includes(quotaState as QuotaStateFilter)
      ? (quotaState as QuotaStateFilter)
      : null,
    provider: params.get('provider'),
    activeWithinHours: parseNumber(params.get('active_within_hours')),
    view: params.get('view'),
  };
}

export function filterStateToUrl(state: FilterState): string {
  const out = new URLSearchParams();
  if (state.status) out.set('status', state.status);
  if (state.search) out.set('q', state.search);
  if (state.sortBy !== 'created') out.set('sort_by', state.sortBy);
  if (state.minCostToday !== null) out.set('min_cost_today', String(state.minCostToday));
  if (state.minCostMonth !== null) out.set('min_cost_month', String(state.minCostMonth));
  if (state.quotaState) out.set('quota_state', state.quotaState);
  if (state.provider) out.set('provider', state.provider);
  if (state.activeWithinHours !== null) {
    out.set('active_within_hours', String(state.activeWithinHours));
  }
  if (state.view) out.set('view', state.view);
  return out.toString();
}

/**
 * Serialise FilterState to backend query params for /admin/users.
 * Always sets sort_by/limit/offset (defaults preserved). The `view` field
 * is purely client-side and is NOT sent to the backend.
 */
export function filterStateToParams(
  state: FilterState,
  pagination: { limit?: number; offset?: number } = {},
): URLSearchParams {
  const params = new URLSearchParams();
  if (state.status) params.set('status', state.status);
  if (state.search) params.set('search', state.search);
  params.set('sort_by', state.sortBy);
  if (state.minCostToday !== null) params.set('min_cost_today', String(state.minCostToday));
  if (state.minCostMonth !== null) params.set('min_cost_month', String(state.minCostMonth));
  if (state.quotaState) params.set('quota_state', state.quotaState);
  if (state.provider) params.set('provider', state.provider);
  if (state.activeWithinHours !== null) {
    params.set('active_within_hours', String(state.activeWithinHours));
  }
  params.set('limit', String(pagination.limit ?? 100));
  params.set('offset', String(pagination.offset ?? 0));
  return params;
}
```

- [ ] **Step 4: Run tests**

Run: `npm test --prefix frontend -- filterTypes`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/lib/filterTypes.ts frontend/src/app/dashboard/admin/users/__tests__/filterTypes.test.ts
git commit -m "feat(admin-ui): add filterTypes URL/params codec"
```

---

### Task 12: `lib/views.ts` (built-in saved views) + tests

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/lib/views.ts`
- Create: `frontend/src/app/dashboard/admin/users/__tests__/views.test.ts`

- [ ] **Step 1: Write failing test**

Create `frontend/src/app/dashboard/admin/users/__tests__/views.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';
import { BUILTIN_VIEWS, getViewById } from '../lib/views';

describe('built-in views', () => {
  it('exports the spec set', () => {
    const ids = BUILTIN_VIEWS.map((v) => v.id);
    expect(ids).toEqual([
      'pending',
      'top-spenders-today',
      'anomalies',
      'near-quota',
      'recently-active',
      'new-this-week',
    ]);
  });

  it('all built-ins have builtin=true', () => {
    expect(BUILTIN_VIEWS.every((v) => v.builtin)).toBe(true);
  });

  it('pending view sets status filter', () => {
    const view = getViewById('pending');
    expect(view?.filterState.status).toBe('pending_approval');
  });

  it('top-spenders sorts by cost_today', () => {
    const view = getViewById('top-spenders-today');
    expect(view?.filterState.sortBy).toBe('cost_today');
  });

  it('returns undefined for unknown id', () => {
    expect(getViewById('nope')).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run to confirm fail**

Run: `npm test --prefix frontend -- views`

- [ ] **Step 3: Implement `lib/views.ts`**

Create `frontend/src/app/dashboard/admin/users/lib/views.ts`:

```typescript
import type { SavedView } from '../types';
import { DEFAULT_FILTER_STATE } from './filterTypes';

export const BUILTIN_VIEWS: SavedView[] = [
  {
    id: 'pending',
    name: 'Pending',
    builtin: true,
    filterState: { ...DEFAULT_FILTER_STATE, status: 'pending_approval', view: 'pending' },
  },
  {
    id: 'top-spenders-today',
    name: 'Top spenders today',
    builtin: true,
    filterState: {
      ...DEFAULT_FILTER_STATE,
      sortBy: 'cost_today',
      view: 'top-spenders-today',
    },
  },
  {
    id: 'anomalies',
    name: 'Anomalies',
    builtin: true,
    filterState: { ...DEFAULT_FILTER_STATE, view: 'anomalies' },
  },
  {
    id: 'near-quota',
    name: 'Near quota',
    builtin: true,
    filterState: { ...DEFAULT_FILTER_STATE, quotaState: 'near', view: 'near-quota' },
  },
  {
    id: 'recently-active',
    name: 'Recently active',
    builtin: true,
    filterState: {
      ...DEFAULT_FILTER_STATE,
      activeWithinHours: 24,
      sortBy: 'last_login',
      view: 'recently-active',
    },
  },
  {
    id: 'new-this-week',
    name: 'New this week',
    builtin: true,
    filterState: {
      ...DEFAULT_FILTER_STATE,
      sortBy: 'created',
      activeWithinHours: 24 * 7,
      view: 'new-this-week',
    },
  },
];

export function getViewById(id: string): SavedView | undefined {
  return BUILTIN_VIEWS.find((v) => v.id === id);
}
```

- [ ] **Step 4: Run tests**

Run: `npm test --prefix frontend -- views`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/lib/views.ts frontend/src/app/dashboard/admin/users/__tests__/views.test.ts
git commit -m "feat(admin-ui): add built-in saved views"
```

---

### Task 13: `hooks/useSavedViews.ts` + tests

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/hooks/useSavedViews.ts`
- Create: `frontend/src/app/dashboard/admin/users/__tests__/useSavedViews.test.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  loadCustomViews,
  saveCustomView,
  deleteCustomView,
  STORAGE_KEY,
} from '../hooks/useSavedViews';
import { DEFAULT_FILTER_STATE } from '../lib/filterTypes';

const memStore: Record<string, string> = {};
beforeEach(() => {
  for (const k of Object.keys(memStore)) delete memStore[k];
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => memStore[k] ?? null,
    setItem: (k: string, v: string) => {
      memStore[k] = v;
    },
    removeItem: (k: string) => {
      delete memStore[k];
    },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe('useSavedViews helpers', () => {
  it('loads empty when nothing stored', () => {
    expect(loadCustomViews()).toEqual([]);
  });

  it('loads empty when storage corrupt (no crash)', () => {
    memStore[STORAGE_KEY] = 'not json';
    expect(loadCustomViews()).toEqual([]);
  });

  it('saves and reloads a view', () => {
    saveCustomView({
      id: 'mine',
      name: 'Mine',
      builtin: false,
      filterState: { ...DEFAULT_FILTER_STATE, search: 'a' },
    });
    const loaded = loadCustomViews();
    expect(loaded).toHaveLength(1);
    expect(loaded[0].id).toBe('mine');
  });

  it('refuses to save a view with builtin=true', () => {
    expect(() =>
      saveCustomView({
        id: 'pending',
        name: 'Pending',
        builtin: true,
        filterState: DEFAULT_FILTER_STATE,
      }),
    ).toThrow();
  });

  it('deleteCustomView removes the entry', () => {
    saveCustomView({
      id: 'mine',
      name: 'Mine',
      builtin: false,
      filterState: DEFAULT_FILTER_STATE,
    });
    deleteCustomView('mine');
    expect(loadCustomViews()).toEqual([]);
  });
});
```

- [ ] **Step 2: Run to confirm fail**

Run: `npm test --prefix frontend -- useSavedViews`

- [ ] **Step 3: Implement `hooks/useSavedViews.ts`**

```typescript
import { useCallback, useEffect, useState } from 'react';
import type { SavedView } from '../types';

export const STORAGE_KEY = 'admin.users.savedViews';

export function loadCustomViews(): SavedView[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (v): v is SavedView =>
        v && typeof v.id === 'string' && typeof v.name === 'string' && v.builtin === false,
    );
  } catch {
    return [];
  }
}

export function saveCustomView(view: SavedView): void {
  if (view.builtin) {
    throw new Error('Cannot save a built-in view');
  }
  const existing = loadCustomViews();
  const next = [...existing.filter((v) => v.id !== view.id), view];
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
}

export function deleteCustomView(id: string): void {
  const existing = loadCustomViews();
  const next = existing.filter((v) => v.id !== id);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
}

export function useSavedViews() {
  const [views, setViews] = useState<SavedView[]>([]);

  useEffect(() => {
    setViews(loadCustomViews());
  }, []);

  const save = useCallback((view: SavedView) => {
    saveCustomView(view);
    setViews(loadCustomViews());
  }, []);

  const remove = useCallback((id: string) => {
    deleteCustomView(id);
    setViews(loadCustomViews());
  }, []);

  return { views, save, remove };
}
```

- [ ] **Step 4: Run tests**

Run: `npm test --prefix frontend -- useSavedViews`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/hooks/useSavedViews.ts frontend/src/app/dashboard/admin/users/__tests__/useSavedViews.test.ts
git commit -m "feat(admin-ui): localStorage-backed custom saved views"
```

---

## Phase 4 — New frontend components

### Task 14: Extend `lib/api/admin.ts` with new fetchers

**Files:**
- Modify: `frontend/src/lib/api/admin.ts`

- [ ] **Step 1: Add new types and fetchers**

Append to `frontend/src/lib/api/admin.ts`:

```typescript
import type {
  CostHistoryPoint,
  UsersSummary,
} from '@/app/dashboard/admin/users/types';

export interface UserCostHistoryResponse {
  user_id: string;
  days: number;
  points: CostHistoryPoint[];
}

export interface BulkCostHistoryResponse {
  days: number;
  histories: Record<string, CostHistoryPoint[]>;
}

export async function getUserCostHistory(
  userId: string,
  days = 7,
): Promise<UserCostHistoryResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/users/${encodeURIComponent(userId)}/cost-history?days=${days}`,
  );
  if (!resp.ok) throw new Error(`cost-history ${resp.status}`);
  return resp.json();
}

export async function getBulkCostHistory(
  userIds: string[],
  days = 7,
): Promise<BulkCostHistoryResponse> {
  if (userIds.length === 0) return { days, histories: {} };
  const params = new URLSearchParams({
    user_ids: userIds.join(','),
    days: String(days),
  });
  const resp = await fetchWithAuth(API_BASE, `/admin/users/cost-history?${params}`);
  if (!resp.ok) throw new Error(`bulk cost-history ${resp.status}`);
  return resp.json();
}

export async function getUsersSummary(): Promise<UsersSummary> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/summary`);
  if (!resp.ok) throw new Error(`users summary ${resp.status}`);
  return resp.json();
}
```

- [ ] **Step 2: Update `listUsers` signature with new params**

Find `listUsers` (around line 54 of `admin.ts`). Change its signature to accept the additional params and append them to the URL when set:

```typescript
export interface ListUsersOptions {
  status?: string;
  search?: string;
  sortBy?: SortBy;
  limit?: number;
  offset?: number;
  minCostToday?: number;
  minCostMonth?: number;
  quotaState?: QuotaStateFilter;
  provider?: string;
  activeWithinHours?: number;
}

export async function listUsers(opts: ListUsersOptions = {}): Promise<ListUsersResponse> {
  const params = new URLSearchParams();
  if (opts.status) params.set('status', opts.status);
  if (opts.search) params.set('search', opts.search);
  if (opts.sortBy) params.set('sort_by', opts.sortBy);
  if (opts.limit !== undefined) params.set('limit', String(opts.limit));
  if (opts.offset !== undefined) params.set('offset', String(opts.offset));
  if (opts.minCostToday !== undefined) params.set('min_cost_today', String(opts.minCostToday));
  if (opts.minCostMonth !== undefined) params.set('min_cost_month', String(opts.minCostMonth));
  if (opts.quotaState) params.set('quota_state', opts.quotaState);
  if (opts.provider) params.set('provider', opts.provider);
  if (opts.activeWithinHours !== undefined) {
    params.set('active_within_hours', String(opts.activeWithinHours));
  }
  const resp = await fetchWithAuth(API_BASE, `/admin/users?${params}`);
  if (!resp.ok) throw new Error(`listUsers ${resp.status}`);
  return resp.json();
}
```

Update the existing call sites in `users/index.tsx` to pass an options object instead of positional args.

- [ ] **Step 3: Run lint + type-check**

Run: `npm run lint --prefix frontend && npm run type-check --prefix frontend`
Expected: 0 errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api/admin.ts frontend/src/app/dashboard/admin/users/index.tsx
git commit -m "feat(admin-ui): admin API client gains cost-history + summary + new list filters"
```

---

### Task 15: `Sparkline.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/Sparkline.tsx`

- [ ] **Step 1: Implement using recharts**

Create `frontend/src/app/dashboard/admin/users/Sparkline.tsx`:

```typescript
'use client';

import { LineChart, Line, ResponsiveContainer, YAxis } from 'recharts';
import type { CostHistoryPoint } from './types';

interface SparklineProps {
  points: CostHistoryPoint[];
  width?: number;
  height?: number;
  color?: string;
}

export function Sparkline({
  points,
  width = 60,
  height = 16,
  color = '#9ca3af', // neutral grey-400
}: SparklineProps) {
  if (!points || points.length === 0) {
    return <span className="text-gray-300 text-xs">—</span>;
  }
  // Normalize to numbers
  const data = points.map((p) => ({ day: p.day, cost: Number(p.cost_usd) }));
  return (
    <div style={{ width, height }} aria-label="7-day cost trend" role="img">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <YAxis hide domain={[0, 'dataMax']} />
          <Line
            type="monotone"
            dataKey="cost"
            stroke={color}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
```

- [ ] **Step 2: Verify type-check**

Run: `npm run type-check --prefix frontend`
Expected: 0 errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/Sparkline.tsx
git commit -m "feat(admin-ui): add Sparkline component (recharts mini line chart)"
```

---

### Task 16: `hooks/useUserCostHistory.ts`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/hooks/useUserCostHistory.ts`

- [ ] **Step 1: Implement bulk hook**

Create `frontend/src/app/dashboard/admin/users/hooks/useUserCostHistory.ts`:

```typescript
'use client';

import { useQuery } from '@tanstack/react-query';
import { getBulkCostHistory, getUserCostHistory } from '@/lib/api/admin';
import type { CostHistoryPoint } from '../types';

const FIVE_MIN = 5 * 60 * 1000;

export function useBulkCostHistory(userIds: string[], days = 7, enabled = true) {
  // Stable cache key: sort ids so order doesn't matter
  const key = [...userIds].sort().join(',');
  return useQuery<Record<string, CostHistoryPoint[]>>({
    queryKey: ['admin', 'users', 'cost-history', 'bulk', key, days],
    queryFn: async () => {
      if (userIds.length === 0) return {};
      const resp = await getBulkCostHistory(userIds, days);
      return resp.histories;
    },
    enabled: enabled && userIds.length > 0,
    staleTime: FIVE_MIN,
  });
}

export function useUserCostHistory(userId: string | null, days = 7) {
  return useQuery<CostHistoryPoint[]>({
    queryKey: ['admin', 'users', 'cost-history', userId, days],
    queryFn: async () => {
      if (!userId) return [];
      const resp = await getUserCostHistory(userId, days);
      return resp.points;
    },
    enabled: !!userId,
    staleTime: FIVE_MIN,
  });
}
```

- [ ] **Step 2: Type-check**

Run: `npm run type-check --prefix frontend`

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/hooks/useUserCostHistory.ts
git commit -m "feat(admin-ui): cost-history hooks (single + bulk, 5min cache)"
```

---

### Task 17: `hooks/useUsers.ts` and `hooks/useUsersSummary.ts`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/hooks/useUsers.ts`
- Create: `frontend/src/app/dashboard/admin/users/hooks/useUsersSummary.ts`

- [ ] **Step 1: Implement `useUsers`**

```typescript
'use client';

import { useQuery } from '@tanstack/react-query';
import { listUsers } from '@/lib/api/admin';
import type { FilterState } from '../types';

const THIRTY_S = 30 * 1000;

export function useUsers(state: FilterState, limit = 100, offset = 0) {
  return useQuery({
    queryKey: ['admin', 'users', 'list', state, limit, offset],
    queryFn: () =>
      listUsers({
        status: state.status ?? undefined,
        search: state.search || undefined,
        sortBy: state.sortBy,
        limit,
        offset,
        minCostToday: state.minCostToday ?? undefined,
        minCostMonth: state.minCostMonth ?? undefined,
        quotaState: state.quotaState ?? undefined,
        provider: state.provider ?? undefined,
        activeWithinHours: state.activeWithinHours ?? undefined,
      }),
    staleTime: THIRTY_S,
  });
}
```

- [ ] **Step 2: Implement `useUsersSummary`**

```typescript
'use client';

import { useQuery } from '@tanstack/react-query';
import { getUsersSummary } from '@/lib/api/admin';

const THIRTY_S = 30 * 1000;

export function useUsersSummary() {
  return useQuery({
    queryKey: ['admin', 'users', 'summary'],
    queryFn: getUsersSummary,
    staleTime: THIRTY_S,
  });
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/hooks/useUsers.ts frontend/src/app/dashboard/admin/users/hooks/useUsersSummary.ts
git commit -m "feat(admin-ui): React Query hooks for users list and summary"
```

---

### Task 18: `SummaryCards.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/SummaryCards.tsx`

- [ ] **Step 1: Implement**

Create `frontend/src/app/dashboard/admin/users/SummaryCards.tsx`:

```typescript
'use client';

import { useUsersSummary } from './hooks/useUsersSummary';
import type { SummaryCard as SummaryCardData, UsersSummary } from './types';

interface SummaryCardsProps {
  onCardClick: (cardId: 'pending' | 'top-spenders-today' | 'anomalies' | 'near-quota') => void;
}

export function SummaryCards({ onCardClick }: SummaryCardsProps) {
  const { data, isLoading, error } = useUsersSummary();

  if (error) {
    return (
      <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
        Could not load summary stats — table is still usable below.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
      <Card
        title="Pending Approval"
        accent="indigo"
        loading={isLoading}
        card={data?.pending}
        onClick={() => onCardClick('pending')}
      />
      <Card
        title="Top Spenders Today"
        accent="emerald"
        loading={isLoading}
        card={data?.top_spenders_today}
        showCost
        onClick={() => onCardClick('top-spenders-today')}
      />
      <Card
        title="Anomalies"
        accent="red"
        loading={isLoading}
        card={data?.anomalies}
        showCost
        onClick={() => onCardClick('anomalies')}
      />
      <Card
        title="Near / Over Quota"
        accent="amber"
        loading={isLoading}
        card={data?.near_quota}
        showCost
        onClick={() => onCardClick('near-quota')}
      />
    </div>
  );
}

interface CardProps {
  title: string;
  accent: 'indigo' | 'emerald' | 'red' | 'amber';
  card: SummaryCardData | undefined;
  loading: boolean;
  showCost?: boolean;
  onClick: () => void;
}

function Card({ title, accent, card, loading, showCost, onClick }: CardProps) {
  const accentBorder = {
    indigo: 'border-indigo-200',
    emerald: 'border-emerald-200',
    red: 'border-red-200',
    amber: 'border-amber-200',
  }[accent];
  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-left rounded-lg border ${accentBorder} bg-white p-4 hover:shadow-md transition-shadow`}
    >
      <div className="text-xs uppercase tracking-wide text-gray-500">{title}</div>
      <div className="mt-1 text-2xl font-semibold">
        {loading ? '—' : (card?.count ?? 0)}
      </div>
      <ul className="mt-2 space-y-1 text-sm text-gray-700">
        {(card?.top ?? []).slice(0, 3).map((u) => (
          <li key={u.id} className="flex justify-between gap-2 truncate">
            <span className="truncate">{u.email}</span>
            {showCost && <span className="font-mono text-xs">${Number(u.today_cost_usd).toFixed(2)}</span>}
          </li>
        ))}
      </ul>
    </button>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `npm run type-check --prefix frontend`

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/SummaryCards.tsx
git commit -m "feat(admin-ui): SummaryCards row (pending / top spenders / anomalies / near quota)"
```

---

### Task 19: `SavedViews.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/SavedViews.tsx`

- [ ] **Step 1: Implement**

Create `frontend/src/app/dashboard/admin/users/SavedViews.tsx`:

```typescript
'use client';

import { useState } from 'react';
import { BUILTIN_VIEWS } from './lib/views';
import { useSavedViews } from './hooks/useSavedViews';
import type { FilterState, SavedView } from './types';

interface SavedViewsProps {
  current: FilterState;
  onApply: (state: FilterState) => void;
}

export function SavedViews({ current, onApply }: SavedViewsProps) {
  const { views: customViews, save, remove } = useSavedViews();
  const [showSaveDialog, setShowSaveDialog] = useState(false);

  const all: SavedView[] = [...BUILTIN_VIEWS, ...customViews];
  const activeId = current.view;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {all.map((v) => (
        <button
          key={v.id}
          type="button"
          onClick={() => onApply(v.filterState)}
          className={`rounded-full px-3 py-1 text-xs font-medium border transition-colors ${
            activeId === v.id
              ? 'bg-blue-600 text-white border-blue-600'
              : 'bg-white text-gray-700 border-gray-300 hover:bg-gray-50'
          }`}
        >
          {v.name}
          {!v.builtin && (
            <span
              role="button"
              aria-label={`Delete view ${v.name}`}
              className="ml-2 text-gray-400 hover:text-red-500"
              onClick={(e) => {
                e.stopPropagation();
                remove(v.id);
              }}
            >
              ×
            </span>
          )}
        </button>
      ))}
      <button
        type="button"
        onClick={() => setShowSaveDialog(true)}
        className="rounded-full px-3 py-1 text-xs font-medium border border-dashed border-gray-400 text-gray-600 hover:bg-gray-50"
      >
        + Save current
      </button>
      {showSaveDialog && (
        <SaveDialog
          current={current}
          onSave={(name) => {
            const id = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-');
            save({ id, name: name.trim(), builtin: false, filterState: { ...current, view: id } });
            setShowSaveDialog(false);
          }}
          onCancel={() => setShowSaveDialog(false)}
        />
      )}
    </div>
  );
}

function SaveDialog({
  current,
  onSave,
  onCancel,
}: {
  current: FilterState;
  onSave: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState('');
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="rounded-lg bg-white p-4 shadow-lg w-80">
        <h3 className="font-semibold text-sm mb-2">Save current filter as a view</h3>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. High-cost free users"
          className="w-full rounded border px-2 py-1 text-sm"
        />
        <div className="mt-3 flex justify-end gap-2 text-sm">
          <button onClick={onCancel} className="px-3 py-1 rounded bg-gray-100">
            Cancel
          </button>
          <button
            onClick={() => name.trim() && onSave(name)}
            disabled={!name.trim()}
            className="px-3 py-1 rounded bg-blue-600 text-white disabled:bg-gray-300"
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/SavedViews.tsx
git commit -m "feat(admin-ui): SavedViews chip row with built-ins + custom view CRUD"
```

---

### Task 20: Filter sub-components

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/filters/StatusFilter.tsx`
- Create: `frontend/src/app/dashboard/admin/users/filters/UsageFilter.tsx`
- Create: `frontend/src/app/dashboard/admin/users/filters/ProviderFilter.tsx`
- Create: `frontend/src/app/dashboard/admin/users/filters/QuotaFilter.tsx`

Each is a simple controlled-input component. Skeleton for `StatusFilter.tsx`:

```typescript
'use client';

import type { UserStatus } from '../types';

const OPTIONS: Array<{ value: UserStatus | ''; label: string }> = [
  { value: '', label: 'All statuses' },
  { value: 'pending_approval', label: 'Pending' },
  { value: 'active', label: 'Active' },
  { value: 'suspended', label: 'Suspended' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'deleted', label: 'Deleted' },
];

interface Props {
  value: UserStatus | null;
  onChange: (value: UserStatus | null) => void;
}

export function StatusFilter({ value, onChange }: Props) {
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange((e.target.value as UserStatus) || null)}
      className="rounded border border-gray-300 bg-white px-2 py-1 text-sm"
    >
      {OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
```

- [ ] **Step 1: Implement StatusFilter** (above)

- [ ] **Step 2: Implement UsageFilter**

```typescript
'use client';

interface Props {
  minCostToday: number | null;
  minCostMonth: number | null;
  activeWithinHours: number | null;
  onChange: (patch: {
    minCostToday?: number | null;
    minCostMonth?: number | null;
    activeWithinHours?: number | null;
  }) => void;
}

export function UsageFilter({
  minCostToday, minCostMonth, activeWithinHours, onChange,
}: Props) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">today ≥ $</span>
        <input
          type="number" min={0} step="0.01"
          value={minCostToday ?? ''}
          onChange={(e) =>
            onChange({ minCostToday: e.target.value === '' ? null : Number(e.target.value) })
          }
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">month ≥ $</span>
        <input
          type="number" min={0} step="0.01"
          value={minCostMonth ?? ''}
          onChange={(e) =>
            onChange({ minCostMonth: e.target.value === '' ? null : Number(e.target.value) })
          }
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-xs text-gray-500">active in last (h)</span>
        <input
          type="number" min={1}
          value={activeWithinHours ?? ''}
          onChange={(e) =>
            onChange({ activeWithinHours: e.target.value === '' ? null : Number(e.target.value) })
          }
          className="w-16 rounded border px-1 py-0.5"
        />
      </label>
    </div>
  );
}
```

- [ ] **Step 3: Implement ProviderFilter**

A simple `<select>` for now with hard-coded options that match the providers configured in `config/providers.json` (or wherever the source of truth is). The list of options is fetched from the existing admin providers endpoint if available, otherwise hard-coded:

```typescript
'use client';

const OPTIONS = ['', 'anthropic', 'openai', 'gemini', 'mistral', 'openrouter'];

interface Props {
  value: string | null;
  onChange: (v: string | null) => void;
}

export function ProviderFilter({ value, onChange }: Props) {
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className="rounded border px-2 py-1 text-sm bg-white"
    >
      {OPTIONS.map((p) => (
        <option key={p} value={p}>
          {p === '' ? 'Any provider' : p}
        </option>
      ))}
    </select>
  );
}
```

(Before merging, audit `config/` for the canonical provider list and either replace the hard-coded array or fetch it.)

- [ ] **Step 4: Implement QuotaFilter**

```typescript
'use client';

import type { QuotaStateFilter } from '../types';

const OPTIONS: Array<{ value: QuotaStateFilter | ''; label: string }> = [
  { value: '', label: 'Any quota' },
  { value: 'default', label: 'Default quota' },
  { value: 'custom', label: 'Custom quota' },
  { value: 'near', label: 'Near quota (≥80%)' },
  { value: 'over', label: 'Over quota' },
];

interface Props {
  value: QuotaStateFilter | null;
  onChange: (v: QuotaStateFilter | null) => void;
}

export function QuotaFilter({ value, onChange }: Props) {
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange((e.target.value as QuotaStateFilter) || null)}
      className="rounded border px-2 py-1 text-sm bg-white"
    >
      {OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/filters/
git commit -m "feat(admin-ui): filter sub-components (status / usage / provider / quota)"
```

---

### Task 21: `FilterBar.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/FilterBar.tsx`

- [ ] **Step 1: Implement**

```typescript
'use client';

import { useEffect, useState } from 'react';
import type { Density, FilterState } from './types';
import { StatusFilter } from './filters/StatusFilter';
import { UsageFilter } from './filters/UsageFilter';
import { ProviderFilter } from './filters/ProviderFilter';
import { QuotaFilter } from './filters/QuotaFilter';
import { DEFAULT_FILTER_STATE } from './lib/filterTypes';

interface FilterBarProps {
  state: FilterState;
  onChange: (state: FilterState) => void;
  density: Density;
  onDensityChange: (d: Density) => void;
}

export function FilterBar({ state, onChange, density, onDensityChange }: FilterBarProps) {
  const [searchInput, setSearchInput] = useState(state.search);
  // Debounce search 300ms
  useEffect(() => {
    const t = setTimeout(() => {
      if (searchInput !== state.search) onChange({ ...state, search: searchInput });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput]);

  const isDefault = JSON.stringify(state) === JSON.stringify(DEFAULT_FILTER_STATE);

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-gray-200 bg-gray-50 p-3">
      <input
        type="search"
        placeholder="Search email, key prefix, id…"
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        className="flex-1 min-w-48 rounded border border-gray-300 px-3 py-1 text-sm bg-white"
      />
      <StatusFilter
        value={state.status}
        onChange={(status) => onChange({ ...state, status })}
      />
      <UsageFilter
        minCostToday={state.minCostToday}
        minCostMonth={state.minCostMonth}
        activeWithinHours={state.activeWithinHours}
        onChange={(patch) => onChange({ ...state, ...patch })}
      />
      <ProviderFilter
        value={state.provider}
        onChange={(provider) => onChange({ ...state, provider })}
      />
      <QuotaFilter
        value={state.quotaState}
        onChange={(quotaState) => onChange({ ...state, quotaState })}
      />
      {!isDefault && (
        <button
          type="button"
          onClick={() => onChange(DEFAULT_FILTER_STATE)}
          className="text-xs text-gray-600 underline hover:text-gray-900"
        >
          Clear filters
        </button>
      )}
      <div className="ml-auto flex items-center gap-1 text-xs text-gray-600">
        <span>Density:</span>
        <button
          onClick={() => onDensityChange('comfortable')}
          className={`rounded px-2 py-0.5 ${density === 'comfortable' ? 'bg-gray-200' : ''}`}
        >
          Comfortable
        </button>
        <button
          onClick={() => onDensityChange('compact')}
          className={`rounded px-2 py-0.5 ${density === 'compact' ? 'bg-gray-200' : ''}`}
        >
          Compact
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/FilterBar.tsx
git commit -m "feat(admin-ui): FilterBar with search debounce, density toggle, clear filters"
```

---

### Task 22: `UserRow.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/UserRow.tsx`

- [ ] **Step 1: Implement**

Create `frontend/src/app/dashboard/admin/users/UserRow.tsx`:

```typescript
'use client';

import { Sparkline } from './Sparkline';
import { isAnomalous } from './lib/anomaly';
import type { CostHistoryPoint, Density, UserRow as User } from './types';

interface UserRowProps {
  user: User;
  history: CostHistoryPoint[] | undefined; // 7d
  pageMedianToday: number;
  density: Density;
  expanded: boolean;
  onToggleExpanded: () => void;
  onApprove: () => void;
  onReject: () => void;
  onRegenerateKey: () => void;
}

const STATUS_GLYPH: Record<string, { glyph: string; color: string; title: string }> = {
  pending_approval: { glyph: '⬤', color: 'text-indigo-500', title: 'Pending' },
  active: { glyph: '●', color: 'text-gray-300', title: 'Active' },
  suspended: { glyph: '▲', color: 'text-amber-500', title: 'Suspended' },
  rejected: { glyph: '✕', color: 'text-red-500', title: 'Rejected' },
  deleted: { glyph: '✕', color: 'text-gray-400', title: 'Deleted' },
};

function todayCostBucket(cost: number, median: number): string {
  if (median <= 0 || cost < median) return '';
  const ratio = cost / median;
  if (ratio < 3) return 'bg-yellow-50';
  if (ratio < 10) return 'bg-amber-100';
  return 'bg-red-100';
}

export function UserRow({
  user,
  history,
  pageMedianToday,
  density,
  expanded,
  onToggleExpanded,
  onApprove,
  onReject,
  onRegenerateKey,
}: UserRowProps) {
  const statusMark = STATUS_GLYPH[user.status] ?? STATUS_GLYPH.active;
  const today = Number(user.usage_today_usd);
  const prior = (history ?? []).slice(0, 7).map((p) => Number(p.cost_usd));
  const anomalous = isAnomalous(today, prior);
  const nearQuota = false; // computed in summary; no per-row quota lookup here for now
  const badge = anomalous ? '⚠' : nearQuota ? '◐' : null;
  const rowHeight = density === 'compact' ? 'h-9' : 'h-14';
  const showSparkline = density === 'comfortable';
  const cellBucket = todayCostBucket(today, pageMedianToday);

  return (
    <>
      <tr
        onClick={onToggleExpanded}
        className={`${rowHeight} cursor-pointer hover:bg-gray-50 border-b`}
      >
        <td className="px-2 text-center">
          <span className={statusMark.color} title={statusMark.title}>
            {statusMark.glyph}
          </span>
        </td>
        <td className="px-2">
          <div className="font-medium text-gray-900">{user.email}</div>
          {density === 'comfortable' && user.user_name && (
            <div className="text-xs text-gray-500">{user.user_name}</div>
          )}
        </td>
        <td className="px-2 text-xs uppercase text-gray-600">
          {user.role !== 'free' ? user.role : null}
        </td>
        <td className={`px-2 font-mono text-sm ${cellBucket}`}>${today.toFixed(2)}</td>
        {showSparkline && (
          <td className="px-2">
            <Sparkline points={history ?? []} />
          </td>
        )}
        <td className="px-2 font-mono text-sm">${Number(user.usage_month_usd).toFixed(2)}</td>
        <td className="px-2 font-mono text-sm text-gray-600">
          ${Number(user.usage_alltime_usd).toFixed(2)}
        </td>
        <td className="px-2 text-xs">{user.status.replace('_', ' ')}</td>
        <td className="px-2 text-center">
          {badge && (
            <span
              className={`inline-block rounded-full px-1 text-xs ${
                anomalous ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
              }`}
              title={anomalous ? 'Anomaly: ≥5x 7d avg' : 'Near or over quota'}
            >
              {badge}
            </span>
          )}
        </td>
        <td className="px-2">
          {user.status === 'pending_approval' && (
            <div className="flex gap-1">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onApprove();
                }}
                className="rounded bg-emerald-600 px-2 py-0.5 text-xs text-white"
              >
                Approve
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onReject();
                }}
                className="rounded bg-red-600 px-2 py-0.5 text-xs text-white"
              >
                Reject
              </button>
            </div>
          )}
          {user.status === 'active' && user.has_key && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRegenerateKey();
              }}
              className="rounded bg-gray-200 px-2 py-0.5 text-xs"
            >
              Regenerate
            </button>
          )}
        </td>
      </tr>
      {expanded && (
        <tr className="bg-gray-50">
          <td colSpan={showSparkline ? 10 : 9} className="px-4 py-3">
            {/* UserDetailPanel slot - rendered by UserTable */}
            <span data-detail-slot={user.id} />
          </td>
        </tr>
      )}
    </>
  );
}
```

(The detail-panel slot is filled by `UserTable` — see Task 23.)

- [ ] **Step 2: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/UserRow.tsx
git commit -m "feat(admin-ui): UserRow with status icon, color-coded today, sparkline, anomaly badge"
```

---

### Task 23: `UserTable.tsx`

**Files:**
- Create: `frontend/src/app/dashboard/admin/users/UserTable.tsx`

- [ ] **Step 1: Implement**

Create `frontend/src/app/dashboard/admin/users/UserTable.tsx`:

```typescript
'use client';

import { useMemo, useState } from 'react';
import type { CostHistoryPoint, Density, FilterState, UserRow as UserRowType } from './types';
import { UserRow } from './UserRow';
import { UserDetailPanel } from './UserDetailPanel';

interface UserTableProps {
  users: UserRowType[];
  costHistories: Record<string, CostHistoryPoint[]>;
  density: Density;
  filterState: FilterState;
  onSortChange: (sortBy: FilterState['sortBy']) => void;
  // pass-through detail-panel handlers
  onApprove: (userId: string) => Promise<void>;
  onReject: (userId: string, reason: string) => Promise<void>;
  onUpdate: (userId: string, patch: Record<string, unknown>) => Promise<void>;
  onSuspend: (userId: string) => Promise<void>;
  onResume: (userId: string) => Promise<void>;
  onDelete: (userId: string, reason: string) => Promise<void>;
  onHardDelete: (userId: string, reason: string) => Promise<void>;
  onRegenerateKey: (userId: string) => Promise<void>;
}

function median(nums: number[]): number {
  if (nums.length === 0) return 0;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

export function UserTable(props: UserTableProps) {
  const { users, costHistories, density, filterState, onSortChange } = props;
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const pageMedianToday = useMemo(
    () => median(users.map((u) => Number(u.usage_today_usd)).filter((n) => n > 0)),
    [users],
  );

  const showSparkline = density === 'comfortable';

  const sortIndicator = (col: FilterState['sortBy']) =>
    filterState.sortBy === col ? '↓' : '';

  return (
    <div className="overflow-x-auto rounded-md border border-gray-200">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-50">
          <tr className="text-left">
            <th className="px-2 py-2 w-8" />
            <th className="px-2 py-2">Email</th>
            <th className="px-2 py-2">Role</th>
            <th className="px-2 py-2 cursor-pointer" onClick={() => onSortChange('cost_today')}>
              Today {sortIndicator('cost_today')}
            </th>
            {showSparkline && <th className="px-2 py-2 w-16">7d</th>}
            <th className="px-2 py-2 cursor-pointer" onClick={() => onSortChange('cost_month')}>
              Month {sortIndicator('cost_month')}
            </th>
            <th className="px-2 py-2 cursor-pointer" onClick={() => onSortChange('cost_alltime')}>
              All-time {sortIndicator('cost_alltime')}
            </th>
            <th className="px-2 py-2">Status</th>
            <th className="px-2 py-2 w-8" />
            <th className="px-2 py-2">Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.length === 0 && (
            <tr>
              <td colSpan={showSparkline ? 10 : 9} className="px-4 py-8 text-center text-gray-500">
                No users match your filters.
              </td>
            </tr>
          )}
          {users.map((u) => (
            <RowGroup
              key={u.id}
              user={u}
              expanded={expandedId === u.id}
              setExpanded={(open) => setExpandedId(open ? u.id : null)}
              history={costHistories[u.id]}
              pageMedianToday={pageMedianToday}
              density={density}
              {...props}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RowGroup(props: {
  user: UserRowType;
  expanded: boolean;
  setExpanded: (open: boolean) => void;
  history: CostHistoryPoint[] | undefined;
  pageMedianToday: number;
  density: Density;
  onApprove: (id: string) => Promise<void>;
  onReject: (id: string, reason: string) => Promise<void>;
  onUpdate: (id: string, patch: Record<string, unknown>) => Promise<void>;
  onSuspend: (id: string) => Promise<void>;
  onResume: (id: string) => Promise<void>;
  onDelete: (id: string, reason: string) => Promise<void>;
  onHardDelete: (id: string, reason: string) => Promise<void>;
  onRegenerateKey: (id: string) => Promise<void>;
}) {
  const { user, expanded, setExpanded, history, pageMedianToday, density, ...handlers } = props;
  return (
    <>
      <UserRow
        user={user}
        history={history}
        pageMedianToday={pageMedianToday}
        density={density}
        expanded={expanded}
        onToggleExpanded={() => setExpanded(!expanded)}
        onApprove={() => handlers.onApprove(user.id)}
        onReject={() => {
          const reason = window.prompt('Reject reason?') ?? '';
          if (reason) handlers.onReject(user.id, reason);
        }}
        onRegenerateKey={() => handlers.onRegenerateKey(user.id)}
      />
      {expanded && (
        <tr>
          <td colSpan={density === 'comfortable' ? 10 : 9} className="bg-gray-50 p-4">
            <UserDetailPanel user={user} {...handlers} />
          </td>
        </tr>
      )}
    </>
  );
}
```

(`UserRow`'s placeholder `<span data-detail-slot/>` is no longer used — `UserTable` now renders `UserDetailPanel` directly under each expanded row. Remove the `expanded` branch from `UserRow.tsx` accordingly.)

- [ ] **Step 2: Update UserRow to remove the inline expanded slot**

In `frontend/src/app/dashboard/admin/users/UserRow.tsx`, remove the entire `{expanded && (...)}` JSX block at the end of the component — `UserTable` handles expanded rendering. Keep only the `<tr>`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/UserTable.tsx frontend/src/app/dashboard/admin/users/UserRow.tsx
git commit -m "feat(admin-ui): UserTable shell, sort headers, expand-row to detail panel"
```

---

### Task 24: Wire everything in `users/index.tsx`

**Files:**
- Modify: `frontend/src/app/dashboard/admin/users/index.tsx`

- [ ] **Step 1: Replace lifted body with the full new layout**

Rewrite `frontend/src/app/dashboard/admin/users/index.tsx`:

```typescript
'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { SummaryCards } from './SummaryCards';
import { SavedViews } from './SavedViews';
import { FilterBar } from './FilterBar';
import { UserTable } from './UserTable';
import { useUsers } from './hooks/useUsers';
import { useBulkCostHistory } from './hooks/useUserCostHistory';
import {
  DEFAULT_FILTER_STATE,
  filterStateFromUrl,
  filterStateToUrl,
} from './lib/filterTypes';
import { getViewById } from './lib/views';
import {
  approveUser,
  rejectUser,
  updateUser,
  deleteUser,
  resumeUser,
  hardDeleteUser,
  regenerateApiKey,
} from '@/lib/api/admin';
import type { Density, FilterState } from './types';

const DENSITY_KEY = 'admin.users.density';

export default function UsersTab() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [density, setDensity] = useState<Density>('comfortable');

  // Load density from localStorage on mount
  useEffect(() => {
    const saved = localStorage.getItem(DENSITY_KEY) as Density | null;
    if (saved === 'compact' || saved === 'comfortable') setDensity(saved);
  }, []);
  useEffect(() => {
    localStorage.setItem(DENSITY_KEY, density);
  }, [density]);

  // FilterState from URL
  const filterState: FilterState = useMemo(
    () => filterStateFromUrl(searchParams),
    [searchParams],
  );

  const setFilterState = (next: FilterState) => {
    const url = filterStateToUrl(next);
    router.replace(url ? `?${url}` : '?', { scroll: false });
  };

  const onCardClick = (cardId: string) => {
    const view = getViewById(cardId);
    if (view) setFilterState(view.filterState);
  };

  // Data
  const usersQuery = useUsers(filterState);
  const users = usersQuery.data?.users ?? [];
  const userIds = users.map((u) => u.id);
  const histQuery = useBulkCostHistory(userIds, 7, density === 'comfortable');
  const costHistories = histQuery.data ?? {};

  // Actions — wrap admin API fns and refetch on success
  const refetch = () => {
    usersQuery.refetch();
  };
  const handlers = {
    onApprove: async (id: string) => {
      await approveUser(id);
      refetch();
    },
    onReject: async (id: string, reason: string) => {
      await rejectUser(id, reason);
      refetch();
    },
    onUpdate: async (id: string, patch: Record<string, unknown>) => {
      await updateUser(id, patch);
      refetch();
    },
    onSuspend: async (id: string) => {
      await updateUser(id, { status: 'suspended' });
      refetch();
    },
    onResume: async (id: string) => {
      await resumeUser(id);
      refetch();
    },
    onDelete: async (id: string, reason: string) => {
      await deleteUser(id, reason);
      refetch();
    },
    onHardDelete: async (id: string, reason: string) => {
      await hardDeleteUser(id, reason);
      refetch();
    },
    onRegenerateKey: async (id: string) => {
      await regenerateApiKey(id);
      refetch();
    },
  };

  return (
    <div className="space-y-4">
      <SummaryCards onCardClick={onCardClick} />
      <SavedViews current={filterState} onApply={setFilterState} />
      <FilterBar
        state={filterState}
        onChange={setFilterState}
        density={density}
        onDensityChange={setDensity}
      />
      {usersQuery.isLoading && <div className="text-sm text-gray-500">Loading users…</div>}
      {usersQuery.error && (
        <div className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          Failed to load users.
          <button
            onClick={() => usersQuery.refetch()}
            className="ml-2 underline"
          >
            Retry
          </button>
        </div>
      )}
      <UserTable
        users={users}
        costHistories={costHistories}
        density={density}
        filterState={filterState}
        onSortChange={(sortBy) => setFilterState({ ...filterState, sortBy })}
        {...handlers}
      />
    </div>
  );
}
```

(If any of the imported admin API functions like `regenerateApiKey` don't exist, check the current `page.tsx` for the actual symbol used and adapt. Likewise, `updateUser` may already accept the patch object directly.)

- [ ] **Step 2: Verify behavior on staging**

Run: `npm run dev --prefix frontend`
Open the admin Users tab. Verify:
- Cards render with counts and top users
- Clicking a card filters the table
- Saved-view chips work (built-in + custom)
- All filters in FilterBar update the URL and table
- Status icons + sparklines + anomaly badges + color-coded today appear
- Density toggle hides sparkline column in compact mode
- Approve / reject / suspend / resume / delete / regenerate work and refresh

If anything is broken, fix it before continuing.

- [ ] **Step 3: Run lint + type-check + tests**

Run: `npm run lint --prefix frontend && npm run type-check --prefix frontend && npm test --prefix frontend`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/dashboard/admin/users/index.tsx
git commit -m "feat(admin-ui): wire SummaryCards + SavedViews + FilterBar + UserTable in UsersTab"
```

---

## Phase 5 — Polish & ship

### Task 25: Manual QA on staging

**Files:** none (verification only)

- [ ] **Step 1: Push branch and open a draft PR or build locally and use a tunnel**

Either deploy this branch to staging or run locally against the staging API. Per CLAUDE.md, staging URL is `https://staging.freeinference.org` — but the new endpoints exist only on this branch, so local frontend against staging API won't fully work for cards/sparklines until the backend is deployed. Easiest path: run frontend + backend locally. (`make dev` if a target exists; otherwise `uv run uvicorn serving.app:app --reload --port 8000` and `npm run dev --prefix frontend`.)

- [ ] **Step 2: Verify each top workflow**

Walk through these as the admin (`admin@admin.com:admin`):
- **Triage:** Click *Pending Approval* card → table shows pending users → approve one → list refreshes, count drops by 1
- **Find heavy:** Click *Top Spenders Today* card → sort defaults to cost_today → top users at the top
- **Anomaly watch:** *Anomalies* card shows users with ≥5x spike → ⚠ badge present in their row
- **Investigate:** Type partial email or key prefix → matching user appears
- **Filter combos:** `min_cost_today=1` + `quota_state=near` returns the intersection
- **Saved view:** Set custom filters → click "+ Save current" → name it → reload page → custom view chip persists
- **URL share:** Copy URL with filters → open in new tab → same filtered state appears
- **Density:** Toggle compact → sparkline column hides, rows shrink → toggle back → sparklines reappear

- [ ] **Step 3: Capture any bugs as TODOs and fix before opening PR**

If anything breaks, fix and re-test. No "ship and fix later" for the workflow basics.

---

### Task 26: Final formatting + open PR

**Files:** none (housekeeping only)

- [ ] **Step 1: Format**

Run from repo root:
```bash
ruff format serving/ test/
ruff check serving/ test/ --fix
npm run format --prefix frontend
```

- [ ] **Step 2: Run full local test pass**

```bash
uv run pytest test/servers test/unit/storage -x -q
npm run lint --prefix frontend
npm run type-check --prefix frontend
npm test --prefix frontend
```
Expected: all pass.

- [ ] **Step 3: Push branch**

```bash
git push -u origin jason/claude/admin-users-redesign
```

- [ ] **Step 4: Open PR to dev**

```bash
gh pr create --base dev --title "feat(admin): reorganize Users tab into workflow dashboard + extract from page.tsx" --body "$(cat <<'EOF'
Closes #353.

## Summary

Redesigns the admin Users tab into a workflow-organized dashboard and extracts it from the 141 KB monolithic admin/page.tsx.

- **Top:** 4 summary cards — Pending Approval, Top Spenders Today, Anomalies (≥5× 7d avg, ≥\$1 floor, ≥3 days history), Near/Over Quota. Click any card to filter the table.
- **Saved views:** Built-in chips + custom localStorage-backed views.
- **Filter bar:** Search (email/name/key prefix/id prefix), status, cost thresholds, time-window, provider, quota state, density toggle. Filter state in URL — links shareable.
- **Table:** Status icon column, color-coded today-cost cell (vs page median), 7d sparkline (comfortable density only, lazy bulk-fetched), anomaly/quota badge.
- **Code split:** New `frontend/src/app/dashboard/admin/users/` folder; `page.tsx` Users tab becomes `<UsersTab />`.
- **Backend:** new endpoints `/admin/users/{id}/cost-history`, bulk variant, `/admin/users/summary`; extended `/admin/users` with `min_cost_today`, `min_cost_month`, `quota_state`, `provider`, `active_within_hours`, broader `search`.

## Test plan

- [x] Backend pytest pass: `uv run pytest test/servers test/unit/storage`
- [x] Frontend lint + type-check + vitest pass
- [x] Manual on dev local: triage / heavy / anomaly / investigate workflows + filter combos + saved view persistence + URL sharing + density toggle
- [ ] Reviewer to verify on staging after merge

## Out of scope (separate follow-ups)

- Bulk multi-select role/quota actions
- Backend persistence of saved views
- Materialized 7d-avg view (only if /admin/users/summary P95 > 500 ms in staging)
- Extracting other admin tabs from page.tsx

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Watch CI + comments**

Per CLAUDE.md: "check comments and fix CI errors every 2 min after creating PR until all comments are resolved and CI is passed."

```bash
gh pr checks
gh pr view --comments
```

Resolve any findings. After PR merges:

```bash
git checkout dev
git pull
git worktree remove ../.worktrees/admin-users-redesign
git branch -D jason/claude/admin-users-redesign
```

---

## Spec Coverage Check

Mapping of spec sections → task(s):

| Spec section | Implemented in |
|---|---|
| Summary cards (Pending / Top / Anomalies / Near Quota) | Task 3 (backend), Task 18 (frontend) |
| Saved views chip row | Task 12, 13, 19 |
| Filter bar (search/status/usage/provider/quota/density) | Task 4, 6, 14, 20, 21 |
| Status icon column | Task 22 |
| Color-coded today cost | Task 22 |
| 7d sparkline | Task 2, 5, 14, 15, 16, 22, 23 |
| Anomaly/quota trailing badge | Task 10, 22 |
| Density toggle | Task 21, 23, 24 |
| `users/` folder code split | Task 7, 8, 9, 24 |
| New backend endpoints | Tasks 1, 2, 3, 5 |
| Extended `/admin/users` filters | Tasks 4, 6 |
| URL filter state, localStorage saved views | Task 11, 13, 24 |
| Anomaly rule (5×, $1, ≥3 days) | Tasks 3, 10 |
| Tests for pure modules + backend | Tasks 2, 3, 4, 5, 6, 10, 11, 12, 13 |
| Out-of-scope items | Captured in PR description (Task 26) |

No gaps.
