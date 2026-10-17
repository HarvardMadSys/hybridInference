'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { TurnstileChallenge } from '@/components/auth/TurnstileChallenge';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { sendPhoneCode, type PhoneCodePurpose } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';

/** The codes that confirm a signed-in account's owner, sent only to its own number. */
export type OwnerCodePurpose = Extract<
  PhoneCodePurpose,
  'delete_account' | 'bind_email' | 'set_password'
>;

const INPUT_CLASS =
  'mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20';

/**
 * A verification code sent to the signed-in account's verified phone, for an
 * account that has no password to confirm a sensitive change with.
 *
 * The number is the account's own; the visitor never types it, and the
 * gateway sends these codes to no other.
 */
export function OwnerPhoneCode({
  phone,
  purpose,
  value,
  onChange,
  disabled = false,
}: {
  phone: string;
  purpose: OwnerCodePurpose;
  value: string;
  onChange: (code: string) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const { branding } = useSiteConfig();
  const inputId = useId();
  const [sending, setSending] = useState(false);
  const [deadline, setDeadline] = useState(0);
  const [seconds, setSeconds] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const captcha = useRef<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);

  useEffect(() => {
    if (!deadline) return;
    const tick = () => setSeconds(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);

  const send = async () => {
    if (sending || seconds > 0) return;
    if (branding.turnstileSiteKey && !captcha.current) {
      setError(t('auth.signup.captcha_required', 'Please complete the captcha.'));
      return;
    }
    setSending(true);
    setError(null);
    setNotice(null);
    try {
      const result = await sendPhoneCode({
        phone,
        purpose,
        turnstileToken: captcha.current ?? undefined,
      });
      const retryAfter = Number.isFinite(result.retry_after) ? Math.max(1, result.retry_after) : 60;
      setSeconds(retryAfter);
      setDeadline(Date.now() + retryAfter * 1000);
      setNotice(fill('A verification code has been sent to {phone}.', { phone }));
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      captcha.current = null;
      setCaptchaReset((count) => count + 1);
      setSending(false);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between">
        <label htmlFor={inputId} className="block text-[13px] text-gray-700">
          {t('auth.phone.code_label', 'Verification code')}
        </label>
        <button
          type="button"
          onClick={send}
          disabled={disabled || sending || seconds > 0}
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
        id={inputId}
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        value={value}
        onChange={(event) => onChange(event.target.value.trim())}
        disabled={disabled}
        className={INPUT_CLASS}
      />
      {branding.turnstileSiteKey ? (
        <div className="mt-2">
          <TurnstileChallenge
            siteKey={branding.turnstileSiteKey}
            onToken={(token) => {
              captcha.current = token;
            }}
            resetKey={captchaReset}
          />
        </div>
      ) : null}
      {notice ? <p className="mt-1.5 text-xs text-green-700">{notice}</p> : null}
      {error ? (
        <p className="mt-1.5 text-xs text-red-600" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** Whether `code` is a complete verification code. */
export function isCompleteCode(code: string): boolean {
  return /^[0-9]{6}$/.test(code);
}
