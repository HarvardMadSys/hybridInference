'use client';

import Link from 'next/link';
import type { FormEvent, ReactNode } from 'react';
import { Button } from '@/components/ui/Button';
import { useTwoFactor } from '@/components/providers/TwoFactorProvider';
import { TwoFactorCodeInput, useTwoFactorCode } from './TwoFactorCode';
import { TWO_FACTOR_ANCHOR } from './TwoFactorSettings';

/**
 * Administration, behind the second factor where the deployment requires one.
 *
 * The gateway refuses every Admin API request of a session that owes a code,
 * so the pages are not mounted until it has given one: they would load into a
 * wall of refusals. An account with no authenticator is sent to add one.
 *
 * A status that cannot be read (a gateway older than this console) opens the
 * pages as before; the gateway's own check is what decides, and a refused
 * request still raises the console's code prompt.
 *
 * Not translated: administration is in English throughout.
 */
export function AdminTwoFactorGate({ children }: { children: ReactNode }) {
  const { status, isPending } = useTwoFactor();
  if (isPending) return null;
  const owed = status?.required ? status : null;
  if (!owed || (owed.enabled && owed.session_verified)) return <>{children}</>;

  return (
    <div className="mx-auto mt-16 max-w-md rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <h2 className="text-base font-semibold text-gray-900">Two-factor authentication</h2>
      {owed.enabled ? (
        <CodeForm />
      ) : (
        <>
          <p className="mt-2 text-sm leading-relaxed text-gray-600">
            Administrator access needs an authenticator app on this account. Add one in Settings,
            then come back.
          </p>
          <Link
            href={`/dashboard/settings#${TWO_FACTOR_ANCHOR}`}
            className="mt-4 inline-flex h-10 items-center rounded-md bg-black px-4 text-sm font-medium text-white shadow-sm transition-colors hover:bg-gray-800"
          >
            Add an authenticator app
          </Link>
        </>
      )}
    </div>
  );
}

function CodeForm() {
  const { code, setCode, busy, error, complete, give } = useTwoFactorCode();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void give();
  };
  return (
    <form onSubmit={submit} className="mt-2 space-y-4">
      <p className="text-sm leading-relaxed text-gray-600">
        Enter a code from your authenticator app to open administration in this browser.
      </p>
      {error && (
        <div
          role="alert"
          className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
        >
          {error}
        </div>
      )}
      <TwoFactorCodeInput value={code} onChange={setCode} disabled={busy} />
      <div className="flex justify-end">
        <Button type="submit" isLoading={busy} disabled={!complete}>
          Verify
        </Button>
      </div>
    </form>
  );
}
