import { useQuery } from '@tanstack/react-query';
import { getModels } from '@/lib/api/user';

export function useModels() {
  return useQuery({
    queryKey: ['user', 'models'],
    queryFn: getModels,
    // Scheduled prices have minute precision. Refresh visible quotes promptly
    // when a pricing window changes, including after returning to this tab.
    staleTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: 60 * 1000,
  });
}
