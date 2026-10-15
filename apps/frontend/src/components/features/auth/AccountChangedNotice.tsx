'use client';

import { useEffect, useId, useRef } from 'react';

/**
 * Covers the page once a sign-in in another tab has moved this browser to a
 * different account.
 *
 * The page underneath was built for the previous account and the API client
 * now refuses to send anything from it, so the one way forward is a reload,
 * which starts again from the account the browser is signed in to. It cannot be
 * dismissed: a dismissed notice would leave a page whose every action fails.
 */
export function AccountChangedNotice() {
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
          You switched accounts in another tab
        </h3>
        <p id={bodyId} className="mt-2 text-[13px] text-gray-600">
          This browser is now signed in to a different account. Reload this page to continue with
          that account.
        </p>
        <div className="mt-4 flex justify-end">
          <button
            ref={reloadRef}
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-900 focus:ring-offset-2"
          >
            Reload
          </button>
        </div>
      </div>
    </div>
  );
}
