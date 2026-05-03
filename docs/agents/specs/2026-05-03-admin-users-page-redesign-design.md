---
status: draft
date: 2026-05-03
author: jason (via Claude)
---

# Admin users page redesign — design

## Problem

The admin Users tab lives inside `frontend/src/app/dashboard/admin/page.tsx`, a ~141 KB monolith that holds all 9 admin tabs. The page is functionally a flat table with status filter tabs, search, and sort. Daily admin work — triaging pending users, spotting heavy spenders, watching for usage anomalies, investigating a specific user — all require manual scanning, repeated sort changes, or context-switching between tabs.

Concrete pain points (ranked by user):

1. **Findability / common-action speed.** Pending triage is the #1 workflow but pending users are buried behind a tab click; heavy spenders require switching sort; anomaly detection isn't supported at all.
2. **Filter/search/sort are limited.** No cost-threshold filter, no time-window filter, no provider/model filter, no quota-state filter, no saved views, search only matches email/name (not key prefix or id).
3. **Visual hierarchy is flat.** Every row looks identical; nothing draws the eye to a $500/day user, an account spiking 10× its average, or a user pinned at quota.
4. **Code organization.** The Users tab logic is inline in the 141 KB `page.tsx` (alongside 8 other tabs), making edits high-friction.

Information density (option A in the original triage) was de-prioritized; the existing expanded-detail panel is fine and stays.

## Goal

Redesign the admin Users tab into a workflow-oriented dashboard, and extract it from the monolithic `page.tsx` into its own component folder.

Top admin workflows the design must serve (in priority order):

1. **Triage pending users** — approve/reject new signups
2. **Find heavy users** — high cost, high volume, abuse cases
3. **Watch for anomalies** — sudden spikes vs baseline
4. **Investigate a specific user** — by email, key prefix, or id

Non-goals:

- Bulk multi-select actions (separate future feature).
- Backend persistence of saved views (localStorage only in this PR).
- Extracting other admin tabs (Analytics/Settings/etc. stay in `page.tsx`).
- Audit-log integration on this page.
- Real-time updates (we poll with React Query, 30 s stale time).

## Architecture

### Page layout (top to bottom)

1. **Summary cards row** — 4 cards, each card = count + 3-5 example rows + click-to-filter:
   - *Pending Approval*
   - *Top Spenders Today*
   - *Anomalies* (today_cost ≥ 5× 7-day avg AND today_cost ≥ $1)
   - *Near / Over Quota* (quota_used ≥ 80%)
2. **Saved views chip row** — built-in chips: *Pending*, *Top spenders today*, *Anomalies*, *Near quota*, *Recently active*, *New this week*. User-saved custom views appended via a `+ Custom view` affordance. Clicking a chip sets the full filter state.
3. **Filter bar (collapsible)** — search input + dropdown filters:
   - **Status** (existing tabs collapsed into a dropdown: All / Pending / Active / Suspended / Rejected / Deleted)
   - **Usage** (cost threshold + time window: today / month / alltime; "active within last N hours")
   - **Provider** (multi-select: providers a user hit in the last 30 days; model-level filtering is out of scope — the expanded detail panel already lists models per user)
   - **Quota** (Default / Custom / Near (≥80%) / Over)
   - **Density** toggle (Comfortable ↔ Compact) on the far right
4. **User table** — see column changes below.
5. **Expanded detail panel** — unchanged; existing layout preserved.

### Table column changes

| Column           | Change                                                                               |
| ---------------- | ------------------------------------------------------------------------------------ |
| Status icon      | **NEW** leftmost, single glyph: `⬤` pending / `▲` suspended / `✕` rejected/deleted / `●` active |
| Email + role     | unchanged                                                                            |
| Today cost       | color-graded vs visible-page median (no tint < 1×, light yellow 1–3×, amber 3–10×, red >10×) |
| 7d sparkline     | **NEW**, ~60×16px, single muted color, comfortable-density only, lazy-rendered       |
| Month + alltime  | unchanged (no color)                                                                 |
| Status text      | unchanged                                                                            |
| Anomaly / quota badge | **NEW** trailing flag column: `⚠` if anomalous, `◐` if near/over quota; mutually exclusive (anomaly wins) |
| Actions          | unchanged                                                                            |

**Per-row visual budget = at most 2 flags** (status icon + one trailing badge). Color-coded cost cells apply only to *today*; month/alltime stay neutral to avoid row noise.

### Frontend file structure

Replace inline Users tab in `page.tsx` with:

```
frontend/src/app/dashboard/admin/users/
├── index.tsx                ← UsersTab entry, owns state + queries
├── SummaryCards.tsx
├── SavedViews.tsx
├── FilterBar.tsx
├── filters/
│   ├── StatusFilter.tsx
│   ├── UsageFilter.tsx
│   ├── ProviderFilter.tsx
│   └── QuotaFilter.tsx
├── UserTable.tsx
├── UserRow.tsx
├── Sparkline.tsx
├── UserDetailPanel.tsx      ← lifted as-is from current page
├── hooks/
│   ├── useUsers.ts
│   ├── useUserCostHistory.ts
│   └── useSavedViews.ts
├── lib/
│   ├── anomaly.ts           ← isAnomalous(today, history[]) pure fn
│   └── filterTypes.ts       ← FilterState type + URL ↔ state codec
├── types.ts
└── __tests__/
    ├── anomaly.test.ts
    ├── filterTypes.test.ts
    ├── useSavedViews.test.ts
    ├── UserRow.test.tsx
    └── FilterBar.test.tsx
```

In `page.tsx` the Users tab body becomes a single `<UsersTab />` import — same convention already used by `AnalyticsTab.tsx` / `SettingsTab.tsx`.

### Backend additions

1. **`GET /admin/users/{user_id}/cost-history?days=7`** — daily buckets `[{day, cost_usd, requests}]` from `user_daily_cost`. Powers single-row sparklines.
2. **`GET /admin/users/cost-history?user_ids=a,b,c&days=7`** — bulk variant; one request per page-load instead of N. Required to avoid N+1 fetches for 50-row pages.
3. **`GET /admin/users/summary`** — returns `{pending: {count, top: [...]}, top_spenders_today: {...}, anomalies: {...}, near_quota: {...}}`. One call powers all 4 summary cards. Anomaly logic runs server-side (today vs prior 7-day average, ≥5× threshold, ≥$1 floor, ≥3 days history required).
4. **Extend `GET /admin/users` query params** — additive (existing callers unaffected):
   - `min_cost_today: float`
   - `min_cost_month: float`
   - `quota_state: "near" | "over" | "custom" | "default"`
   - `provider: str` (matches users who hit that provider in last 30 d)
   - `active_within_hours: int`
   - `search` extended to also match `key_prefix` (case-sensitive, prefix-only) and `id` prefix (UUID prefix), in addition to existing email / user_name match.

All new endpoints live in the admin router package and follow existing auth/authorization patterns (admin-only, `require_admin` dependency).

### Frontend data flow

```
UsersTab (index.tsx)
  ├─ useUsers(filterState)              → /admin/users?...           (table rows)
  ├─ useSummaryStats()                  → /admin/users/summary       (cards)
  └─ useBulkCostHistory(visibleUserIds) → /admin/users/cost-history  (sparklines)
```

- React Query: `staleTime: 30s` on `/admin/users` and `/admin/users/summary`.
- Cost-history cached `5min` (past daily buckets are immutable).
- Sparkline rows render lazy via IntersectionObserver — non-visible rows don't trigger fetches.
- Compact density mode skips the bulk cost-history call entirely.

### URL & persistence

- **Filter state in URL**: `?view=top-spenders` for built-in views; `?status=active&min_cost=10&q=alice` for ad-hoc. Sharable links between admins. Unknown query keys are ignored (forward-compat).
- **Saved custom views** in `localStorage` keyed `admin.users.savedViews` (array of `{name, filterState}`). Built-ins are not editable/deletable.
- **Density preference** in `localStorage` keyed `admin.users.density`.

### Anomaly detection

Server-side in `/admin/users/summary`:

```
is_anomalous(user) =
    user.days_with_history >= 3
    AND user.today_cost >= 1.00
    AND user.today_cost >= 5 * user.avg_cost_prior_7d
    AND user.status == 'active'
```

Implemented as a single SQL query against `user_daily_cost` joined to `users`, computing the 7-day prior average per user and filtering. For initial PR this runs live per request. **Performance note:** at >10 k users, expect ≥500 ms — if measured P95 exceeds 500 ms in staging, add a daily materialized view (`user_daily_cost_avg_7d`) refreshed by the existing daily rollup job. Tracked as a follow-up; not blocking initial PR.

## Error handling

- **Sparkline fetch fails** → row renders without sparkline, no error state, no toast (sparkline is decorative).
- **Summary cards fetch fails** → cards collapse to a single retry banner; the table continues to load independently.
- **Saved views localStorage corrupt/missing** → fall back to built-in views only, log to console, do not crash.
- **Unknown URL filter params** → ignored silently (forward-compat).
- **Empty result states**:
  - "No users match your filters" + *Clear filters* button (filter-driven empty)
  - "No pending users 🎉" or similar neutral message (when a card-driven empty)

## Testing strategy

**Pure-function unit tests:**

- `anomaly.test.ts` — 5× threshold, $1 floor, <3 days history → not anomalous, missing data, exact threshold behavior.
- `filterTypes.test.ts` — round-trip URL ↔ FilterState (both directions, unknown keys ignored, default-state produces empty querystring).

**Hook tests:**

- `useSavedViews.test.ts` — CRUD against in-memory localStorage mock; built-ins immutable.

**Component tests:**

- `UserRow.test.tsx` — flag rendering for each status × condition combination; density toggle hides/shows sparkline; color bucket selection per cost cell.
- `FilterBar.test.tsx` — filter changes update URL; `Clear filters` resets to default; search input is debounced and only updates URL on commit.

**Backend tests (pytest):**

- `/admin/users/summary` — anomaly threshold edge cases (4.99×, 5.0×, 5.01×; $0.99 vs $1.00; 2 vs 3 days history).
- `/admin/users` extended params — each new param filters correctly; combinations narrow as expected; existing tests pass unchanged (param additions are backward compatible).
- `/admin/users/cost-history` — single + bulk; days param; nonexistent user or user with no cost rows → **200 + empty `points` list** (not 404). Returning 200+empty is intentional: the UI shows a flat sparkline rather than an error state, which is less noisy for new users who simply have no daily-cost rows yet.

### Edge cases captured

- New user (<3 days of data) → never anomalous (no day-1 false positives).
- Deleted users → excluded from summary cards.
- Users without API key → sparkline shows flat dash.
- Free-tier users at $0 → color coding stays neutral when median is $0.
- Pagination + summary → card counts reflect *unfiltered* totals; the table reflects current filters. The two layers are independent.

## Out of scope (explicitly)

- Bulk multi-select role/quota changes.
- Backend persistence of saved views (localStorage only).
- Extracting other admin tabs from `page.tsx`.
- Audit-log integration / "who changed what" timeline on this page.
- Real-time / websocket updates.
- New analytics charts beyond the per-row sparkline.

## Open questions / follow-ups

- **Materialized 7-day average view** for anomaly performance — defer until measured.
- **Server-side saved views** if multi-device sharing becomes a need.
- **Bulk actions** as a separate spec once column/row interactions stabilize.
