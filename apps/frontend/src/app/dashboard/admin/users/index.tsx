'use client';

import { useCallback, useEffect, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import toast from 'react-hot-toast';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  approveUser,
  deleteUser,
  hardDeleteUser,
  regenerateApiKeyAdmin,
  rejectUser,
  resumeUser,
  updateUser,
} from '@/lib/api/admin';
import { SummaryCards } from './SummaryCards';
import { SavedViews } from './SavedViews';
import { FilterBar } from './FilterBar';
import { UserTable } from './UserTable';
import { Pagination, PAGE_SIZE_OPTIONS } from './Pagination';
import { useUsers } from './hooks/useUsers';
import { useBulkCostHistory } from './hooks/useUserCostHistory';
import { filterStateFromUrl, filterStateToUrl } from './lib/filterTypes';
import { getViewById } from './lib/views';
import type { Density, FilterState, UserRow } from './types';
import type { SummaryCardId } from './SummaryCards';

const DENSITY_KEY = 'admin.users.density';
const PAGE_SIZE_KEY = 'admin.users.pageSize';
const DEFAULT_PAGE_SIZE = PAGE_SIZE_OPTIONS[1]; // 100

export default function UsersTab() {
  // Filter state — initialized from URL on mount so reload + shared links
  // preserve filters. `applyFilterState` (below) keeps state and URL in sync.
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [filterState, setFilterState] = useState<FilterState>(() =>
    filterStateFromUrl(searchParams),
  );
  const [density, setDensity] = useState<Density>('comfortable');

  // Pagination state. `page` is zero-based; `pageSize` is persisted across
  // sessions like `density`. Filter changes reset back to the first page.
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);

  // In Next.js App Router, useSearchParams() is reactive: it returns a new
  // object on every URL change (including browser back/forward). Sync
  // filterState whenever searchParams changes so the table stays in sync with
  // the URL even when the user navigates history without going through
  // applyFilterState.
  useEffect(() => {
    setFilterState(filterStateFromUrl(searchParams));
    // Filters changed (incl. browser back/forward) — return to the first page
    // so the user isn't stranded on an out-of-range offset.
    setPage(0);
  }, [searchParams]);

  const applyFilterState = useCallback(
    (next: FilterState) => {
      setFilterState(next);
      setPage(0);
      const qs = filterStateToUrl(next);
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [pathname, router],
  );

  // New API key banner (shown after a regenerate)
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // Persisted density
  useEffect(() => {
    const saved = localStorage.getItem(DENSITY_KEY) as Density | null;
    if (saved === 'compact' || saved === 'comfortable') setDensity(saved);
  }, []);
  useEffect(() => {
    localStorage.setItem(DENSITY_KEY, density);
  }, [density]);

  // Persisted page size
  useEffect(() => {
    const saved = Number(localStorage.getItem(PAGE_SIZE_KEY));
    if (PAGE_SIZE_OPTIONS.includes(saved as (typeof PAGE_SIZE_OPTIONS)[number])) {
      setPageSize(saved);
    }
  }, []);
  useEffect(() => {
    localStorage.setItem(PAGE_SIZE_KEY, String(pageSize));
  }, [pageSize]);

  // Data
  const usersQuery = useUsers(filterState, pageSize, page * pageSize);

  // Surface query errors via toast (non-fatal — table also shows inline error)
  useEffect(() => {
    if (usersQuery.error) {
      toast.error(getErrorMessage(usersQuery.error));
    }
  }, [usersQuery.error]);

  const users = usersQuery.data?.users ?? [];
  const total = usersQuery.data?.total ?? 0;

  const onPageSizeChange = (size: number) => {
    setPageSize(size);
    setPage(0);
  };

  // Map AdminUser[] (from listUsers) → UserRow[] expected by the table.
  // AdminUser.role is `string`, AdminUser.usage_*_usd are `number`; UserRow
  // expects a typed role/status union and string-serialised decimals.
  const userRows: UserRow[] = users.map((u) => ({
    ...u,
    role: (u.role as UserRow['role']) ?? 'free',
    status: (u.status as UserRow['status']) ?? 'active',
    usage_today_usd: String(u.usage_today_usd),
    usage_month_usd: String(u.usage_month_usd),
    usage_alltime_usd: String(u.usage_alltime_usd),
  }));

  const userIds = users.map((u) => u.id);
  const histQuery = useBulkCostHistory(userIds, 7, density === 'comfortable');
  const costHistories = histQuery.data ?? {};

  // Card click → apply built-in view filter
  const onCardClick = (cardId: SummaryCardId) => {
    const view = getViewById(cardId);
    if (view) applyFilterState(view.filterState);
  };

  // Action handlers — wrap admin API fns and refetch on success.
  // These are passed down to UserTable, which owns its own delete/hard-delete
  // modals and detail-edit interactions.
  const handlers = {
    onApprove: async (id: string) => {
      try {
        const user = users.find((u) => u.id === id);
        await approveUser(id);
        toast.success(`Approved ${user?.email ?? id}`);
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
    onReject: async (id: string, reason: string) => {
      try {
        const user = users.find((u) => u.id === id);
        await rejectUser(id, reason);
        toast.success(`Rejected ${user?.email ?? id}`);
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
    onUpdate: async (id: string, patch: Record<string, unknown>) => {
      try {
        await updateUser(id, patch);
        toast.success('Saved');
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
    onSuspend: async (id: string) => {
      try {
        await updateUser(id, { status: 'suspended' });
        toast.success('Suspended');
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
    onResume: async (id: string) => {
      try {
        const user = users.find((u) => u.id === id);
        await resumeUser(id);
        toast.success(`Resumed ${user?.email ?? id}`);
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
    onDelete: async (id: string, reason: string) => {
      try {
        const user = users.find((u) => u.id === id);
        await deleteUser(id, reason);
        toast.success(`Deleted ${user?.email ?? id}`);
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
    onHardDelete: async (id: string, reason: string) => {
      try {
        const user = users.find((u) => u.id === id);
        await hardDeleteUser(id, reason || undefined);
        toast.success(`Permanently deleted ${user?.email ?? id}`);
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
    onRegenerateKey: async (id: string) => {
      try {
        const r = await regenerateApiKeyAdmin(id);
        setNewKey(r.api_key);
        usersQuery.refetch();
      } catch (e) {
        toast.error(getErrorMessage(e));
      }
    },
  };

  return (
    <div className="mt-6 space-y-4">
      <SummaryCards onCardClick={onCardClick} />
      <SavedViews current={filterState} onApply={applyFilterState} />
      <FilterBar
        state={filterState}
        onChange={applyFilterState}
        density={density}
        onDensityChange={setDensity}
      />

      {/* New API key banner (after regenerate) */}
      {newKey && (
        <div className="rounded-lg border border-gray-200 bg-white p-4">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-gray-900">New API key generated</span>
            <button
              type="button"
              onClick={() => setNewKey(null)}
              className="text-gray-300 hover:text-gray-500"
            >
              &times;
            </button>
          </div>
          <p className="mt-1 text-[12px] text-gray-400">
            Copy it now. It won&apos;t be shown again.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <code className="flex-1 select-all break-all rounded-md border border-gray-100 bg-gray-50 px-3 py-2 font-mono text-[13px] text-gray-900">
              {newKey}
            </code>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(newKey);
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
              }}
              className="shrink-0 rounded-md bg-gray-900 px-3 py-2 text-[12px] font-semibold text-white transition hover:bg-gray-800"
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
        </div>
      )}

      {usersQuery.isLoading && (
        <div className="flex justify-center py-12">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        </div>
      )}
      {usersQuery.error && (
        <div className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          Failed to load users.
          <button type="button" onClick={() => usersQuery.refetch()} className="ml-2 underline">
            Retry
          </button>
        </div>
      )}
      {!usersQuery.isLoading && (
        <>
          <UserTable
            users={userRows}
            costHistories={costHistories}
            density={density}
            filterState={filterState}
            onSortChange={(sortBy) => applyFilterState({ ...filterState, sortBy })}
            {...handlers}
          />
          <Pagination
            page={page}
            pageSize={pageSize}
            total={total}
            count={users.length}
            onPageChange={setPage}
            onPageSizeChange={onPageSizeChange}
          />
        </>
      )}
    </div>
  );
}
