'use client';

import type { ReactNode } from 'react';
import {
  ADMIN_REAUTH_PURPOSE,
  AdminSsoReauthScope,
} from '@/components/features/admin/confirm/adminSsoReauth';
import { SsoReauthProvider, useSsoReturn } from '@/components/features/auth/SsoReauth';

/**
 * Reads how a round trip to the administrator's identity provider came back to
 * an Admin page, for every guarded change drawn on it.
 *
 * An administrator who signs in only through a provider confirms a guarded
 * change by signing in there again (`ReauthPassword`). The page is a new
 * document when the browser returns, so what it was doing is gone: this says
 * the confirmation holds, and the change made again within five minutes uses
 * it. Drawn only for a signed-in administrator, in the browser.
 */
export function AdminSsoReturn({ userId, children }: { userId: string; children: ReactNode }) {
  const { reauth, error } = useSsoReturn(userId);
  return (
    <SsoReauthProvider value={reauth}>
      <AdminSsoReauthScope>
        {reauth.confirmed === ADMIN_REAUTH_PURPOSE ? (
          <p
            role="status"
            className="mt-3 rounded-md border border-green-200 bg-green-50 px-3 py-2 text-[13px] text-green-700"
          >
            Identity confirmed. Make the change again within five minutes to save it.
          </p>
        ) : null}
        {error ? (
          <p
            role="alert"
            className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-[13px] text-red-700"
          >
            Single sign-on did not confirm your identity. Try again.
          </p>
        ) : null}
        {children}
      </AdminSsoReauthScope>
    </SsoReauthProvider>
  );
}
