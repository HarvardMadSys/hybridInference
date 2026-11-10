import { useQuery } from '@tanstack/react-query';
import { getRecentRequests } from '@/lib/api/user';

export function useRecentRequests(
  limit: number = 50,
  offset: number = 0,
  modelId?: string,
  keyPrefix?: string,
) {
  return useQuery({
    queryKey: ['user', 'recent-requests', limit, offset, modelId, keyPrefix],
    queryFn: () => getRecentRequests(limit, offset, modelId, keyPrefix),
    staleTime: 30 * 1000, // 30 seconds
    refetchInterval: 60 * 1000, // Auto-refresh every minute
  });
}
