'use client';

import { useQuery } from '@tanstack/react-query';
import { getUsersSummary } from '@/lib/api/admin';

const THIRTY_S = 30 * 1000;

export function useUsersSummary() {
  return useQuery({
    queryKey: ['admin', 'users', 'summary'],
    queryFn: getUsersSummary,
    staleTime: THIRTY_S,
  });
}
