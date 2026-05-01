import { useQuery } from '@tanstack/react-query';
import { getModels } from '@/lib/api/user';

export function useModels() {
  return useQuery({
    queryKey: ['user', 'models'],
    queryFn: getModels,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 10 * 60 * 1000,
  });
}
