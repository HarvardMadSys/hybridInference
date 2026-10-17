'use client';

import { useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import clsx from 'clsx';
import { bindEmail } from '@/lib/api/auth';
import { createAuthSchemas, type EmailBindFormData } from '@/lib/schemas/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { useT } from '@/components/providers/useT';
import { NEUTRAL_AUTH_APPEARANCE, useAuthAppearance } from '@/site-ui/appearance';
import { AuthField, AuthNotice } from './AuthForm';

export function EmailBindForm({
  onSuccess,
  isChanging = false,
}: {
  onSuccess: () => Promise<void>;
  isChanging?: boolean;
}) {
  const t = useT();
  const skin = useAuthAppearance();
  const schemas = useMemo(() => createAuthSchemas(t), [t]);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<EmailBindFormData>({
    resolver: zodResolver(schemas.emailBindSchema),
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const onSubmit = async (data: EmailBindFormData) => {
    if (loading) return;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const result = await bindEmail({
        email: data.email.trim(),
      });
      await onSuccess();
      setNotice(
        result.email_verified
          ? 'Email address updated successfully.'
          : fill(
              'Verification email sent to {email}. Click the link in your inbox to finish verifying.',
              { email: result.email },
            ),
      );
      reset({ email: '' });
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
  const labelText = isChanging
    ? 'New email address'
    : t('auth.signup.email_label', 'Email address');
  const hintText = isChanging
    ? 'We will send a verification link to your new address. Your current email stays active until verified.'
    : 'We will send a verification link to this address.';

  return (
    <form
      onSubmit={handleSubmit(onSubmit)}
      className={isNeutral ? 'space-y-4' : skin.form}
      data-auth="form"
    >
      {error && <AuthNotice tone="error">{error}</AuthNotice>}
      {notice && <AuthNotice tone="ok">{notice}</AuthNotice>}
      <AuthField id="bind-email" label={labelText} hint={hintText} error={errors.email?.message}>
        {isNeutral ? (
          <div
            className={clsx(
              'mt-1.5 flex w-full items-stretch rounded-lg border bg-white shadow-sm transition-all duration-200',
              errors.email
                ? 'border-red-300 hover:border-red-400 focus-within:border-red-500 focus-within:ring-2 focus-within:ring-red-500/20'
                : 'border-gray-300 hover:border-gray-400 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-500/20',
            )}
          >
            <span
              className="inline-flex shrink-0 items-center pl-3.5 text-gray-400"
              aria-hidden="true"
            >
              <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                <path d="M3 4a2 2 0 00-2 2v1.161l8.441 4.221a1.25 1.25 0 001.118 0L19 7.162V6a2 2 0 00-2-2H3z" />
                <path d="M19 8.839l-7.77 3.885a2.75 2.75 0 01-2.46 0L1 8.839V14a2 2 0 002 2h14a2 2 0 002-2V8.839z" />
              </svg>
            </span>
            <input
              id="bind-email"
              className="min-w-0 flex-1 border-0 bg-transparent px-3 py-2.5 text-sm text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-0 disabled:cursor-not-allowed disabled:opacity-50"
              data-auth="control"
              type="email"
              autoComplete="email"
              placeholder="name@example.com"
              disabled={loading}
              {...register('email')}
            />
            <button
              type="submit"
              data-auth="submit"
              disabled={loading}
              aria-busy={loading}
              className="inline-flex shrink-0 items-center justify-center rounded-r-lg border-l border-gray-200 bg-gray-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-gray-800 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? 'Sending…' : 'Send verification email'}
            </button>
          </div>
        ) : (
          <input
            id="bind-email"
            className={errors.email ? skin.inputError : skin.input}
            data-auth="control"
            type="email"
            autoComplete="email"
            disabled={loading}
            {...register('email')}
          />
        )}
      </AuthField>
      {!isNeutral && (
        <button
          type="submit"
          className={skin.submit}
          data-auth="submit"
          disabled={loading}
          aria-busy={loading}
        >
          {loading ? 'Sending…' : 'Send verification email'}
        </button>
      )}
    </form>
  );
}
