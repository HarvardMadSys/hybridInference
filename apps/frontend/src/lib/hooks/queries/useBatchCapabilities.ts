import { useQuery } from '@tanstack/react-query';

import { useAuth } from '@/components/providers';
import { getMyBatchCapabilities, type BatchCapabilities } from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';

export interface BatchAccess {
  /** The signed-in account's capabilities; null until one of its answers arrived. */
  capabilities: BatchCapabilities | null;
  /** No answer for this account yet, and none has failed. */
  loading: boolean;
  /** The latest lookup failed. The last answer, if any, is still in `capabilities`. */
  error: string | null;
}

/**
 * What the console offers the signed-in account for Batch.
 *
 * Cached per account, and an answer naming another account is ignored, so a
 * slow response to the previous account never decides this one's entry. A
 * failed refresh keeps the account's last answer, so a known entry does not
 * vanish on a transient error. Refreshed on focus and, while the tab is
 * visible, every minute: an administrator's change to `BATCH_ENABLED` or
 * `BATCH_MIN_ROLE`, or the account's role, shows without a reload.
 */
export function useBatchCapabilities(): BatchAccess {
  const { state } = useAuth();
  const userId = state.user?.id ?? null;
  const query = useQuery({
    queryKey: ['user', 'batch-capabilities', userId],
    queryFn: async () => {
      const result = await getMyBatchCapabilities();
      if (result.user_id !== userId) throw new Error('Batch access could not be verified.');
      return result;
    },
    enabled: userId !== null,
    staleTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: 60 * 1000,
    refetchIntervalInBackground: false,
  });
  const capabilities = query.data && query.data.user_id === userId ? query.data : null;
  return {
    capabilities,
    loading: userId !== null && capabilities === null && !query.isError,
    error: query.isError ? getErrorMessage(query.error) : null,
  };
}
