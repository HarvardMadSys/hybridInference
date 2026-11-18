'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { TwoFactorPrompt } from '@/components/features/twoFactor/TwoFactorPrompt';
import { getTwoFactorStatus, type TwoFactorStatus } from '@/lib/api/twoFactor';
import { useAuth } from './AuthProvider';

const QUERY_KEY = 'twoFactor';

interface TwoFactorState {
  /**
   * The signed-in account's second factor and what this session has proved.
   * `undefined` until read, and when it cannot be read: a gateway older than
   * this console has no such route, and everything then behaves as before.
   */
  status: TwoFactorStatus | undefined;
  /** The status is on its way; a gate should hold what it guards until then. */
  isPending: boolean;
  /** Read the status again, after anything that changed it. */
  refresh: () => Promise<void>;
  /** Ask for the status to be read; `useTwoFactor` does. */
  need: () => void;
}

// Outside the provider nothing is known and nothing is asked: a component
// rendered on its own behaves as it does for an account without a second factor.
const TwoFactorContext = createContext<TwoFactorState>({
  status: undefined,
  isPending: false,
  refresh: async () => {},
  need: () => {},
});

/**
 * The account's second factor, for the pages that care, and the dialog that
 * asks for a code when the gateway refuses a request for want of one.
 *
 * The status is read only once a page asks (`useTwoFactor`): the settings
 * page and administration do, the rest of the console never does.
 */
export function TwoFactorProvider({ children }: { children: React.ReactNode }) {
  const { state } = useAuth();
  const queryClient = useQueryClient();
  const [needed, setNeeded] = useState(false);
  const userId = state.isAuthenticated ? (state.user?.id ?? null) : null;

  const query = useQuery({
    queryKey: [QUERY_KEY, userId],
    queryFn: getTwoFactorStatus,
    enabled: needed && userId !== null,
    staleTime: 60 * 1000,
    // Asking an older gateway again would only delay the page that waits.
    retry: false,
  });

  const need = useCallback(() => setNeeded(true), []);
  const refresh = useCallback(
    () => queryClient.invalidateQueries({ queryKey: [QUERY_KEY] }),
    [queryClient],
  );
  const status = userId !== null ? query.data : undefined;
  const isPending = userId !== null && (!needed || query.isPending);
  const value = useMemo(
    () => ({ status, isPending, refresh, need }),
    [status, isPending, refresh, need],
  );

  return (
    <TwoFactorContext.Provider value={value}>
      {children}
      {/* After the pages, so its dialog opens over one of theirs. */}
      <TwoFactorPrompt />
    </TwoFactorContext.Provider>
  );
}

/** The account's second factor; reading it here is what has it fetched. */
export function useTwoFactor(): Pick<TwoFactorState, 'status' | 'isPending' | 'refresh'> {
  const context = useContext(TwoFactorContext);
  const { need } = context;
  useEffect(() => need(), [need]);
  return context;
}

/** Only the way to have the status read again, for what changes it without showing it. */
export function useRefreshTwoFactor(): () => Promise<void> {
  return useContext(TwoFactorContext).refresh;
}
