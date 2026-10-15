'use client';

import { useState } from 'react';
import { useT } from '@/components/providers/useT';
import type { AccountSummary } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel, userDisplayName } from '@/lib/utils/userLabel';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AUTH_DATA, AuthNotice } from './AuthForm';

/**
 * The login page's list of accounts already signed in on this browser.
 *
 * Choosing a signed-in account switches to it without a password; choosing one
 * whose session ended hands its identifier to the sign-in form. Each account
 * can be removed from the browser here, signed in or not, which is how a shared
 * computer is cleaned up.
 */
export function AccountChooser({
  accounts,
  onChoose,
  onSignIn,
  onRemove,
  onUseAnother,
}: {
  accounts: AccountSummary[];
  onChoose: (account: AccountSummary) => Promise<void>;
  onSignIn: (account: AccountSummary) => void;
  onRemove: (account: AccountSummary) => Promise<void>;
  onUseAnother: () => void;
}) {
  const t = useT();
  const skin = useAuthAppearance();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={skin.form} data-auth={AUTH_DATA.form}>
      {error && <AuthNotice tone="error">{error}</AuthNotice>}
      <ul className="divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200">
        {accounts.map((account) => (
          <li key={account.id} className="flex items-center gap-2 bg-white">
            <button
              type="button"
              disabled={busy !== null}
              aria-busy={busy === `choose:${account.id}`}
              onClick={() =>
                account.signed_in
                  ? act(`choose:${account.id}`, () => onChoose(account))
                  : onSignIn(account)
              }
              className="flex min-w-0 flex-1 items-center gap-3 px-4 py-3 text-left hover:bg-gray-50 disabled:opacity-60"
            >
              <span
                aria-hidden="true"
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gray-900 text-sm font-semibold uppercase text-white"
              >
                {userDisplayName(account).slice(0, 1) || '?'}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium text-gray-900">
                  {userDisplayName(account)}
                </span>
                <span className="block truncate text-xs text-gray-500">
                  {account.signed_in
                    ? userAccountLabel(account)
                    : `${userAccountLabel(account)} · ${t('auth.accounts.signed_out', 'Signed out')}`}
                </span>
              </span>
            </button>
            <button
              type="button"
              className={`${skin.linkButton} mr-3 shrink-0`}
              disabled={busy !== null}
              aria-label={`${t('auth.accounts.remove', 'Remove')} ${userAccountLabel(account)}`}
              onClick={() => act(`remove:${account.id}`, () => onRemove(account))}
            >
              {t('auth.accounts.remove', 'Remove')}
            </button>
          </li>
        ))}
      </ul>
      <button type="button" className={skin.linkButton} onClick={onUseAnother}>
        {t('auth.accounts.use_another', 'Use another account')}
      </button>
    </div>
  );
}
