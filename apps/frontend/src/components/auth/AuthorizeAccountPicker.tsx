'use client';

import { useT } from '@/components/providers/useT';
import type { AccountSummary } from '@/lib/api/auth';
import { userAccountLabel, userDisplayName } from '@/lib/utils/userLabel';

/**
 * The consent pages' choice of account: which of the accounts signed in on this
 * browser an application is answered for.
 *
 * One radio per account. A signed-out account cannot be picked and offers its
 * own sign-in instead; a page may also rule an account out (`unavailableNote`),
 * which disables it and says why beside its name. Picking an account changes
 * nothing on the browser: the page asks for that account's token only when the
 * visitor goes ahead.
 */
export function AuthorizeAccountPicker({
  accounts,
  chosen,
  onPick,
  signInAgainHref,
  unavailableNote,
}: {
  accounts: AccountSummary[];
  /** The account currently picked, or null for none. */
  chosen: string | null;
  onPick: (accountId: string) => void;
  /** Where a signed-out account signs in again. */
  signInAgainHref: (account: AccountSummary) => string;
  /** Why a signed-in account cannot be picked here, or null when it can. */
  unavailableNote?: (account: AccountSummary) => string | null;
}): JSX.Element {
  const t = useT();
  return (
    <fieldset>
      <legend className="sr-only">{t('auth.authorize.account_legend', 'Account')}</legend>
      <ul className="divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200">
        {accounts.map((account) => {
          const note = account.signed_in ? (unavailableNote?.(account) ?? null) : null;
          return (
            <li key={account.id} className="flex items-center gap-3 px-4 py-3">
              <input
                id={`account-${account.id}`}
                type="radio"
                name="account"
                value={account.id}
                checked={chosen === account.id}
                disabled={!account.signed_in || note !== null}
                onChange={() => onPick(account.id)}
              />
              <label htmlFor={`account-${account.id}`} className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-gray-900">
                  {userDisplayName(account)}
                </span>
                <span className="block truncate text-xs text-gray-500">
                  {userAccountLabel(account)}
                  {note !== null && ` · ${note}`}
                </span>
              </label>
              {!account.signed_in && (
                <a
                  className="shrink-0 text-sm font-medium text-blue-600 hover:text-blue-700"
                  href={signInAgainHref(account)}
                >
                  {t('auth.authorize.sign_in_again', 'Sign in again')}
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </fieldset>
  );
}
