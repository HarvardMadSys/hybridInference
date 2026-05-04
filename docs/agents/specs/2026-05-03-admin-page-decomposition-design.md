# Admin Page Decomposition — Design

**Date:** 2026-05-03
**Status:** Draft → ready for plan
**Author:** Architecture review follow-up (issue #5 of 6)

## Problem

[`apps/frontend/src/app/dashboard/admin/page.tsx`](../../../apps/frontend/src/app/dashboard/admin/page.tsx) is a single React client component of **3343 lines** mixing six administrative concerns (Users, Audit, Broadcasts, Provider Performance, Analytics, Settings) plus 32 inline `useState` calls and a tab switcher. Three tabs (Provider Performance, Analytics, Settings) have already been extracted to component files but are still imported and rendered from inside the monolith.

Problems:

- Hard to test, refactor, or delegate.
- Every admin code change forces re-reading 3343 lines.
- Tab content is non-deep-linkable — `/dashboard/admin` is the only URL; refresh always returns the user to the default tab.
- No code-splitting between tabs; loading the heaviest tab (Users) ships the bundle for all six.
- 32 `useState` calls in one component encourage racey, ad-hoc state changes.

## Goals

1. Split `dashboard/admin/page.tsx` into six per-tab Next.js sub-routes under a shared `AdminLayout`.
2. URL determines the active tab — deep-linkable, refresh-safe, browser-back works.
3. Per-route code-splitting via Next.js App Router conventions.
4. `admin/page.tsx` becomes a thin server-side redirect to the first tab.
5. Land in **one PR** — single cohesive shape change reviewed in one pass.

## Non-goals

- `useState` → `useReducer` / state-machine library refactor. Tab-internal state stays as is; the monolith was the problem, not `useState`.
- Move `AuthProvider` into React Query. Separate concern, separate blast radius.
- Tests for tab-internal behavior (form validation, list operations). Beyond the smoke tests defined here, full coverage is a separate effort.
- Refactor of the playground page (`dashboard/playground/page.tsx`, 820 lines) — same problem, separate brainstorm.
- API client (`/lib/api/admin.ts`) changes.

## Architecture

### Approach: Next.js App Router sub-routes per tab

Use a route group `(tabs)` so the shared layout exists without prefixing the URL. URLs like `/dashboard/admin/users`, `/dashboard/admin/audit`, etc.

### Tab inventory

| Tab | Current location | Destination | Notes |
|---|---|---|---|
| Users | inline in `page.tsx` (largest chunk; ~12 useState) | `(tabs)/users/page.tsx` + `components/features/admin/UsersTab.tsx` | Most operations; highest complexity; first to extract |
| Audit | inline (~5 useState) | `(tabs)/audit/page.tsx` + `AuditTab.tsx` | Read-only browsing |
| Broadcasts | inline (~8 useState) | `(tabs)/broadcasts/page.tsx` + `BroadcastsTab.tsx` | Authoring + preview |
| Provider Performance | extracted (`ProviderPerformanceTab.tsx`, 402 lines) | `(tabs)/provider-performance/page.tsx` thin wrapper | Already a component |
| Analytics | extracted (`AnalyticsTab.tsx`, 271 lines) | `(tabs)/analytics/page.tsx` thin wrapper | Already a component |
| Settings | extracted (`SettingsTab.tsx`, 283 lines) | `(tabs)/settings/page.tsx` thin wrapper | Already a component; minor toast cleanup possible later |

### File layout

```
apps/frontend/src/app/dashboard/admin/
  layout.tsx                       # NEW — admin shell: role gate + tab nav
  page.tsx                         # NEW — server component, redirect('/dashboard/admin/users')
  (tabs)/
    users/page.tsx                 # NEW — renders <UsersTab />
    audit/page.tsx                 # NEW — renders <AuditTab />
    broadcasts/page.tsx            # NEW — renders <BroadcastsTab />
    provider-performance/page.tsx  # NEW — renders <ProviderPerformanceTab />
    analytics/page.tsx             # NEW — renders <AnalyticsTab />
    settings/page.tsx              # NEW — renders <SettingsTab />

apps/frontend/src/components/features/admin/
  AdminTabNav.tsx                  # NEW — six <Link> elements, highlights active via usePathname()
  UsersTab.tsx                     # NEW — extracted from page.tsx Users section
  AuditTab.tsx                     # NEW — extracted from page.tsx Audit section
  BroadcastsTab.tsx                # NEW — extracted from page.tsx Broadcasts section
  ProviderPerformanceTab.tsx       # EXISTING (no change beyond default-export verification)
  AnalyticsTab.tsx                 # EXISTING
  SettingsTab.tsx                  # EXISTING
```

After the PR, `apps/frontend/src/app/dashboard/admin/page.tsx` is a server component:

```tsx
import { redirect } from 'next/navigation';

export default function AdminPage() {
  redirect('/dashboard/admin/users');
}
```

The 3343-line previous body is gone (split across the new files).

### `layout.tsx` — admin shell

```tsx
'use client';
import { useAuth } from '@/components/providers/AuthProvider';
import { useRouter } from 'next/navigation';
import { useEffect } from 'react';
import { AdminTabNav } from '@/components/features/admin/AdminTabNav';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user?.is_admin) router.replace('/dashboard');
  }, [loading, user, router]);

  if (loading || !user?.is_admin) return <div>Loading...</div>;

  return (
    <div className="admin-shell">
      <AdminTabNav />
      <main>{children}</main>
    </div>
  );
}
```

`AdminTabNav` is ~30 lines: array of `{ slug, label }`, mapped to `<Link>` with active highlight via `usePathname()`.

### Per-tab routes

Already-extracted tabs are 3-line wrappers:

```tsx
// (tabs)/provider-performance/page.tsx
'use client';
import ProviderPerformanceTab from '@/components/features/admin/ProviderPerformanceTab';
export default function Page() { return <ProviderPerformanceTab />; }
```

Newly-extracted tabs (Users, Audit, Broadcasts) get full component files in `components/features/admin/`. Each owns its own `useState`, hooks (`useUsers()`, `useAudit()`, etc.), and JSX — extracted verbatim from the monolith. A 3-line route wrapper imports and renders.

### Deep-linking with URL search params

Where a tab today uses `useState` for filter/pagination state that users would reasonably expect to survive a refresh, use `useSearchParams()` and update via `router.replace(...)` — for example:

- Users tab: `?search=foo&page=2&role=admin`.
- Audit tab: `?from=2026-05-01&user_id=u1`.

Internal modal/draft state stays in `useState`.

### State that crosses tabs

None. Each tab unmounts on switch. URL params preserve filter state where it matters; everything else is intentionally tab-local.

### Auth gate

Today the page checks role inline at the top of `admin/page.tsx`. Move that check into `AdminLayout` so it covers all six routes. Behavior matches today: non-admin users hitting any `/dashboard/admin/*` URL are redirected to `/dashboard`.

### What stays the same

- `AuthProvider` — out of scope.
- `/lib/api/admin.ts` — out of scope (already 768 lines but mostly type interface declarations; not flagged).
- Other dashboard routes (`/dashboard`, `/dashboard/playground`, `/dashboard/settings`) — untouched.
- Existing extracted tab components — wrapped, not modified.

## Testing strategy

Frontend test coverage is thin (4 test files repo-wide). We add minimal smoke coverage and rely on manual QA:

| Layer | What | Tooling |
|---|---|---|
| Build / typecheck | `next build` + `tsc --noEmit` succeed. | existing Next.js / CI |
| Smoke route tests (6 tests, 1 per tab) | Each route renders without crashing and shows a tab-specific anchor (heading text). | Vitest + `@testing-library/react` (or Playwright if already configured — check before adding) |
| Manual QA checklist (in PR description) | 1 pass through each tab as admin user on staging: open user detail; edit a role; expand audit row; draft broadcast preview; view analytics charts; change a setting; assert non-admin redirect. | Manual |

No tests are added for tab-internal logic — pre-existing untested territory; deferring is intentional.

## Risk + rollback

| Risk | Mitigation |
|---|---|
| Lost feature: inline behavior in monolith doesn't make it to the new tab file | Mechanical extraction; manual QA checklist exercises every tab; smoke tests catch render-level breaks. |
| Auth gate broken | `AdminLayout` enforces `is_admin` redirect before rendering; smoke test asserts non-admin redirect. |
| Bookmarks broken | Today, all admin tabs share `/dashboard/admin`; tab state was in-memory only. There are no deep-linked admin tab bookmarks to break. The new URLs are an improvement. |
| Active-tab highlight wrong on direct URL hit | `AdminTabNav` reads `usePathname()`; smoke test asserts highlight matches URL. |
| Per-tab state cleared on switch (e.g., typed filter, switched tabs, switched back, filter gone) | Acceptable — switching tabs is deliberate. URL params preserve filters where users expect. |
| Existing extracted tabs depend on props from parent | Verify pre-extraction; if so, hoist props' sources into the wrapper or refactor the tab to fetch its own data. |
| Extracted tab files balloon (extracted code + own state hooks) | Per-tab files are 300-600 lines vs. the 3343-line monolith — manageable. If a tab grows, separate cleanup PR. |

**Rollback:** single `git revert`. No data state involved.

## Performance

- Sub-route navigation triggers Next.js client-side route change (~100ms perceived); shared `AdminLayout` doesn't re-mount.
- Per-route code-splitting: initial `/dashboard/admin/users` load doesn't ship Audit/Broadcasts/Analytics code. Modest bundle-size win.
- React Query caches survive route changes within `QueryClient`; tab-revisit cost stays low.

## Open questions (resolved)

| Question | Resolution |
|---|---|
| `useState` → `useReducer` in this PR? | No — out of scope. |
| `AuthProvider` → React Query in this PR? | No — separate refactor. |
| What happens to `activeTab` state in old `page.tsx`? | Deleted; URL replaces it. |
| Sidebar or top tabs? | Match existing inline UI — minimal visual change. |
| `/dashboard/admin` (no tab)? | Server-side redirect to `/dashboard/admin/users`. |

## Out-of-scope follow-ups

- State-machine / `useReducer` refactor of Users tab (the most complex; benefits most).
- React-Query-backed `useAuth` replacing the Context-only `AuthProvider`.
- Playground page extraction (`dashboard/playground/page.tsx`, 820 lines).
- Component-level tests for the extracted tabs.
- Decompose [completions.py](../../../apps/backend/serving/servers/routers/completions.py) (issue #2 — spec exists).
- Schema migrations (issue #3 — spec exists).
- Tracked fire-and-forget tasks (issue #4 — spec exists).
- Routing config expressiveness (issue #6).
