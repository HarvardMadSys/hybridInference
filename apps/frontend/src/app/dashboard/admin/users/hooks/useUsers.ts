'use client';

import { useMoney } from '@/lib/hooks/useMoney';
import { useQuery } from '@tanstack/react-query';
import { listUsers } from '@/lib/api/admin';
import type { FilterState } from '../types';

const THIRTY_S = 30 * 1000;

// Shared prefix for every paginated users-list cache entry. Invalidating this
// key (rather than refetching a single observer) refreshes all cached pages
// after a mutation, since each page/filter combination is its own entry.
export const USERS_LIST_QUERY_KEY = ['admin', 'users', 'list'] as const;

export function useUsers(state: FilterState, limit = 100, offset = 0) {
  const { currency } = useMoney();
  // Only a spend threshold is read in the ledger's currency, so only a request
  // with one names it: the gateway refuses that request when its ledger has
  // since moved to another currency, and nothing else in the list depends on it.
  const thresholds = state.minCostToday !== null || state.minCostMonth !== null;
  return useQuery({
    queryKey: [...USERS_LIST_QUERY_KEY, state, limit, offset, currency],
    queryFn: () =>
      listUsers({
        currency: thresholds ? currency : undefined,
        status: state.status ?? undefined,
        search: state.search || undefined,
        sortBy: state.sortBy,
        includeUsageTotals: false,
        limit,
        offset,
        minCostToday: state.minCostToday ?? undefined,
        minCostMonth: state.minCostMonth ?? undefined,
        quotaState: state.quotaState ?? undefined,
        provider: state.provider ?? undefined,
        activeWithinHours: state.activeWithinHours ?? undefined,
        anomaly: state.anomaly ?? undefined,
        organizationId: state.organization ?? undefined,
      }),
    staleTime: THIRTY_S,
  });
}
