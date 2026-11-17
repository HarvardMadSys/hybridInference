'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from 'react';
import { usePathname } from 'next/navigation';
import { useSsoReauth, type SsoReauth } from '@/components/features/auth/SsoReauth';

/**
 * What a fresh sign-in at the administrator's identity provider confirms: one
 * guarded change of this session (the gateway's `ADMIN_REAUTH_PURPOSE`).
 */
export const ADMIN_REAUTH_PURPOSE = 'admin_action';

/** How long the gateway honours such a confirmation (`SSO_REAUTH_MAX_AGE_SECONDS`). */
export const ADMIN_REAUTH_MAX_AGE_MS = 5 * 60 * 1000;

const AdminSsoReauthContext = createContext<SsoReauth | null>(null);

/**
 * How the signed-in administrator confirms a guarded change through their
 * identity provider, or `null`: an account with a password or a verified phone
 * confirms the way it always has, and outside the Admin layout there is no
 * signed-in account to ask about.
 */
export function useAdminSsoReauth(): SsoReauth | null {
  return useContext(AdminSsoReauthContext);
}

/**
 * Run a guarded write, spending a single sign-on confirmation once the gateway
 * has answered it. The confirmation holds once: a request the gateway took,
 * or refused, has used it, and the page must offer the provider again; one
 * that never arrived leaves it (`SsoReauth.settle`). Without a single sign-on
 * confirmation the write simply runs.
 */
export function useSpendAdminSso(): <T>(write: () => Promise<T>) => Promise<T> {
  const sso = useAdminSsoReauth();
  return useCallback(
    async <T,>(write: () => Promise<T>): Promise<T> => {
      let result: T;
      try {
        result = await write();
      } catch (error) {
        sso?.settle(error);
        throw error;
      }
      sso?.settle();
      return result;
    },
    [sso],
  );
}

/**
 * Provides {@link useAdminSsoReauth} to the Admin pages. Drawn inside the
 * page's `SsoReauthProvider`, so it knows when the provider has just confirmed.
 *
 * The Admin layout stays mounted while the administrator moves between tabs,
 * so the provider is sent back to the tab open now, not the one the layout
 * first drew. A confirmation is given up once the gateway would no longer
 * honour it, so the page offers the provider again instead of a stale
 * "confirmed".
 */
export function AdminSsoReauthScope({ children }: { children: ReactNode }) {
  const reauth = useSsoReauth(ADMIN_REAUTH_PURPOSE);
  const pathname = usePathname();
  const confirmed = reauth?.confirmed === true;
  const settle = reauth?.settle;

  useEffect(() => {
    if (!confirmed || !settle) return undefined;
    const timer = window.setTimeout(() => settle(), ADMIN_REAUTH_MAX_AGE_MS);
    return () => window.clearTimeout(timer);
    // `settle` is rebuilt with the reauth value; the clock starts with the confirmation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmed]);

  const value = useMemo(
    () => (reauth && pathname ? { ...reauth, returnTo: pathname } : reauth),
    [reauth, pathname],
  );
  return <AdminSsoReauthContext.Provider value={value}>{children}</AdminSsoReauthContext.Provider>;
}
