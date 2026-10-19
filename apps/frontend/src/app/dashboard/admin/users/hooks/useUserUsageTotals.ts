'use client';

import { useQuery } from '@tanstack/react-query';
import { getBulkUsageTotals, type UserUsageTotals } from '@/lib/api/admin';

export const USAGE_TOTALS_QUERY_KEY = ['admin', 'users', 'usage-totals'] as const;

export function useBulkUsageTotals(userIds: string[]) {
  const ids = [...userIds].sort();
  return useQuery({
    queryKey: [...USAGE_TOTALS_QUERY_KEY, ids.join(',')],
    queryFn: async ({ signal }) => {
      const totals: Record<string, UserUsageTotals> = {};
      // Bound each query and avoid concurrent lifetime scans on large pages.
      for (let i = 0; i < ids.length; i += 200) {
        Object.assign(totals, (await getBulkUsageTotals(ids.slice(i, i + 200), signal)).totals);
      }
      return totals;
    },
    enabled: ids.length > 0,
    staleTime: 30_000,
    // A slow statistics query must not create a retry storm or reload on focus.
    retry: false,
    refetchOnWindowFocus: false,
  });
}
