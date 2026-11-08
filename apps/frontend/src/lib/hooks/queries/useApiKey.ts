import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createApiKey,
  deleteApiKey,
  listApiKeys,
  regenerateApiKey,
  renameApiKey,
  revealApiKey,
  updateApiKey,
  type ApiKeyUpdate,
} from '@/lib/api/user';

export function useApiKeys() {
  return useQuery({
    queryKey: ['apiKeys'],
    queryFn: listApiKeys,
    staleTime: 10 * 60 * 1000, // 10 minutes
  });
}

/** Show one saved key in full; the answer is dropped as soon as nothing observes it. */
export function useRevealApiKey() {
  return useMutation({
    mutationFn: ({ keyPrefix, password }: { keyPrefix: string; password: string }) =>
      revealApiKey(keyPrefix, password),
    gcTime: 0,
  });
}

export function useCreateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (args?: { name?: string | null; projectId?: string | null }) =>
      createApiKey(args?.name ?? null, args?.projectId ?? null),
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

export function useUpdateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ keyPrefix, changes }: { keyPrefix: string; changes: ApiKeyUpdate }) =>
      updateApiKey(keyPrefix, changes),
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
