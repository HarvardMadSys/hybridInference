'use client';

import { useQuery } from '@tanstack/react-query';
import { listUserTags } from '@/lib/api/admin';

/** Every user tag; invalidated after any tag or membership write. */
export const USER_TAGS_QUERY_KEY = ['admin', 'user-tags'] as const;

export function useUserTags(enabled = true) {
  return useQuery({
    queryKey: USER_TAGS_QUERY_KEY,
    queryFn: listUserTags,
    staleTime: 30 * 1000,
    enabled,
  });
}
