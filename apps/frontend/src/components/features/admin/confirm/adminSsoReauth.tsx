'use client';

import { createContext, useContext, type ReactNode } from 'react';
import { useSsoReauth, type SsoReauth } from '@/components/features/auth/SsoReauth';

/**
 * What a fresh sign-in at the administrator's identity provider confirms: one
 * guarded change of this session (the gateway's `ADMIN_REAUTH_PURPOSE`).
 */
export const ADMIN_REAUTH_PURPOSE = 'admin_action';

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
 * Provides {@link useAdminSsoReauth} to the Admin pages. Drawn inside the
 * page's `SsoReauthProvider`, so it knows when the provider has just confirmed.
 */
export function AdminSsoReauthScope({ children }: { children: ReactNode }) {
  const reauth = useSsoReauth(ADMIN_REAUTH_PURPOSE);
  return <AdminSsoReauthContext.Provider value={reauth}>{children}</AdminSsoReauthContext.Provider>;
}
