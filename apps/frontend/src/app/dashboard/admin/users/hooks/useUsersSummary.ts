'use client';

import { useMoney } from '@/lib/hooks/useMoney';
import { useQuery } from '@tanstack/react-query';
import { getUsersSummary } from '@/lib/api/admin';

const THIRTY_S = 30 * 1000;

export function useUsersSummary() {
  const { currency } = useMoney();
  return useQuery({
    queryKey: ['admin', 'users', 'summary', currency],
    queryFn: getUsersSummary,
    staleTime: THIRTY_S,
  });
}
