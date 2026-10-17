'use client';

import { useEffect, useId, useRef } from 'react';
import type { AccountChangeReason } from '@/lib/api/client';
import { useT, type Translate } from '@/components/providers/useT';

function wordingFor(reason: AccountChangeReason, t: Translate): { title: string; body: string } {
  return reason === 'switched'
    ? {
        title: t('chrome.account_changed.switched_title', 'You switched accounts in another tab'),
        body: t(
          'chrome.account_changed.switched_body',
          'This browser is now using a different account. Reload this page to continue with that account.',
        ),
      }
    : {
        title: t('chrome.account_changed.signed_out_title', 'You signed out in another tab'),
        body: t(
          'chrome.account_changed.signed_out_body',
          'This account is no longer signed in on this browser. Reload this page to choose an account.',
        ),
      };
}

/**
 * Covers the page once another tab has moved this browser to a different
 * account, or signed out the one this page shows.
 *
 * The page underneath was built for the previous account and the API client
 * now refuses to send anything from it, so the one way forward is a reload,
 * which starts again from the account the browser is signed in to. It cannot be
 * dismissed: a dismissed notice would leave a page whose every action fails.
 */
export function AccountChangedNotice({ reason }: { reason: AccountChangeReason }) {
  const t = useT();
  const wording = wordingFor(reason, t);
  const titleId = useId();
  const bodyId = useId();
  const reloadRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    reloadRef.current?.focus();
  }, []);

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 px-4">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        className="w-full max-w-md rounded-xl bg-white p-5 shadow-lg"
      >
        <h3 id={titleId} className="text-[15px] font-semibold text-gray-900">
          {wording.title}
        </h3>
        <p id={bodyId} className="mt-2 text-[13px] text-gray-600">
          {wording.body}
        </p>
        <div className="mt-4 flex justify-end">
          <button
            ref={reloadRef}
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-900 focus:ring-offset-2"
          >
            {t('chrome.account_changed.reload', 'Reload')}
          </button>
        </div>
      </div>
    </div>
  );
}
