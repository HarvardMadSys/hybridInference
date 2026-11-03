import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createApiKey,
  deleteApiKey,
  listApiKeys,
  regenerateApiKey,
  renameApiKey,
} from '@/lib/api/user';

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
    mutationFn: (name?: string | null) => createApiKey(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
  });
}

export function useRenameApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ keyPrefix, name }: { keyPrefix: string; name: string | null }) =>
      renameApiKey(keyPrefix, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
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
    mutationFn: (keyPrefix: string) => regenerateApiKey(keyPrefix),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
    },
  });
}
