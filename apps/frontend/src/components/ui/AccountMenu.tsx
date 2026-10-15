'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import toast from 'react-hot-toast';
import { useAuth } from '@/components/providers';
import type { AccountSummary } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel, userDisplayName } from '@/lib/utils/userLabel';

function Initial({ name }: { name: string }) {
  return (
    <span
      aria-hidden="true"
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gray-900 text-xs font-semibold uppercase text-white"
    >
      {name.slice(0, 1) || '?'}
    </span>
  );
}

/** Where "Add another account" lands: the sign-in form, returning here afterwards. */
function addAccountHref(pathname: string): string {
  return `/login?add=1&next=${encodeURIComponent(pathname || '/dashboard')}`;
}

/**
 * The signed-in account, the browser's other accounts, and the ways out.
 *
 * Switching changes the browser's account, not just this tab's: every other
 * console tab is told to reload (see `accountSync`). An account whose session
 * has ended is listed too, and choosing it goes to the sign-in form.
 */
export function AccountMenu() {
  const { state, accounts, maxAccounts, switchAccount, logout, logoutAll } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const user = state.user;
  if (!user) return null;
  const others = accounts.filter((account) => account.id !== user.id);
  const canAdd = accounts.length < maxAccounts;

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setBusy(false);
      setOpen(false);
    }
  };

  const choose = (account: AccountSummary) => {
    if (!account.signed_in) {
      const identifier = account.email || account.login_name || '';
      router.push(
        `${addAccountHref(pathname)}${identifier ? `&identifier=${encodeURIComponent(identifier)}` : ''}`,
      );
      setOpen(false);
      return;
    }
    void run(async () => {
      await switchAccount(account.id);
      toast.success(`Switched to ${userDisplayName(account)}`);
    });
  };

  const signOut = () =>
    run(async () => {
      await logout();
      router.replace(others.length > 0 ? '/login' : '/');
    });

  const signOutAll = () =>
    run(async () => {
      await logoutAll();
      router.replace('/');
    });

  const itemClass =
    'flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50';

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900"
      >
        <Initial name={userDisplayName(user)} />
        <span className="hidden max-w-[10rem] truncate sm:inline">{userDisplayName(user)}</span>
      </button>
      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label="Account"
          className="absolute right-0 z-40 mt-2 w-72 rounded-xl bg-white p-2 shadow-lg ring-1 ring-gray-200"
        >
          <div className="flex items-center gap-3 px-3 py-2">
            <Initial name={userDisplayName(user)} />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-gray-900">
                {userDisplayName(user)}
              </p>
              <p className="truncate text-xs text-gray-500">
                {userAccountLabel(user)} · {user.role}
              </p>
            </div>
          </div>

          {others.length > 0 && (
            <div className="mt-1 border-t border-gray-100 pt-1">
              {others.map((account) => (
                <button
                  key={account.id}
                  type="button"
                  role="menuitem"
                  disabled={busy}
                  onClick={() => choose(account)}
                  className={itemClass}
                >
                  <Initial name={userDisplayName(account)} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{userDisplayName(account)}</span>
                    <span className="block truncate text-xs text-gray-500">
                      {account.signed_in ? userAccountLabel(account) : 'Signed out · sign in again'}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}

          <div className="mt-1 border-t border-gray-100 pt-1">
            {canAdd ? (
              <button
                type="button"
                role="menuitem"
                disabled={busy}
                onClick={() => {
                  setOpen(false);
                  router.push(addAccountHref(pathname));
                }}
                className={itemClass}
              >
                Add another account
              </button>
            ) : (
              <p className="px-3 py-2 text-xs text-gray-500">
                This browser holds {maxAccounts} accounts, the most it can. Sign one out to add
                another.
              </p>
            )}
            <button
              type="button"
              role="menuitem"
              disabled={busy}
              onClick={signOut}
              className={itemClass}
            >
              Log out
            </button>
            {others.length > 0 && (
              <button
                type="button"
                role="menuitem"
                disabled={busy}
                onClick={signOutAll}
                className={itemClass}
              >
                Log out of all accounts
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
