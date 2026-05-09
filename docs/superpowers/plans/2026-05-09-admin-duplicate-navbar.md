# Admin Duplicate Navbar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplicate admin dashboard navbar/header on `/dashboard/admin` while keeping the shared bottom admin tab navigation as the only admin shell.

**Architecture:** The shared admin shell already lives in `apps/frontend/src/app/dashboard/admin/layout.tsx`, so the implementation should leave that layout in place and trim `apps/frontend/src/app/dashboard/admin/page.tsx` down to landing-page content plus page-local controls only. A small regression test should prove the landing page no longer renders its own duplicate `Admin` heading or duplicate dashboard back link when rendered under the shared layout.

**Tech Stack:** Next.js App Router, React 18, TypeScript, Vitest, Testing Library

---

### Task 1: Remove The Duplicate Admin Shell From The Landing Page

**Files:**
- Create: `apps/frontend/src/app/dashboard/admin/__tests__/AdminPage.test.tsx`
- Modify: `apps/frontend/src/app/dashboard/admin/page.tsx`
- Test: `apps/frontend/src/app/dashboard/admin/__tests__/AdminPage.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/app/dashboard/admin/__tests__/AdminPage.test.tsx` with a regression test that renders the admin landing page content inside a wrapper that simulates the shared layout shell. Mock the heavy child tabs and auth wrappers so the test stays focused on duplicate shell markup.

```tsx
// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import AdminPage from '../page';

vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      loading: false,
      isAuthenticated: true,
      user: { is_admin: true },
    },
  }),
}));

vi.mock('../users/index', () => ({
  default: () => <div data-testid="users-tab">Users tab</div>,
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    listRecentRequests: vi.fn(async () => ({ requests: [], total: 0 })),
    getRecentRequestMetrics: vi.fn(async () => ({
      total_requests: 0,
      success_rate: 0,
      avg_latency_ms: 0,
      total_cost_usd: 0,
    })),
  };
});

describe('AdminPage', () => {
  it('does not render a duplicate admin shell when shown inside the shared admin layout', async () => {
    render(
      <div>
        <a href="/dashboard">Dashboard</a>
        <h1>Admin</h1>
        <nav aria-label="Admin tabs">
          <a href="/dashboard/admin/users">Users</a>
        </nav>
        <AdminPage />
      </div>,
    );

    expect(screen.getAllByRole('heading', { name: 'Admin' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Dashboard' })).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument();
    expect(await screen.findByTestId('users-tab')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run from `apps/frontend`:

```bash
npm run test -- src/app/dashboard/admin/__tests__/AdminPage.test.tsx
```

Expected: FAIL because the current `AdminPage` renders another `Dashboard` link and another `Admin` heading inside `page.tsx`, so the length assertions are `2` instead of `1`.

- [ ] **Step 3: Write the minimal implementation**

Edit `apps/frontend/src/app/dashboard/admin/page.tsx` so the main authenticated render block removes only the duplicated shell markup and keeps the page-local refresh control plus the existing tab content. The top of the returned JSX should change from the current shell wrapper to this shape:

```tsx
  return (
    <ProtectedRoute>
      <div className="mx-auto w-full max-w-4xl pb-20">
        <div className="mb-10 flex items-center justify-end">
          <button
            onClick={refreshActiveTab}
            disabled={auditLoading || reqLoading || reqMetricsLoading || providerQuotasLoading}
            className="text-[13px] text-gray-400 transition hover:text-gray-900 disabled:opacity-40"
          >
            {auditLoading || reqLoading || reqMetricsLoading || providerQuotasLoading
              ? 'Loading...'
              : 'Refresh'}
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-lg bg-red-50 px-4 py-2.5 text-[13px] text-red-600">
            {error}{' '}
            <button onClick={() => setError(null)} className="ml-2 font-bold">
              &times;
            </button>
          </div>
        )}
        {toast && (
          <div className="mt-4 rounded-lg bg-gray-900 px-4 py-2.5 text-[13px] text-white">
            {toast}
          </div>
        )}

        {activeTab === 'users' && <UsersTab />}
```

Delete these blocks from the authenticated branch in `page.tsx` and do not replace them elsewhere:

```tsx
        {/* Nav */}
        <div className="mb-10 flex items-center justify-between">
          <a href="/dashboard" ...>
            ...
          </a>
          <button ...>...</button>
        </div>

        {/* Title */}
        <h1 className="text-[28px] font-bold tracking-tight text-gray-900">Admin</h1>
        <p className="mt-0.5 text-[15px] text-gray-500">
          Manage users, API keys, quotas, and audit log.
        </p>

        {/* Top-level tab toggle */}
        <div className="mt-6 flex items-center gap-1">
          ...
        </div>
```

Also delete the now-unused `onTabChange` helper from `page.tsx`, because once the duplicate tab row is removed there is no remaining caller for it.

- [ ] **Step 4: Run the targeted test to verify it passes**

Run from `apps/frontend`:

```bash
npm run test -- src/app/dashboard/admin/__tests__/AdminPage.test.tsx
```

Expected: PASS, with exactly one `Admin` heading, exactly one `Dashboard` link, and the `Refresh` button still visible.

- [ ] **Step 5: Run focused verification for side effects**

Run from `apps/frontend`:

```bash
npm run lint
npm run type-check
```

Expected: PASS. If either command fails, fix the specific issue before moving on. Pay special attention to any lint or type errors caused by deleting `onTabChange` or other now-unused state.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/app/dashboard/admin/page.tsx apps/frontend/src/app/dashboard/admin/__tests__/AdminPage.test.tsx
git commit -m "fix(admin): remove duplicate landing page navbar"
```

Only create this commit if the user explicitly asks for a commit.
