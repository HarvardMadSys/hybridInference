'use client';

import { useEffect, useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { bindEmail } from '@/lib/api/auth';
import { createAuthSchemas, type EmailBindFormData } from '@/lib/schemas/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { useT } from '@/components/providers/useT';
import { NEUTRAL_AUTH_APPEARANCE, useAuthAppearance } from '@/site-ui/appearance';
import { InputField } from '@/components/ui/InputField';
import { isCompleteCode, OwnerPhoneCode } from '@/components/features/auth/OwnerPhoneCode';
import { AuthField, AuthNotice } from './AuthForm';

/**
 * Bind an email to the signed-in account, or change it.
 *
 * The owner confirms the way the account signs in: its current password, or,
 * for an account without one, a code sent to its verified `phone`. The address
 * itself is then proven by a link sent to it.
 */
export function EmailBindForm({
  currentEmail,
  hasPassword,
  phone,
  onSuccess,
}: {
  currentEmail?: string | null;
  hasPassword: boolean;
  phone?: string | null;
  onSuccess: () => Promise<void>;
}) {
  const t = useT();
  const skin = useAuthAppearance();
  const schemas = useMemo(() => createAuthSchemas(t), [t]);
  const {
    register,
    handleSubmit,
    reset,
    watch,
    formState: { errors },
  } = useForm<EmailBindFormData>({
    resolver: zodResolver(schemas.emailBindSchema),
    defaultValues: { email: currentEmail || '' },
  });
  const [password, setPassword] = useState('');
  const [phoneCode, setPhoneCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    reset({ email: currentEmail || '' });
  }, [currentEmail, reset]);

  const watchedEmail = watch('email');
  const isUnchangedCurrent =
    Boolean(currentEmail) &&
    (watchedEmail || '').trim().toLowerCase() === currentEmail?.trim().toLowerCase();

  const onSubmit = async (data: EmailBindFormData) => {
    if (loading) return;
    const trimmed = data.email.trim();
    if (currentEmail && trimmed.toLowerCase() === currentEmail.trim().toLowerCase()) {
      setError(
        t(
          'auth.email_bind.same_email',
          'Please enter a different email address to change your email.',
        ),
      );
      return;
    }
    if (hasPassword ? !password : !isCompleteCode(phoneCode)) {
      setError(
        hasPassword
          ? t(
              'auth.email_bind.password_required',
              'Enter your current password to confirm this change.',
            )
          : t('settings.password.code_required', 'Enter the verification code sent to your phone.'),
      );
      return;
    }
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const result = await bindEmail(
        hasPassword ? { email: trimmed, password } : { email: trimmed, phone_code: phoneCode },
      );
      setPassword('');
      setPhoneCode('');
      await onSuccess();
      setNotice(
        result.email_verified
          ? t('auth.email_bind.updated', 'Email address updated successfully.')
          : fill(
              t(
                'auth.email_bind.sent',
                'Verification email sent to {email}. Click the link in your inbox to finish verifying.',
              ),
              { email: result.email },
            ),
      );
    } catch (err) {
      const suspensionMessage =
        err instanceof APIError && err.code === 'ACCOUNT_SUSPENDED'
          ? (err.details?.suspension_message as string | undefined)
          : undefined;
      setError(suspensionMessage || getErrorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  const isNeutral = skin === NEUTRAL_AUTH_APPEARANCE;
  const hintText = currentEmail
    ? t(
        'auth.email_bind.change_hint',
        'Enter a new email address to send a verification link. Your current email stays active until verified.',
      )
    : t('auth.email_bind.bind_hint', 'We will send a verification link to this address.');
  const submitLabel = loading
    ? 'Sending…'
    : currentEmail
      ? t('auth.email_bind.change', 'Change Email')
      : t('auth.email_bind.send', 'Send verification email');

  const submitButton = (
    <button
      type="submit"
      className={isNeutral ? skin.submit.replace(/\bw-full\b/, '') : skin.submit}
      data-auth="submit"
      disabled={loading}
      aria-busy={loading}
    >
      {submitLabel}
    </button>
  );

  return (
    <form
      onSubmit={handleSubmit(onSubmit)}
      className={isNeutral ? 'space-y-4' : skin.form}
      data-auth="form"
    >
      {error && <AuthNotice tone="error">{error}</AuthNotice>}
      {notice && <AuthNotice tone="ok">{notice}</AuthNotice>}
      <AuthField
        id="bind-email"
        label={t('auth.signup.email_label', 'Email')}
        hint={hintText}
        error={errors.email?.message}
      >
        <div className="relative mt-1.5">
          <input
            id="bind-email"
            className={`${errors.email ? skin.inputError : skin.input} !mt-0 ${isUnchangedCurrent ? 'pr-24' : ''}`}
            data-auth="control"
            type="email"
            autoComplete="email"
            placeholder="name@example.com"
            disabled={loading}
            {...register('email', {
              onChange: () => {
                setError(null);
              },
            })}
          />
          {isUnchangedCurrent && (
            <span className="pointer-events-none absolute right-3 top-1/2 inline-flex -translate-y-1/2 items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/20">
              <svg
                className="h-3 w-3 text-emerald-600"
                viewBox="0 0 20 20"
                fill="currentColor"
                aria-hidden="true"
              >
                <path
                  fillRule="evenodd"
                  d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
                  clipRule="evenodd"
                />
              </svg>
              {t('settings.phone.verified', 'Verified')}
            </span>
          )}
        </div>
      </AuthField>
      {hasPassword ? (
        <InputField
          label={t('auth.email_bind.current_password', 'Current password')}
          type="password"
          autoComplete="current-password"
          hint={t('auth.email_bind.current_password_hint', 'Confirms that this change is yours.')}
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
            setError(null);
          }}
          disabled={loading}
        />
      ) : phone ? (
        <OwnerPhoneCode
          phone={phone}
          purpose="bind_email"
          value={phoneCode}
          onChange={(code) => {
            setPhoneCode(code);
            setError(null);
          }}
          disabled={loading}
        />
      ) : null}
      {isNeutral ? <div className="flex justify-end">{submitButton}</div> : submitButton}
    </form>
  );
}
