# Admin Page Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 3343-line `apps/frontend/src/app/dashboard/admin/page.tsx` into six per-tab Next.js sub-routes under a shared `AdminLayout`, with deep-linkable URLs and per-route code-splitting.

**Architecture:** Next.js App Router route group `(tabs)` for the shared shell. Each tab gets its own route file (3-line wrapper) plus a component file under `components/features/admin/`. The new `admin/page.tsx` is a server-side redirect to `/dashboard/admin/users`. State stays as `useState` per tab; URL search params replace `useState` for filter/pagination state where users expect persistence.

**Tech Stack:** Next.js 15.5 (App Router), React 18, TypeScript 5.3, React Query (TanStack 5.17), Tailwind CSS, existing frontend test framework (verify which: Vitest / Playwright / Jest).

**Spec:** [docs/agents/specs/2026-05-03-admin-page-decomposition-design.md](../specs/2026-05-03-admin-page-decomposition-design.md)

**Process notes (from CLAUDE.md):**
- Pull `origin/dev` before starting.
- Single PR on feature branch `jason/claude/admin-page-decomposition`.
- Worktree: `/home/juncheng/hybridInference-worktrees/admin-page-decomposition`.
- Per CLAUDE.md: create issue → branch → implement → format check → PR → monitor CI every 2 min → cleanup after merge.

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `apps/frontend/src/app/dashboard/admin/layout.tsx` | Admin shell: role gate (must be admin) + `<AdminTabNav />`. |
| `apps/frontend/src/app/dashboard/admin/page.tsx` | Server component, `redirect('/dashboard/admin/users')`. |
| `apps/frontend/src/app/dashboard/admin/(tabs)/users/page.tsx` | 3-line wrapper rendering `<UsersTab />`. |
| `apps/frontend/src/app/dashboard/admin/(tabs)/audit/page.tsx` | 3-line wrapper rendering `<AuditTab />`. |
| `apps/frontend/src/app/dashboard/admin/(tabs)/broadcasts/page.tsx` | 3-line wrapper rendering `<BroadcastsTab />`. |
| `apps/frontend/src/app/dashboard/admin/(tabs)/provider-performance/page.tsx` | 3-line wrapper rendering existing `<ProviderPerformanceTab />`. |
| `apps/frontend/src/app/dashboard/admin/(tabs)/analytics/page.tsx` | 3-line wrapper rendering existing `<AnalyticsTab />`. |
| `apps/frontend/src/app/dashboard/admin/(tabs)/settings/page.tsx` | 3-line wrapper rendering existing `<SettingsTab />`. |
| `apps/frontend/src/components/features/admin/AdminTabNav.tsx` | Six `<Link>` elements, highlights active via `usePathname()`. |
| `apps/frontend/src/components/features/admin/UsersTab.tsx` | User management — extracted from monolith. |
| `apps/frontend/src/components/features/admin/AuditTab.tsx` | Audit log browsing — extracted. |
| `apps/frontend/src/components/features/admin/BroadcastsTab.tsx` | Broadcast authoring — extracted. |

### Deleted (the monolithic content)

- The body of the original `apps/frontend/src/app/dashboard/admin/page.tsx` (3343 lines) is gone — replaced by the new server-component redirect.

### Unchanged

- `apps/frontend/src/components/features/admin/ProviderPerformanceTab.tsx`
- `apps/frontend/src/components/features/admin/AnalyticsTab.tsx`
- `apps/frontend/src/components/features/admin/SettingsTab.tsx`
- `apps/frontend/src/components/providers/AuthProvider.tsx` (out of scope)
- `apps/frontend/src/lib/api/admin.ts` (out of scope)

---

## Tasks

### Task 1: Worktree + issue setup

- [ ] **Step 1:** `git fetch origin && git checkout dev && git pull origin dev`
- [ ] **Step 2:** Create issue: `gh issue create --title "Decompose admin page into per-tab Next.js sub-routes" --body "Spec: docs/agents/specs/2026-05-03-admin-page-decomposition-design.md"`
- [ ] **Step 3:** `git worktree add /home/juncheng/hybridInference-worktrees/admin-page-decomposition -b jason/claude/admin-page-decomposition origin/dev`
- [ ] **Step 4:** `cd /home/juncheng/hybridInference-worktrees/admin-page-decomposition/frontend && npm install` (if not already cached) and confirm `npm run build` succeeds.

---

### Task 2: Discover frontend test framework + tab inventory

**Files:** none (research)

- [ ] **Step 1:** Determine the frontend test runner:
  ```bash
  cd /home/juncheng/hybridInference-worktrees/admin-page-decomposition/frontend
  cat package.json | grep -E 'vitest|jest|playwright|@testing-library'
  ls __tests__ 2>/dev/null || ls test 2>/dev/null || find . -name "*.test.tsx" -not -path "./node_modules/*" | head -5
  ```
  Pick the framework already in use. If no test runner is configured, use Vitest + `@testing-library/react` (Vitest is fastest to set up against an existing Next.js project; install with `npm install -D vitest @testing-library/react @testing-library/jest-dom @vitejs/plugin-react jsdom`).

- [ ] **Step 2:** Read `apps/frontend/src/app/dashboard/admin/page.tsx` and confirm tab structure. Look for the tab-switcher (likely `useState<TabName>(...)` plus a `<Tabs>` or conditional render block). Note the JSX boundaries for each of the six tabs:
  - **Users** — search for "User" / "approve" / "reject" / table of users.
  - **Audit** — search for "audit" / audit-log entries.
  - **Broadcasts** — search for "broadcast" / draft / preview.
  - **Provider Performance** — already extracted as `<ProviderPerformanceTab />`.
  - **Analytics** — already extracted as `<AnalyticsTab />`.
  - **Settings** — already extracted as `<SettingsTab />`.

  Confirm role-gate location (likely top of `page.tsx`: a `useAuth()` call + `if (!user.is_admin) redirect('/dashboard')`).

- [ ] **Step 3:** Document findings in commit message of next task.

---

### Task 3: Create `AdminTabNav` component

**Files:** Create `apps/frontend/src/components/features/admin/AdminTabNav.tsx`.

- [ ] **Step 1:** Write component:

  ```tsx
  'use client';
  import Link from 'next/link';
  import { usePathname } from 'next/navigation';

  const TABS = [
    { slug: 'users', label: 'Users' },
    { slug: 'audit', label: 'Audit' },
    { slug: 'broadcasts', label: 'Broadcasts' },
    { slug: 'provider-performance', label: 'Provider Performance' },
    { slug: 'analytics', label: 'Analytics' },
    { slug: 'settings', label: 'Settings' },
  ];

  export function AdminTabNav() {
    const pathname = usePathname();
    return (
      <nav className="flex gap-2 border-b mb-6">
        {TABS.map((tab) => {
          const href = `/dashboard/admin/${tab.slug}`;
          const isActive = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <Link
              key={tab.slug}
              href={href}
              className={[
                'px-4 py-2 text-sm font-medium',
                isActive
                  ? 'border-b-2 border-blue-600 text-blue-600'
                  : 'text-gray-600 hover:text-gray-900',
              ].join(' ')}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    );
  }
  ```

  (Adapt class names to the existing Tailwind conventions in the codebase — open `apps/frontend/src/components/features/admin/AnalyticsTab.tsx` to see the project's class style and match it.)

- [ ] **Step 2:** Commit:
  ```bash
  git add apps/frontend/src/components/features/admin/AdminTabNav.tsx
  git commit -m "feat(admin): add AdminTabNav with active-route highlight"
  ```

---

### Task 4: Create `AdminLayout` shell

**Files:** Create `apps/frontend/src/app/dashboard/admin/layout.tsx`.

- [ ] **Step 1:** Write layout:

  ```tsx
  'use client';
  import { useEffect } from 'react';
  import { useRouter } from 'next/navigation';
  import { useAuth } from '@/components/providers/AuthProvider';
  import { AdminTabNav } from '@/components/features/admin/AdminTabNav';

  export default function AdminLayout({ children }: { children: React.ReactNode }) {
    const { user, loading } = useAuth();
    const router = useRouter();

    useEffect(() => {
      if (!loading && !user?.is_admin) router.replace('/dashboard');
    }, [loading, user, router]);

    if (loading || !user?.is_admin) {
      return <div className="p-8">Loading...</div>;
    }

    return (
      <div className="admin-shell p-6">
        <AdminTabNav />
        <main>{children}</main>
      </div>
    );
  }
  ```

  Match existing `useAuth()` hook signature — open `apps/frontend/src/components/providers/AuthProvider.tsx` to confirm the hook returns `{ user, loading }` (or whatever the actual shape is) and adapt.

- [ ] **Step 2:** Commit:
  ```bash
  git add apps/frontend/src/app/dashboard/admin/layout.tsx
  git commit -m "feat(admin): add AdminLayout with role gate + tab nav"
  ```

---

### Task 5: Replace `admin/page.tsx` with redirect

**Files:** Modify `apps/frontend/src/app/dashboard/admin/page.tsx`.

- [ ] **Step 1:** Save a copy of the current 3343-line file body to a scratch location for reference during extraction (e.g., `cp apps/frontend/src/app/dashboard/admin/page.tsx /tmp/admin-page-original.tsx`).

- [ ] **Step 2:** Replace the entire file with:

  ```tsx
  import { redirect } from 'next/navigation';

  export default function AdminPage() {
    redirect('/dashboard/admin/users');
  }
  ```

  Note: this is a **server component** (no `'use client'` directive) so `redirect()` works directly.

- [ ] **Step 3:** Verify build: `cd frontend && npm run build`. Build will fail because no `(tabs)/users/page.tsx` exists yet. That's expected — the next tasks add it.

- [ ] **Step 4:** Do **NOT** commit yet — leave the build broken until at least the Users tab is in place. (We commit one cohesive working state in Task 6.)

---

### Task 6: Extract Users tab

**Files:**
- Create: `apps/frontend/src/components/features/admin/UsersTab.tsx`
- Create: `apps/frontend/src/app/dashboard/admin/(tabs)/users/page.tsx`

- [ ] **Step 1:** From `/tmp/admin-page-original.tsx`, identify the JSX block that renders the Users tab (the largest tab, with user list, detail modal, edit role/quota controls, hard-delete confirmation).

- [ ] **Step 2:** Identify the supporting state and effects: which `useState` calls and `useQuery` / `useMutation` hooks are used by the Users JSX. They might be intermixed with state for other tabs in the original — be deliberate about pulling only Users-related state.

- [ ] **Step 3:** Create `apps/frontend/src/components/features/admin/UsersTab.tsx`:

  ```tsx
  'use client';
  import { useState } from 'react';
  import { useSearchParams, useRouter } from 'next/navigation';
  // ... import existing API hooks: from '@/lib/api/admin' or '@/lib/hooks/useAdminUsers'
  // ... import any UI primitives the original tab used

  export function UsersTab() {
    const searchParams = useSearchParams();
    const router = useRouter();

    // Filter / pagination state — read from URL, write back via router.replace().
    const search = searchParams.get('search') ?? '';
    const page = Number(searchParams.get('page') ?? '1');
    const role = searchParams.get('role') ?? '';

    const setQueryParam = (key: string, value: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value === null || value === '') params.delete(key);
      else params.set(key, value);
      router.replace(`?${params.toString()}`);
    };

    // Modal / form state — local to component.
    const [detailUserId, setDetailUserId] = useState<string | null>(null);
    const [editRole, setEditRole] = useState<string | null>(null);
    // ... other local state from the original Users JSX

    // ... rest of the tab body, lifted from /tmp/admin-page-original.tsx Users section

    return (
      <section>
        <h1 className="text-2xl font-bold mb-4">Users</h1>
        {/* JSX from original Users section, with these substitutions:
              - useState filter/page/role  →  read from searchParams, write via setQueryParam
              - keep all other JSX, handlers, mutations as-is */}
      </section>
    );
  }
  ```

  Implementer note: this is the heaviest task in the plan. Move JSX verbatim where possible. Convert any `useState`-based filter/pagination to URL-search-param-driven (`?search=`, `?page=`, `?role=`); leave modal / form state as `useState`.

- [ ] **Step 4:** Create `apps/frontend/src/app/dashboard/admin/(tabs)/users/page.tsx`:

  ```tsx
  'use client';
  import { UsersTab } from '@/components/features/admin/UsersTab';

  export default function Page() {
    return <UsersTab />;
  }
  ```

- [ ] **Step 5:** Verify build: `cd frontend && npm run build`. Now it should succeed (the redirect from `admin/page.tsx` lands on `/dashboard/admin/users` which renders `<UsersTab />`).

- [ ] **Step 6:** Manual smoke: `npm run dev`, log in as admin user on a local dev gateway, navigate to `/dashboard/admin`, confirm redirect to `/users`, confirm Users content renders, confirm the URL search params (`?search=...`, `?page=2`) survive a refresh.

- [ ] **Step 7:** Commit:
  ```bash
  git add apps/frontend/src/components/features/admin/UsersTab.tsx \
          apps/frontend/src/app/dashboard/admin/\(tabs\)/users/page.tsx \
          apps/frontend/src/app/dashboard/admin/page.tsx
  git commit -m "refactor(admin): extract Users tab into UsersTab + (tabs)/users route"
  ```

---

### Task 7: Extract Audit tab

**Files:**
- Create: `apps/frontend/src/components/features/admin/AuditTab.tsx`
- Create: `apps/frontend/src/app/dashboard/admin/(tabs)/audit/page.tsx`

- [ ] **Step 1:** From the original file, identify the Audit-tab JSX (read-only browsing of audit log entries with date filter / user-id filter / pagination).

- [ ] **Step 2:** Create `AuditTab.tsx` mirroring the Users-tab pattern:
  - URL search params: `?from=YYYY-MM-DD`, `?to=YYYY-MM-DD`, `?user_id=...`, `?page=...`.
  - Local state: any expanded-row UI, modal toggles.

- [ ] **Step 3:** Create the route wrapper `(tabs)/audit/page.tsx`:

  ```tsx
  'use client';
  import { AuditTab } from '@/components/features/admin/AuditTab';

  export default function Page() {
    return <AuditTab />;
  }
  ```

- [ ] **Step 4:** Verify build + manual smoke (open `/dashboard/admin/audit`).

- [ ] **Step 5:** Commit:
  ```bash
  git add apps/frontend/src/components/features/admin/AuditTab.tsx \
          apps/frontend/src/app/dashboard/admin/\(tabs\)/audit/page.tsx
  git commit -m "refactor(admin): extract Audit tab into AuditTab + (tabs)/audit route"
  ```

---

### Task 8: Extract Broadcasts tab

**Files:**
- Create: `apps/frontend/src/components/features/admin/BroadcastsTab.tsx`
- Create: `apps/frontend/src/app/dashboard/admin/(tabs)/broadcasts/page.tsx`

- [ ] **Step 1:** From the original file, identify the Broadcasts-tab JSX (broadcast list + draft/edit form + preview modal + send confirmation).

- [ ] **Step 2:** Create `BroadcastsTab.tsx`:
  - Local `useState` for draft contents, preview modal, send confirmation (these are *workflow* states, not durable filter state — keep `useState`).
  - URL search params optional: `?broadcast_id=...` for deep-linking to a specific broadcast.

- [ ] **Step 3:** Create the route wrapper `(tabs)/broadcasts/page.tsx`.

- [ ] **Step 4:** Verify build + manual smoke.

- [ ] **Step 5:** Commit:
  ```bash
  git add apps/frontend/src/components/features/admin/BroadcastsTab.tsx \
          apps/frontend/src/app/dashboard/admin/\(tabs\)/broadcasts/page.tsx
  git commit -m "refactor(admin): extract Broadcasts tab into BroadcastsTab + (tabs)/broadcasts route"
  ```

---

### Task 9: Wrap already-extracted tabs in routes

**Files:** Create three 3-line route wrappers.

- [ ] **Step 1:** Create `apps/frontend/src/app/dashboard/admin/(tabs)/provider-performance/page.tsx`:

  ```tsx
  'use client';
  import ProviderPerformanceTab from '@/components/features/admin/ProviderPerformanceTab';

  export default function Page() {
    return <ProviderPerformanceTab />;
  }
  ```

  (Verify the existing tab uses default export vs named export — adjust the import accordingly.)

- [ ] **Step 2:** Create `apps/frontend/src/app/dashboard/admin/(tabs)/analytics/page.tsx`:

  ```tsx
  'use client';
  import AnalyticsTab from '@/components/features/admin/AnalyticsTab';

  export default function Page() {
    return <AnalyticsTab />;
  }
  ```

- [ ] **Step 3:** Create `apps/frontend/src/app/dashboard/admin/(tabs)/settings/page.tsx`:

  ```tsx
  'use client';
  import SettingsTab from '@/components/features/admin/SettingsTab';

  export default function Page() {
    return <SettingsTab />;
  }
  ```

- [ ] **Step 4:** Verify build + manual smoke for all three tabs.

- [ ] **Step 5:** Commit:
  ```bash
  git add apps/frontend/src/app/dashboard/admin/\(tabs\)/provider-performance/page.tsx \
          apps/frontend/src/app/dashboard/admin/\(tabs\)/analytics/page.tsx \
          apps/frontend/src/app/dashboard/admin/\(tabs\)/settings/page.tsx
  git commit -m "refactor(admin): wrap existing extracted tabs in (tabs)/* routes"
  ```

---

### Task 10: Smoke route tests

**Files:** Create one test file per tab in the existing test directory (e.g., `apps/frontend/__tests__/admin/<tab>.test.tsx`). If frontend has no test runner, install Vitest first (see Task 2 Step 1).

- [ ] **Step 1:** Set up Vitest if not already configured. Add to `apps/frontend/package.json`:
  ```json
  {
    "scripts": {
      "test": "vitest",
      "test:ci": "vitest run"
    }
  }
  ```
  Add a minimal `vitest.config.ts` at the frontend root using `@vitejs/plugin-react` and `jsdom` env.

- [ ] **Step 2:** Write `apps/frontend/__tests__/admin/users.test.tsx`:

  ```tsx
  import { render, screen } from '@testing-library/react';
  import { describe, it, expect, vi } from 'vitest';

  // Mock next/navigation since the tab uses useSearchParams()
  vi.mock('next/navigation', () => ({
    useSearchParams: () => new URLSearchParams(),
    useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
    usePathname: () => '/dashboard/admin/users',
  }));

  // Mock the API hooks the tab uses; adapt to actual import paths.
  vi.mock('@/lib/api/admin', () => ({
    useUsers: () => ({ data: [], isLoading: false, error: null }),
    // ... other hooks the tab calls
  }));

  import { UsersTab } from '@/components/features/admin/UsersTab';

  describe('UsersTab', () => {
    it('renders the Users heading', () => {
      render(<UsersTab />);
      expect(screen.getByText('Users')).toBeInTheDocument();
    });
  });
  ```

- [ ] **Step 3:** Repeat for Audit, Broadcasts, Provider Performance, Analytics, Settings — each test asserts the tab's anchor heading text is in the DOM.

- [ ] **Step 4:** Run: `cd frontend && npm run test:ci`. Expected: 6 passed.

- [ ] **Step 5:** Commit:
  ```bash
  git add apps/frontend/__tests__/admin/ apps/frontend/package.json apps/frontend/vitest.config.ts
  git commit -m "test(admin): smoke route tests for the six tabs"
  ```

---

### Task 11: Verify monolith is gone + final check

- [ ] **Step 1:** Confirm `admin/page.tsx` is the 4-line redirect: `wc -l apps/frontend/src/app/dashboard/admin/page.tsx`. Expected: ~4-7 lines.

- [ ] **Step 2:** Verify no orphaned imports: `grep -rn "from '@/app/dashboard/admin/page'" apps/frontend/`. Expected: nothing references the old monolith.

- [ ] **Step 3:** Verify build + tests + lint:
  ```bash
  cd frontend && npm run build && npm run lint && npm run test:ci
  ```

- [ ] **Step 4:** Manual QA checklist (paste into PR description):
  - [ ] Log in as admin → land on `/dashboard/admin/users` (redirected from `/dashboard/admin`).
  - [ ] Click each tab in `<AdminTabNav>`; URL updates and content renders.
  - [ ] On Users tab: enter search, refresh page → search persists.
  - [ ] On Users tab: open user detail modal, edit role, save; row reflects change.
  - [ ] On Audit tab: open one row's expanded view.
  - [ ] On Broadcasts tab: draft a broadcast, preview, send confirm modal appears.
  - [ ] On Provider Performance tab: charts render.
  - [ ] On Analytics tab: charts render.
  - [ ] On Settings tab: change a setting, save.
  - [ ] Log in as non-admin → hitting `/dashboard/admin/users` redirects to `/dashboard`.

---

### Task 12: PR finalization

- [ ] **Step 1:** Push: `git push -u origin jason/claude/admin-page-decomposition`.

- [ ] **Step 2:** Open PR:
  ```bash
  gh pr create --base dev --title "Decompose admin page into per-tab Next.js sub-routes" \
    --body "$(cat <<'EOF'
  ## Summary

  Splits the 3343-line \`apps/frontend/src/app/dashboard/admin/page.tsx\` into six per-tab Next.js sub-routes under a shared \`AdminLayout\`.

  - URL determines active tab (\`/dashboard/admin/users\`, \`/audit\`, etc.) — deep-linkable, refresh-safe.
  - Per-route code-splitting via Next.js App Router.
  - \`admin/page.tsx\` is now a 4-line server-side redirect to the first tab.
  - 32 \`useState\` calls now distributed across 6 tab components.
  - \`AuthProvider\`, API client, existing extracted tabs untouched.

  Spec: [docs/agents/specs/2026-05-03-admin-page-decomposition-design.md](docs/agents/specs/2026-05-03-admin-page-decomposition-design.md)

  ## Test plan
  - [x] \`npm run build\` succeeds
  - [x] \`npm run lint\` clean
  - [x] 6 smoke route tests pass
  - [x] Manual QA checklist (see body of plan, Task 11 Step 4) — completed on local dev
  - [ ] Manual QA on staging post-merge

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

- [ ] **Step 3:** Monitor CI + comments every 2 min until merged.

- [ ] **Step 4:** After merge, cleanup:
  ```bash
  cd /home/juncheng/hybridInference
  git worktree remove /home/juncheng/hybridInference-worktrees/admin-page-decomposition
  git branch -D jason/claude/admin-page-decomposition
  ```

---

## Self-Review Checklist (post-implementation)

- [ ] `apps/frontend/src/app/dashboard/admin/page.tsx` ≤ 10 lines (server-component redirect).
- [ ] `apps/frontend/src/app/dashboard/admin/layout.tsx` exists with role gate + `<AdminTabNav />`.
- [ ] All six route files exist under `(tabs)/`.
- [ ] All six tab component files exist under `apps/frontend/src/components/features/admin/`.
- [ ] `grep -n "useState" apps/frontend/src/app/dashboard/admin/page.tsx` returns nothing (the monolith's `useState` calls moved to per-tab components).
- [ ] `apps/frontend/src/components/providers/AuthProvider.tsx` is unchanged.
- [ ] `apps/frontend/src/lib/api/admin.ts` is unchanged.
- [ ] Six smoke route tests pass.
- [ ] `npm run build` and `npm run lint` clean.
- [ ] Manual QA checklist completed on local dev.
- [ ] PR description includes the manual QA checklist for staging.
