'use client';

import { createContext, useContext, useMemo } from 'react';
import toast from 'react-hot-toast';

/** Invalidated synchronously when the account leaves, before any awaited refresh. */
export interface AccountToastScope {
  userId: string | null;
  active: boolean;
}

export const AccountToastContext = createContext<AccountToastScope | null>(null);

/**
 * An async handler retains the scope of the render that started it. A later
 * account gets a new scope, never a reactivated old one (including A -> B -> A).
 * Auth transition confirmations deliberately use react-hot-toast directly.
 */
export function useAccountToast() {
  const scope = useContext(AccountToastContext);
  return useMemo(
    () => ({
      success: (...args: Parameters<typeof toast.success>) => {
        if (!scope || scope.active) toast.success(...args);
      },
      error: (...args: Parameters<typeof toast.error>) => {
        if (!scope || scope.active) toast.error(...args);
      },
    }),
    [scope],
  );
}
