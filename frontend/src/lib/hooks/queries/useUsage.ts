import { useQuery } from '@tanstack/react-query';
import { getUsage } from '@/lib/api/user';

export function useUsageStats(period: 'today' | 'month' | 'all' = 'today') {
  return useQuery({
    queryKey: ['usage', 'stats', period],
    queryFn: () => getUsage(period),
    staleTime: 2 * 60 * 1000, // 2 minutes
    refetchInterval: 5 * 60 * 1000, // Auto-refresh every 5 minutes
  });
}
