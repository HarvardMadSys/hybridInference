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
        anomaly: state.anomaly ?? undefined,
      }),
    staleTime: THIRTY_S,
  });
}
