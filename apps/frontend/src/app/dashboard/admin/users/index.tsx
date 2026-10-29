'use client';

import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel } from '@/lib/utils/userLabel';
import {
  approveUser,
  deleteUser,
  hardDeleteUser,
  rejectUser,
  resumeUser,
  updateUser,
  type UpdateUserData,
} from '@/lib/api/admin';
import { SummaryCards } from './SummaryCards';
import { SavedViews } from './SavedViews';
import { FilterBar } from './FilterBar';
import { UserTable } from './UserTable';
import { Pagination, PAGE_SIZE_OPTIONS } from './Pagination';
import { useUsers, USERS_LIST_QUERY_KEY } from './hooks/useUsers';
import { useBulkUsageTotals, USAGE_TOTALS_QUERY_KEY } from './hooks/useUserUsageTotals';
import { useBulkCostHistory } from './hooks/useUserCostHistory';
import { useBulkTurnAverages } from './hooks/useUserTurnAverages';
import { useBulkAskQuestionFractions } from './hooks/useUserAskQuestionFractions';
import { useBulkAutomationScores } from './hooks/useUserAutomationScores';
import { compareByScore } from './lib/automation';
import { filterStateFromUrl, filterStateToUrl } from './lib/filterTypes';
import { getViewById } from './lib/views';
import { parseHiddenColumns, type ColumnId } from './lib/columns';
import type { Density, FilterState, UserRow } from './types';
import type { SummaryCardId } from './SummaryCards';

const DENSITY_KEY = 'admin.users.density';
const PAGE_SIZE_KEY = 'admin.users.pageSize';
const HIDDEN_COLUMNS_KEY = 'admin.users.hiddenColumns';
const DEFAULT_PAGE_SIZE = PAGE_SIZE_OPTIONS[1]; // 100

export default function UsersTab() {
  const toast = useAccountToast();
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

  // Persisted column visibility (the "Columns" menu)
  const [hiddenColumns, setHiddenColumns] = useState<ColumnId[]>([]);
  const [hiddenColumnsLoaded, setHiddenColumnsLoaded] = useState(false);
  useEffect(() => {
    setHiddenColumns(parseHiddenColumns(localStorage.getItem(HIDDEN_COLUMNS_KEY)));
    setHiddenColumnsLoaded(true);
  }, []);
  useEffect(() => {
    // Skip the first render so the empty default doesn't overwrite the saved list.
    if (hiddenColumnsLoaded)
      localStorage.setItem(HIDDEN_COLUMNS_KEY, JSON.stringify(hiddenColumns));
  }, [hiddenColumns, hiddenColumnsLoaded]);

  // Data
  const queryClient = useQueryClient();
  const usersQuery = useUsers(filterState, pageSize, page * pageSize);

  // Refresh after a mutation. Each visited page/filter is its own cache entry,
  // and a mutation can shift totals/ordering across all of them, so invalidate
  // the whole list family rather than refetching only the active offset (which
  // would leave other cached pages stale within the hook's staleTime window).
  const refreshUsers = useCallback(
    () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: USERS_LIST_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: USAGE_TOTALS_QUERY_KEY }),
      ]),
    [queryClient],
  );

  // Surface query errors via toast (non-fatal — table also shows inline error)
  useEffect(() => {
    if (usersQuery.error) {
      toast.error(getErrorMessage(usersQuery.error));
    }
  }, [toast, usersQuery.error]);

  const users = usersQuery.data?.users ?? [];
  const total = usersQuery.data?.total ?? 0;
  const usageTotalsQuery = useBulkUsageTotals(
    users
      .filter((u) => u.usage_alltime_requests == null || u.usage_alltime_tokens == null)
      .map((u) => u.id),
  );

  // Clamp the page when the total shrinks beneath the current offset without a
  // filter change — e.g. an admin deletes/approves the last users on a later
  // page, so a refetch of the same offset comes back empty. Only act on a
  // settled result for the current query (isSuccess && !isFetching); otherwise
  // the transient total=0 while a freshly-navigated page loads would bounce the
  // user straight back to page 0 and break forward navigation.
  useEffect(() => {
    if (!usersQuery.isSuccess || usersQuery.isFetching) return;
    const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
    if (page > lastPage) setPage(lastPage);
  }, [usersQuery.isSuccess, usersQuery.isFetching, total, page, pageSize]);

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
    usage_alltime_requests:
      u.usage_alltime_requests ?? usageTotalsQuery.data?.[u.id]?.requests ?? null,
    usage_alltime_tokens: u.usage_alltime_tokens ?? usageTotalsQuery.data?.[u.id]?.tokens ?? null,
  }));

  const userIds = users.map((u) => u.id);
  const histQuery = useBulkCostHistory(userIds, 7, density === 'comfortable');
  const costHistories = histQuery.data ?? {};
  const turnQuery = useBulkTurnAverages(userIds);
  const turnAverages = turnQuery.data ?? {};
  const askQuestionQuery = useBulkAskQuestionFractions(userIds);
  const askQuestionFractions = askQuestionQuery.data ?? {};

  // Automation score (human vs. script) is computed on demand: the admin clicks
  // the "Automation" column header to run it for the current page, then can
  // click again to sort the page by score (client-side, since it is a computed
  // metric the server doesn't sort by).
  const [scoreRun, setScoreRun] = useState(false);
  const [scoreSortDir, setScoreSortDir] = useState<'desc' | 'asc' | null>(null);
  const scoreQuery = useBulkAutomationScores(userIds, 30, scoreRun);
  const automationScores = scoreQuery.data ?? {};
  const scoreState: 'idle' | 'loading' | 'loaded' = scoreQuery.isFetching
    ? 'loading'
    : scoreRun
      ? 'loaded'
      : 'idle';

  useEffect(() => {
    if (scoreQuery.error) toast.error(getErrorMessage(scoreQuery.error));
  }, [scoreQuery.error, toast]);

  // Re-gate scoring whenever the visible set of users changes (pagination,
  // page-size, or filters). Without this, a single header click would leave
  // `scoreRun` true and auto-fire the comparatively expensive bulk scoring on
  // every subsequent page, defeating the on-demand gate.
  useEffect(() => {
    setScoreRun(false);
    setScoreSortDir(null);
  }, [page, pageSize, filterState]);

  const onScoreHeader = () => {
    if (!scoreRun) {
      setScoreRun(true);
      setScoreSortDir('desc');
      return;
    }
    setScoreSortDir((d) => (d === 'desc' ? 'asc' : d === 'asc' ? null : 'desc'));
  };

  const byScore = (a: UserRow, b: UserRow) =>
    compareByScore(automationScores[a.id], automationScores[b.id], scoreSortDir ?? 'desc');
  const displayRows = scoreSortDir ? [...userRows].sort(byScore) : userRows;

  // Card click → apply built-in view filter
  const onCardClick = (cardId: SummaryCardId) => {
    const view = getViewById(cardId);
    if (view) applyFilterState(view.filterState);
  };

  // Write handlers — wrap the admin API fns and refetch on success. A failure
  // is thrown, not toasted: UserTable shows it in the confirmation that asked
  // for the write or beside the save that made it, and toasts it otherwise.
  const labelOf = (id: string) => userAccountLabel(users.find((u) => u.id === id)) || id;
  const handlers = {
    onApprove: async (id: string) => {
      const label = labelOf(id);
      await approveUser(id);
      toast.success(`Approved ${label}`);
      void refreshUsers();
    },
    onReject: async (id: string, reason: string) => {
      const label = labelOf(id);
      await rejectUser(id, reason);
      toast.success(`Rejected ${label}`);
      void refreshUsers();
    },
    onUpdate: async (id: string, patch: UpdateUserData) => {
      await updateUser(id, patch);
      toast.success('Saved');
      void refreshUsers();
    },
    onSuspend: async (id: string, suspensionMessage?: string | null) => {
      // Always send suspension_message (null when blank) so re-suspending
      // clears any stale message from a prior suspension.
      await updateUser(id, {
        status: 'suspended',
        suspension_message: suspensionMessage ?? null,
      });
      toast.success('Suspended');
      void refreshUsers();
    },
    onResume: async (id: string, recoverStaleClaim = false) => {
      const label = labelOf(id);
      await resumeUser(id, undefined, recoverStaleClaim);
      toast.success(`Resumed ${label}`);
      void refreshUsers();
    },
    onDelete: async (id: string, reason: string) => {
      const label = labelOf(id);
      await deleteUser(id, reason);
      toast.success(`Deleted ${label}`);
      void refreshUsers();
    },
    onHardDelete: async (id: string, reason: string, recoverStaleClaim = false) => {
      const label = labelOf(id);
      await hardDeleteUser(id, reason || undefined, recoverStaleClaim);
      toast.success(`Permanently deleted ${label}`);
      void refreshUsers();
    },
  };

  return (
    <div className="mt-6 space-y-4">
      <SummaryCards onCardClick={onCardClick} activeView={filterState.view} />

      {/* Keep toolbar popovers visible even when the table has few or no rows.
          UserTable owns the horizontal scrolling; only its content is clipped. */}
      <div className="rounded-lg border border-gray-200 bg-white">
        <div className="space-y-3 rounded-t-lg border-b border-gray-200 bg-gray-50/50 p-3">
          <SavedViews current={filterState} onApply={applyFilterState} />
          <FilterBar
            state={filterState}
            onChange={applyFilterState}
            density={density}
            onDensityChange={setDensity}
            hiddenColumns={hiddenColumns}
            onHiddenColumnsChange={setHiddenColumns}
          />
        </div>

        {usersQuery.isLoading && (
          <div className="flex justify-center py-16">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          </div>
        )}
        {usersQuery.error && (
          <div className="m-3 rounded-md border border-red-200 bg-red-50 p-3 text-[13px] text-red-700">
            Failed to load users.
            <button type="button" onClick={() => usersQuery.refetch()} className="ml-2 underline">
              Retry
            </button>
          </div>
        )}
        {usageTotalsQuery.isError && (
          <div role="status" className="m-3 text-[13px] text-gray-500">
            Historical request and token totals could not be loaded.
            <button
              type="button"
              onClick={() => usageTotalsQuery.refetch()}
              className="ml-2 underline"
            >
              Retry statistics
            </button>
          </div>
        )}
        {!usersQuery.isLoading && (
          <>
            <UserTable
              users={displayRows}
              costHistories={costHistories}
              turnAverages={turnAverages}
              askQuestionFractions={askQuestionFractions}
              automationScores={automationScores}
              scoreState={scoreState}
              scoreSortDir={scoreSortDir}
              onScoreHeader={onScoreHeader}
              density={density}
              hiddenColumns={hiddenColumns}
              filterState={filterState}
              onSortChange={(sortBy) => applyFilterState({ ...filterState, sortBy })}
              {...handlers}
            />
            <div className="border-t border-gray-200 px-3">
              <Pagination
                page={page}
                pageSize={pageSize}
                total={total}
                count={users.length}
                onPageChange={setPage}
                onPageSizeChange={onPageSizeChange}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
