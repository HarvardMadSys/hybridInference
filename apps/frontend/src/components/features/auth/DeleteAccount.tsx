'use client';

import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import toast from 'react-hot-toast';
import { TurnstileChallenge } from '@/components/auth/TurnstileChallenge';
import { Card } from '@/components/ui/Card';
import { useAuth } from '@/components/providers';
import type { AuthUser } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { sendPhoneCode } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { userAccountLabel } from '@/lib/utils/userLabel';

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

function contactText(contactEmail: string | undefined): string {
  return contactEmail ? `contact ${contactEmail}` : 'contact the administrator';
}

/** The settings page's Danger Zone: deleting the signed-in account. */
export function DangerZone() {
  const { state } = useAuth();
  const { branding, features } = useSiteConfig();
  const [open, setOpen] = useState(false);
  const user = state.user;
  if (!user) return null;
  const proof = deletionProof(user, Boolean(features.phoneAuth));
  const contact = contactText(branding.contactEmail);

  return (
    <Card>
      <h2 className="text-lg font-medium mb-3 text-red-700">Danger Zone</h2>
      <div className="flex flex-col gap-3 rounded-lg border border-red-200 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Delete this account</h3>
          <p className="mt-1 text-sm text-gray-600">
            {proof === 'admin'
              ? 'An administrator account cannot delete itself. Ask another administrator to delete it.'
              : proof === 'unavailable'
                ? `This account cannot confirm its deletion here. To delete it, ${contact}.`
                : 'Revokes its API keys and signs it out everywhere. You cannot undo this yourself.'}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOpen(true)}
          disabled={proof === 'admin' || proof === 'unavailable'}
          className="shrink-0 rounded-md border border-red-300 bg-white px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-600 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Delete account
        </button>
      </div>
      {open && (proof === 'password' || proof === 'phone') ? (
        <DeleteAccountDialog
          user={user}
          proof={proof}
          contact={contact}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </Card>
  );
}

function DeleteAccountDialog({
  user,
  proof,
  contact,
  onClose,
}: {
  user: AuthUser;
  proof: 'password' | 'phone';
  contact: string;
  onClose: () => void;
}) {
  const { deleteAccount } = useAuth();
  const { branding } = useSiteConfig();
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
  const [notice, setNotice] = useState<string | null>(null);

  const [sending, setSending] = useState(false);
  const [deadline, setDeadline] = useState(0);
  const [seconds, setSeconds] = useState(0);
  const captcha = useRef<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);

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

  useEffect(() => {
    if (!deadline) return;
    const tick = () => setSeconds(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);

  const matches = confirmationMatches(typed, identifier);
  const proven = proof === 'password' ? password !== '' : /^[0-9]{6}$/.test(code);
  const canDelete = matches && proven && !busy;

  const sendCode = async () => {
    if (sending || seconds > 0 || !user.phone) return;
    if (branding.turnstileSiteKey && !captcha.current) {
      setError(t('auth.signup.captcha_required', 'Please complete the captcha.'));
      return;
    }
    setSending(true);
    setError(null);
    setNotice(null);
    try {
      const result = await sendPhoneCode({
        phone: user.phone,
        purpose: 'delete_account',
        turnstileToken: captcha.current ?? undefined,
      });
      const retryAfter = Number.isFinite(result.retry_after) ? Math.max(1, result.retry_after) : 60;
      setSeconds(retryAfter);
      setDeadline(Date.now() + retryAfter * 1000);
      setNotice(fill('A verification code has been sent to {phone}.', { phone: user.phone ?? '' }));
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      captcha.current = null;
      setCaptchaReset((value) => value + 1);
      setSending(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canDelete) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAccount(
        proof === 'password' ? { confirm: typed, password } : { confirm: typed, phoneCode: code },
      );
      toast.success('Your account has been deleted.');
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
          Delete account
        </h3>
        <div className="mt-3 space-y-2 text-[13px] text-gray-600">
          <p>Deleting this account will:</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>revoke all of its API keys at once;</li>
            <li>sign it out on every device, the Agent included.</li>
          </ul>
          <p>
            Its request history is kept. To have that erased as well, or if you change your mind,{' '}
            {contact}.
          </p>
        </div>

        <form onSubmit={submit} className="mt-4 space-y-3">
          <div>
            <label htmlFor={confirmId} className="block text-[13px] text-gray-700">
              To confirm, type{' '}
              <span className="rounded bg-gray-100 px-1 py-0.5 font-mono text-[12px] text-gray-900">
                {identifier}
              </span>{' '}
              below:
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
                Your password
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
            <div>
              <div className="flex items-center justify-between">
                <label htmlFor={secretId} className="block text-[13px] text-gray-700">
                  {t('auth.phone.code_label', 'Verification code')}
                </label>
                <button
                  type="button"
                  onClick={sendCode}
                  disabled={busy || sending || seconds > 0}
                  aria-busy={sending || undefined}
                  className="text-[13px] font-medium text-blue-600 hover:text-blue-700 disabled:text-gray-400"
                >
                  {sending
                    ? t('auth.phone.sending', 'Sending…')
                    : seconds > 0
                      ? fill(t('auth.phone.retry_in', 'Send again in {seconds}s'), {
                          seconds: String(seconds),
                        })
                      : t('auth.phone.send_code', 'Send verification code')}
                </button>
              </div>
              <input
                id={secretId}
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={code}
                onChange={(event) => setCode(event.target.value.trim())}
                disabled={busy}
                className={inputClass}
              />
              {branding.turnstileSiteKey ? (
                <div className="mt-2">
                  <TurnstileChallenge
                    siteKey={branding.turnstileSiteKey}
                    onToken={(value) => {
                      captcha.current = value;
                    }}
                    resetKey={captchaReset}
                  />
                </div>
              ) : null}
            </div>
          )}

          {notice ? (
            <p className="rounded-lg bg-green-50 px-3 py-2 text-[12px] text-green-700">{notice}</p>
          ) : null}
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
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canDelete}
              aria-busy={busy || undefined}
              className="rounded-md bg-red-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-600 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? 'Deleting…' : 'Delete this account'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
