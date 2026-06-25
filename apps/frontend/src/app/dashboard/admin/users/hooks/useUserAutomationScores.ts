'use client';

import { useQuery } from '@tanstack/react-query';
import { getBulkAutomationScores } from '@/lib/api/admin';
import type { UserAutomationScore } from '@/lib/api/admin';

const FIVE_MIN = 5 * 60 * 1000;

// Backend bulk endpoint caps at 200 user_ids per request.
const CHUNK_SIZE = 200;

function chunkArray<T>(arr: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let i = 0; i < arr.length; i += size) {
    chunks.push(arr.slice(i, i + size));
  }
  return chunks;
}

/**
 * Bulk automation scores for the current page of users. Gated behind `enabled`
 * so the (comparatively expensive) scoring only runs when the admin clicks the
 * "Score automation" button, not on every page load.
 */
export function useBulkAutomationScores(userIds: string[], days = 30, enabled = false) {
  // Stable cache key: sort ids so order doesn't matter.
  const key = [...userIds].sort().join(',');
  return useQuery<Record<string, UserAutomationScore>>({
    queryKey: ['admin', 'users', 'automation-scores', 'bulk', days, key],
    queryFn: async () => {
      if (userIds.length === 0) return {};
      const chunks = chunkArray(userIds, CHUNK_SIZE);
      const results = await Promise.all(
        chunks.map((chunk) => getBulkAutomationScores(chunk, days)),
      );
      return Object.assign({}, ...results.map((r) => r.scores));
    },
    enabled: enabled && userIds.length > 0,
    staleTime: FIVE_MIN,
  });
}
