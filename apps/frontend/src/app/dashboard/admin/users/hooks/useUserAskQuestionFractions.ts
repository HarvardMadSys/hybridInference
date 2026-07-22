'use client';

import { useQuery } from '@tanstack/react-query';
import { getBulkAskQuestionFractions } from '@/lib/api/admin';
import type { UserAskQuestionFraction } from '@/lib/api/admin';

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

export function useBulkAskQuestionFractions(userIds: string[], enabled = true) {
  // Stable cache key: sort ids so order doesn't matter.
  const key = [...userIds].sort().join(',');
  return useQuery<Record<string, UserAskQuestionFraction>>({
    queryKey: ['admin', 'users', 'ask-question-fractions', 'bulk', key],
    queryFn: async () => {
      if (userIds.length === 0) return {};
      // Chunk into groups of ≤200 to stay within the backend limit, then merge.
      const chunks = chunkArray(userIds, CHUNK_SIZE);
      const results = await Promise.all(chunks.map((chunk) => getBulkAskQuestionFractions(chunk)));
      return Object.assign({}, ...results.map((r) => r.fractions));
    },
    enabled: enabled && userIds.length > 0,
    staleTime: FIVE_MIN,
  });
}
