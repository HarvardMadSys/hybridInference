'use client';

import { useQuery } from '@tanstack/react-query';
import { getBulkUsageTotals, type UserUsageTotals } from '@/lib/api/admin';

export const USAGE_TOTALS_QUERY_KEY = ['admin', 'users', 'usage-totals'] as const;

const FIVE_MIN = 5 * 60 * 1000;

// Backend bulk endpoint caps at 200 user_ids per request.
const CHUNK_SIZE = 200;

export function useBulkUsageTotals(userIds: string[]) {
  const ids = [...userIds].sort();
  return useQuery({
    queryKey: [...USAGE_TOTALS_QUERY_KEY, ids.join(',')],
    queryFn: async ({ signal }) => {
      const totals: Record<string, UserUsageTotals> = {};
      // Bound each query and avoid concurrent lifetime scans on large pages.
      for (let i = 0; i < ids.length; i += CHUNK_SIZE) {
        const chunk = ids.slice(i, i + CHUNK_SIZE);
        Object.assign(totals, (await getBulkUsageTotals(chunk, signal)).totals);
      }
      return totals;
    },
    enabled: ids.length > 0,
    // Lifetime totals move slowly and each fetch rescans the logs.
    staleTime: FIVE_MIN,
    // A slow statistics query must not create a retry storm or reload on focus.
    retry: false,
    refetchOnWindowFocus: false,
  });
}
