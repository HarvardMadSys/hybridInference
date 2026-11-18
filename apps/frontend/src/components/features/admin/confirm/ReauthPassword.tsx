'use client';

import { useState, type MutableRefObject } from 'react';
import { SsoReauthConfirm } from '@/components/features/auth/SsoReauth';
import { useTwoFactor } from '@/components/providers/TwoFactorProvider';
import { useAdminSsoReauth } from './adminSsoReauth';

export { ADMIN_REAUTH_PURPOSE, useAdminSsoReauth } from './adminSsoReauth';

/**
 * How the signed-in administrator confirms a guarded change, in the order the
 * gateway asks: a code from an authenticator app when the account has one (the
 * console asks for it when the gateway wants one, `TwoFactorPrompt`), else a
 * fresh sign-in at its identity provider when it has neither a password nor a
 * verified phone, else its password.
 */
export type ConfirmMode = 'code' | 'sso' | 'password';

/**
 * Whether the signed-in administrator confirms a guarded change with a code
 * from their authenticator app instead of their password. The gateway takes
 * the code from an account that has an authenticator and ignores the password.
 */
export function useConfirmsByCode(): boolean {
  return useTwoFactor().status?.enabled === true;
}

/** How the signed-in administrator confirms a guarded change (see {@link ConfirmMode}). */
export function useConfirmMode(): ConfirmMode {
  const byCode = useConfirmsByCode();
  const sso = useAdminSsoReauth();
  if (byCode) return 'code';
  return sso ? 'sso' : 'password';
}

/**
 * Whether a guarded change is sent without a password: confirmed by a code or
 * by a fresh single sign-on. A dialog asks for the password only otherwise.
 */
export function useConfirmsWithoutPassword(): boolean {
  return useConfirmMode() !== 'password';
}

/**
 * The administrator's password, inside a confirmation dialog. The dialog's body
 * is fixed when it opens, so the field keeps its own state and hands the value
 * to the write through `valueRef`.
 *
 * An administrator with an authenticator app has nothing to type here: the
 * field gives way to a line saying a code confirms the change. One that signs
 * in only through an identity provider is offered the button that signs in
 * there again; the browser comes back to this page, and the change made again
 * within five minutes is confirmed.
 */
export function ReauthPassword({
  valueRef,
  label,
}: {
  valueRef: MutableRefObject<string>;
  label: string;
}) {
  const [value, setValue] = useState('');
  const mode = useConfirmMode();
  const sso = useAdminSsoReauth();
  if (mode === 'code') {
    return (
      <p className="text-[12px] text-gray-700">
        A code from your authenticator app confirms this. You are asked for one unless this session
        gave a code in the last few minutes.
      </p>
    );
  }
  if (mode === 'sso' && sso) {
    return (
      <div className="space-y-1.5 text-[12px] text-gray-700">
        {sso.confirmed ? null : (
          <p>
            Your account signs in through {sso.provider.name}. Sign in there again to confirm: you
            come back to this page, and the change you make again within five minutes is confirmed.
          </p>
        )}
        <SsoReauthConfirm reauth={sso} />
      </div>
    );
  }
  return (
    <label className="block text-[12px] text-gray-700">
      {label}
      <input
        type="password"
        value={value}
        onChange={(event) => {
          setValue(event.target.value);
          valueRef.current = event.target.value;
        }}
        autoComplete="current-password"
        className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
      />
    </label>
  );
}
