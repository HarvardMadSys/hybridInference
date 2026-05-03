'use client';

import { useQuery } from '@tanstack/react-query';
import { getBulkCostHistory, getUserCostHistory } from '@/lib/api/admin';
import type { CostHistoryPoint } from '../types';

const FIVE_MIN = 5 * 60 * 1000;

export function useBulkCostHistory(userIds: string[], days = 7, enabled = true) {
  // Stable cache key: sort ids so order doesn't matter
  const key = [...userIds].sort().join(',');
  return useQuery<Record<string, CostHistoryPoint[]>>({
    queryKey: ['admin', 'users', 'cost-history', 'bulk', key, days],
    queryFn: async () => {
      if (userIds.length === 0) return {};
      const resp = await getBulkCostHistory(userIds, days);
      return resp.histories;
    },
    enabled: enabled && userIds.length > 0,
    staleTime: FIVE_MIN,
  });
}

export function useUserCostHistory(userId: string | null, days = 7) {
  return useQuery<CostHistoryPoint[]>({
    queryKey: ['admin', 'users', 'cost-history', userId, days],
    queryFn: async () => {
      if (!userId) return [];
      const resp = await getUserCostHistory(userId, days);
      return resp.points;
    },
    enabled: !!userId,
    staleTime: FIVE_MIN,
  });
}
