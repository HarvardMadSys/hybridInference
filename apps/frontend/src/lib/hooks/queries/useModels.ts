import { useMoney } from '@/lib/hooks/useMoney';
import { useQuery } from '@tanstack/react-query';
import { getModels } from '@/lib/api/user';

export function useModels() {
  const { currency } = useMoney();
  return useQuery({
    queryKey: ['user', 'models', currency],
    queryFn: getModels,
    // Scheduled prices have minute precision. Refresh visible quotes promptly
    // when a pricing window changes, including after returning to this tab.
    staleTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: 60 * 1000,
  });
}
