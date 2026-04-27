import { useQuery } from '@tanstack/react-query';
import { getRecentRequests } from '@/lib/api/user';

export function useRecentRequests(limit: number = 50, offset: number = 0, modelId?: string) {
  return useQuery({
    queryKey: ['user', 'recent-requests', limit, offset, modelId],
    queryFn: () => getRecentRequests(limit, offset, modelId),
    staleTime: 30 * 1000, // 30 seconds
    refetchInterval: 60 * 1000, // Auto-refresh every minute
  });
}
