'use client';

import { useState, type MutableRefObject } from 'react';
import { useTwoFactor } from '@/components/providers/TwoFactorProvider';

/**
 * Whether the signed-in administrator confirms a guarded change with a code
 * from their authenticator app instead of their password. The gateway takes
 * the code from an account that has an authenticator and ignores the password;
 * the console asks for the code when the gateway wants one (`TwoFactorPrompt`).
 */
export function useConfirmsByCode(): boolean {
  return useTwoFactor().status?.enabled === true;
}

/**
 * The administrator's password, inside a confirmation dialog. The dialog's body
 * is fixed when it opens, so the field keeps its own state and hands the value
 * to the write through `valueRef`.
 *
 * An administrator with an authenticator app has nothing to type here: the
 * field gives way to a line saying a code confirms the change.
 */
export function ReauthPassword({
  valueRef,
  label,
}: {
  valueRef: MutableRefObject<string>;
  label: string;
}) {
  const [value, setValue] = useState('');
  const byCode = useConfirmsByCode();
  if (byCode) {
    return (
      <p className="text-[12px] text-gray-700">
        A code from your authenticator app confirms this. You are asked for one unless this session
        gave a code in the last few minutes.
      </p>
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
