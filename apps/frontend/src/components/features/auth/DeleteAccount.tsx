'use client';

import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import toast from 'react-hot-toast';
import { useAuth } from '@/components/providers';
import type { AuthUser } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel } from '@/lib/utils/userLabel';
import { isCompleteCode, OwnerPhoneCode } from './OwnerPhoneCode';
import { useT, type Translate } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';

/** How the signed-in account proves it is its owner's, or why it cannot here. */
export type DeletionProof = 'password' | 'phone' | 'admin' | 'unavailable';

export function deletionProof(user: AuthUser, phoneAuth: boolean): DeletionProof {
  if (user.role === 'admin' || user.is_admin) return 'admin';
  if (user.has_password !== false) return 'password';
  if (user.phone && user.phone_verified && phoneAuth) return 'phone';
  return 'unavailable';
}

/** Whether `typed` names the account the way the gateway compares it. */
export function confirmationMatches(typed: string, identifier: string): boolean {
  return identifier !== '' && typed.trim().toLowerCase() === identifier.toLowerCase();
}

/**
 * "…, contact {email}." — whole sentences per case, because where the address
 * goes, and what replaces it when there is none, differs between languages.
 */
function cannotDeleteHere(t: Translate, contactEmail: string | undefined): string {
  return contactEmail
    ? fill(
        t(
          'account.delete.unavailable_email',
          'This account cannot confirm its deletion here. To delete it, contact {email}.',
        ),
        { email: contactEmail },
      )
    : t(
        'account.delete.unavailable_admin',
        'This account cannot confirm its deletion here. To delete it, contact the administrator.',
      );
}

function historyKept(t: Translate, contactEmail: string | undefined): string {
  return contactEmail
    ? fill(
        t(
          'account.delete.history_email',
          'Its request history is kept. To have that erased as well, or if you change your mind, contact {email}.',
        ),
        { email: contactEmail },
      )
    : t(
        'account.delete.history_admin',
        'Its request history is kept. To have that erased as well, or if you change your mind, contact the administrator.',
      );
}

/**
 * The body of the settings page's Danger Zone: deleting the signed-in account.
 * The page draws the section around it.
 */
export function DangerZone() {
  const { state } = useAuth();
  const { branding, features } = useSiteConfig();
  const [open, setOpen] = useState(false);
  const t = useT();
  const user = state.user;
  if (!user) return null;
  const proof = deletionProof(user, Boolean(features.phoneAuth));
  const contactEmail = branding.contactEmail;

  return (
    <>
      <div className="flex flex-col gap-3 rounded-lg border border-red-100 bg-red-50/50 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">
            {t('account.delete.heading', 'Delete this account')}
          </h3>
          <p className="mt-1 text-sm text-gray-600">
            {proof === 'admin'
              ? t(
                  'account.delete.admin',
                  'An administrator account cannot delete itself. Ask another administrator to delete it.',
                )
              : proof === 'unavailable'
                ? cannotDeleteHere(t, contactEmail)
                : t(
                    'account.delete.summary',
                    'Revokes its API keys and signs it out everywhere. You cannot undo this yourself.',
                  )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOpen(true)}
          disabled={proof === 'admin' || proof === 'unavailable'}
          className="shrink-0 rounded-md border border-red-300 bg-white px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-600 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t('account.delete.open', 'Delete account')}
        </button>
      </div>
      {open && (proof === 'password' || proof === 'phone') ? (
        <DeleteAccountDialog
          user={user}
          proof={proof}
          contactEmail={contactEmail}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

function DeleteAccountDialog({
  user,
  proof,
  contactEmail,
  onClose,
}: {
  user: AuthUser;
  proof: 'password' | 'phone';
  contactEmail: string | undefined;
  onClose: () => void;
}) {
  const { deleteAccount } = useAuth();
  const t = useT();
  const identifier = userAccountLabel(user);
  const titleId = useId();
  const confirmId = useId();
  const secretId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);

  const [typed, setTyped] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The key handler is installed once; these keep it reading the latest values.
  const busyRef = useRef(busy);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    busyRef.current = busy;
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const restore = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    confirmRef.current?.focus();
    return () => restore?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (!busyRef.current) onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const container = containerRef.current;
      if (!container) return;
      const focusables = container.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), iframe',
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !container.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const matches = confirmationMatches(typed, identifier);
  const proven = proof === 'password' ? password !== '' : isCompleteCode(code);
  const canDelete = matches && proven && !busy;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canDelete) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAccount(
        proof === 'password' ? { confirm: typed, password } : { confirm: typed, phoneCode: code },
      );
      toast.success(t('account.delete.done_toast', 'Your account has been deleted.'));
    } catch (err) {
      setError(getErrorMessage(err));
      setBusy(false);
    }
  };

  const inputClass =
    'mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-red-500 focus:outline-none focus:ring-2 focus:ring-red-500/20';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={() => {
        if (!busy) onClose();
      }}
    >
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-md rounded-xl bg-white p-5 shadow-lg"
        onClick={(event) => event.stopPropagation()}
      >
        <h3 id={titleId} className="text-[15px] font-semibold text-gray-900">
          {t('account.delete.open', 'Delete account')}
        </h3>
        <div className="mt-3 space-y-2 text-[13px] text-gray-600">
          <p>{t('account.delete.will', 'Deleting this account will:')}</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>{t('account.delete.will_revoke', 'revoke all of its API keys at once;')}</li>
            <li>
              {t(
                'account.delete.will_sign_out',
                'sign it out on every device, the Agent included.',
              )}
            </li>
          </ul>
          <p>{historyKept(t, contactEmail)}</p>
        </div>

        <form onSubmit={submit} className="mt-4 space-y-3">
          <div>
            <label htmlFor={confirmId} className="block text-[13px] text-gray-700">
              {t('account.delete.confirm_lead', 'To confirm, type')}{' '}
              <span className="rounded bg-gray-100 px-1 py-0.5 font-mono text-[12px] text-gray-900">
                {identifier}
              </span>{' '}
              {t('account.delete.confirm_tail', 'below:')}
            </label>
            <input
              id={confirmId}
              ref={confirmRef}
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              disabled={busy}
              className={inputClass}
            />
          </div>

          {proof === 'password' ? (
            <div>
              <label htmlFor={secretId} className="block text-[13px] text-gray-700">
                {t('account.delete.password', 'Your password')}
              </label>
              <input
                id={secretId}
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={busy}
                className={inputClass}
              />
            </div>
          ) : (
            <OwnerPhoneCode
              phone={user.phone ?? ''}
              purpose="delete_account"
              value={code}
              onChange={setCode}
              disabled={busy}
            />
          )}

          {error ? (
            <p className="rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700" role="alert">
              {error}
            </p>
          ) : null}

          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-40"
            >
              {t('common.cancel', 'Cancel')}
            </button>
            <button
              type="submit"
              disabled={!canDelete}
              aria-busy={busy || undefined}
              className="rounded-md bg-red-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-600 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy
                ? t('account.delete.deleting', 'Deleting…')
                : t('account.delete.heading', 'Delete this account')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
