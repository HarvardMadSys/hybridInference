'use client';

import { useCallback, useId, useState, type RefObject } from 'react';
import { useRefreshTwoFactor } from '@/components/providers/TwoFactorProvider';
import { useT } from '@/components/providers/useT';
import { isCompleteTwoFactorCode, verifyTwoFactor } from '@/lib/api/twoFactor';
import { getErrorMessage } from '@/lib/utils/errors';

/**
 * Giving this session a code: the typed value, the request, and its failure.
 * `give` resolves to whether the session is now verified.
 */
export function useTwoFactorCode() {
  const refresh = useRefreshTwoFactor();
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const complete = isCompleteTwoFactorCode(code);

  const give = useCallback(async () => {
    if (busy || !isCompleteTwoFactorCode(code)) return false;
    setBusy(true);
    setError(null);
    try {
      await verifyTwoFactor(code.trim());
    } catch (e) {
      setError(getErrorMessage(e));
      setBusy(false);
      return false;
    }
    setCode('');
    // What the session has proved is part of the status every gate reads.
    await refresh();
    setBusy(false);
    return true;
  }, [busy, code, refresh]);

  return { code, setCode, busy, error, complete, give };
}

/** Where a code from the authenticator app, or a recovery code, is typed. */
export function TwoFactorCodeInput({
  value,
  onChange,
  onEnter,
  disabled = false,
  inputRef,
}: {
  value: string;
  onChange: (code: string) => void;
  /** Enter in the field, for a surface with no form of its own. */
  onEnter?: () => void;
  disabled?: boolean;
  inputRef?: RefObject<HTMLInputElement>;
}) {
  const t = useT();
  const hintId = useId();
  return (
    <label className="block text-[13px] font-medium text-gray-700">
      {t('two_factor.code.label', 'Authentication code')}
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && onEnter) {
            event.preventDefault();
            onEnter();
          }
        }}
        disabled={disabled}
        autoComplete="one-time-code"
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
        aria-describedby={hintId}
        className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-sm tracking-wider text-gray-900 shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 disabled:opacity-60"
      />
      <span id={hintId} className="mt-1.5 block text-xs font-normal text-gray-500">
        {t(
          'two_factor.code.hint',
          'Six digits from your authenticator app. Without your device, enter one of your recovery codes.',
        )}
      </span>
    </label>
  );
}
