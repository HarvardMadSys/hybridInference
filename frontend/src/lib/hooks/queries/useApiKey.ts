import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createApiKey, getApiKey, regenerateApiKey } from '@/lib/api/user';

export function useApiKey() {
  return useQuery({
    queryKey: ['apiKey'],
    queryFn: getApiKey,
    staleTime: 10 * 60 * 1000, // 10 minutes
  });
}

export function useCreateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: createApiKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKey'] });
    },
  });
}

export function useRegenerateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: regenerateApiKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKey'] });
    },
  });
}
