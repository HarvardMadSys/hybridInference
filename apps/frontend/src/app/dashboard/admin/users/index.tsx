'use client';

import { useCallback, useEffect, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import toast from 'react-hot-toast';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  approveUser,
  deleteUser,
  hardDeleteUser,
  rejectUser,
  resumeUser,
  updateUser,
} from '@/lib/api/admin';
import { SummaryCards } from './SummaryCards';
import { SavedViews } from './SavedViews';
import { FilterBar } from './FilterBar';
import { UserTable } from './UserTable';
import { useUsers } from './hooks/useUsers';
import { useBulkCostHistory } from './hooks/useUserCostHistory';
import { filterStateFromUrl, filterStateToUrl } from './lib/filterTypes';
import { getViewById } from './lib/views';
import type { Density, FilterState, UserRow } from './types';
import type { SummaryCardId } from './SummaryCards';

const DENSITY_KEY = 'admin.users.density';

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

  // In Next.js App Router, useSearchParams() is reactive: it returns a new
  // object on every URL change (including browser back/forward). Sync
  // filterState whenever searchParams changes so the table stays in sync with
  // the URL even when the user navigates history without going through
  // applyFilterState.
  useEffect(() => {
    setFilterState(filterStateFromUrl(searchParams));
  }, [searchParams]);

  const applyFilterState = useCallback(
    (next: FilterState) => {
      setFilterState(next);
      const qs = filterStateToUrl(next);
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [pathname, router],
  );

  // Persisted density
  useEffect(() => {
    const saved = localStorage.getItem(DENSITY_KEY) as Density | null;
    if (saved === 'compact' || saved === 'comfortable') setDensity(saved);
  }, []);
  useEffect(() => {
    localStorage.setItem(DENSITY_KEY, density);
  }, [density]);

  // Data
  const usersQuery = useUsers(filterState);

  // Surface query errors via toast (non-fatal — table also shows inline error)
  useEffect(() => {
    if (usersQuery.error) {
      toast.error(getErrorMessage(usersQuery.error));
    }
  }, [usersQuery.error]);

  const users = usersQuery.data?.users ?? [];

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
        <UserTable
          users={userRows}
          costHistories={costHistories}
          density={density}
          filterState={filterState}
          onSortChange={(sortBy) => applyFilterState({ ...filterState, sortBy })}
          {...handlers}
        />
      )}
    </div>
  );
}
