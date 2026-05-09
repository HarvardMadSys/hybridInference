# Provider Performance Tab — Compact Layout

**Date:** 2026-05-09
**Scope:** [apps/frontend/src/app/dashboard/admin/ProviderPerformanceTab.tsx](../../../apps/frontend/src/app/dashboard/admin/ProviderPerformanceTab.tsx)

## Problem

Provider performance dashboard wastes vertical space. KPI cards (3 per row, `p-4`, `text-2xl` value) and chart card padding/whitespace dominate the layout. Charts — the actual content — are pushed below the fold.

## Goal

Tighten chrome around charts so charts sit higher and more fits per viewport. Preserve all data and chart fidelity. No backend changes.

## Out of scope

- Backend API changes
- Removing or restructuring charts
- Adding sparkline/table views
- Mobile-specific layouts (existing responsive breakpoints retained)

## Changes

### 1. Overall KPI strip

Replace the 3-card grid (lines 360–366) with a single inline strip.

**Before:**
```tsx
<div className="grid grid-cols-1 md:grid-cols-3 gap-4">
  <Kpi label="Total requests" value={...} />
  <Kpi label="Error rate" value={...} />
  <Kpi label="Completion tokens" value={...} />
</div>
```

**After:**
```tsx
<div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[12px]">
  <span><span className="text-gray-500">Total requests </span><span className="font-semibold text-gray-900 tabular-nums">{...}</span></span>
  <span className="text-gray-300">·</span>
  <span><span className="text-gray-500">Error rate </span><span className="font-semibold text-gray-900 tabular-nums">{...}</span></span>
  <span className="text-gray-300">·</span>
  <span><span className="text-gray-500">Completion tokens </span><span className="font-semibold text-gray-900 tabular-nums">{...}</span></span>
</div>
```

No card chrome. One row on desktop, wraps on narrow screens.

### 2. Per-model heading + inline KPIs

Replace per-model heading + 3-card KPI grid (lines 156–164) with single row: model name (semibold) on left, KPI strip on right.

**Before:**
```tsx
<div className="flex items-center gap-2">
  <h3 className="text-[14px] font-semibold text-gray-900">{modelId}</h3>
</div>

<div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
  <Kpi label="Requests" value={...} />
  <Kpi label="Error rate" value={...} />
  <Kpi label="Completion tokens" value={...} />
</div>
```

**After:**
```tsx
<div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
  <h3 className="text-[13px] font-semibold text-gray-900">{modelId}</h3>
  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[12px] text-gray-600 tabular-nums">
    <span>{requests} req</span>
    <span className="text-gray-300">·</span>
    <span>{errorRate}% err</span>
    <span className="text-gray-300">·</span>
    <span>{tokens} tok</span>
  </div>
</div>
```

Short labels (`req`, `err`, `tok`). Existing `data-testid` attributes (if any) retained — none currently on the model heading.

### 3. Chart cards (TTFT + Throughput)

In `ModelPerformanceSection` (lines 170, 187):
- `rounded-xl border p-3` → `rounded-xl border p-2`
- Title `mb-1 text-[13px] font-semibold` → `mb-0.5 text-[12px] font-semibold`
- `h-48` → `h-40`

`data-testid` attributes preserved.

### 4. TTFT scatter card

In `TtftScatterCard` (lines 52, 71):
- `p-4` → `p-3`
- Header row: collapse legend onto same line as title (right-aligned). Keep colored dots + counts.
- `h-[260px]` → `h-48`
- ScatterChart margin `bottom: 24` → `bottom: 18`
- Axis tick fontSize `10` retained, label fontSize `11` retained
- Axis label `offset: -10` → `offset: -6` to fit reduced bottom margin

### 5. Section spacing

- Top wrapper `space-y-6` (line 325) → `space-y-4`
- `ModelPerformanceSection` outer `space-y-4` (line 155) → `space-y-2`
- Chart-row `gap-3` (line 168) → `gap-2`
- Scatter section `mb-3` heading (line 373) → `mb-2`

## Testing

- Existing test: [apps/frontend/src/app/dashboard/admin/__tests__/ProviderPerformanceTab.test.tsx](../../../apps/frontend/src/app/dashboard/admin/__tests__/ProviderPerformanceTab.test.tsx) — must continue to pass.
- Test references `data-testid="provider-performance-chart-row"`, `provider-performance-ttft-card`, `provider-performance-throughput-card` — all retained.
- KPI cards do not have testids — switching from cards to inline spans does not break tests.
- Manual verification on staging (`admin@admin.com` / `admin`) at `/dashboard/admin` Provider Performance tab.

## Risk

Low. Pure presentation change in a single file. No data flow, API, or test surface changes.

## Acceptance

- All charts and numbers render identically to before.
- Vertical scroll length for default `7d` range with 3 models ≥ 35% shorter.
- Existing tests pass without modification.
