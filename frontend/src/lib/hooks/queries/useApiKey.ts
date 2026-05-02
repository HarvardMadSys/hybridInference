import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createApiKey, deleteApiKey, listApiKeys, regenerateApiKey } from '@/lib/api/user';

export function useApiKeys() {
  return useQuery({
    queryKey: ['apiKeys'],
    queryFn: listApiKeys,
    staleTime: 10 * 60 * 1000, // 10 minutes
  });
}

export function useCreateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: createApiKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
  });
}

export function useDeleteApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: deleteApiKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
  });
}

export function useRegenerateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: regenerateApiKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
  });
}
