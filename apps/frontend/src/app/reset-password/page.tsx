'use client';

import { Suspense, useMemo, useState, useEffect } from 'react';
import { useSearchParams } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { createAuthSchemas } from '@/lib/schemas/auth';
import { resetPassword } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import Link from 'next/link';
import { useT } from '@/components/providers/useT';
import { AuthField, AuthLoading, AuthNotice } from '@/components/auth/AuthForm';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';

export const dynamic = 'force-dynamic';

/**
 * The unrefined shape names the form type. The runtime schema below is the
 * same shape with the shared password rules and the match check attached, so
 * the type stays as literal as the old module-level `z.infer` was. The
 * underscore marks it as type-level only: the value is never read.
 */
const _resetPasswordShape = {
  password: z.string(),
  confirmPassword: z.string(),
};

type ResetPasswordFormData = z.infer<z.ZodObject<typeof _resetPasswordShape>>;

function ResetPasswordContent() {
  const t = useT();
  const skin = useAuthAppearance();
  const searchParams = useSearchParams();
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  // The shared password rules and match check, rebuilt per translator so the
  // messages resolve through the same slots the signup form uses.
  const resetPasswordSchema = useMemo(() => {
    const { passwordSchema } = createAuthSchemas(t);
    return z
      .object({
        password: passwordSchema,
        confirmPassword: z.string(),
      })
      .refine((data) => data.password === data.confirmPassword, {
        message: t('auth.validation.passwords_differ', 'Passwords do not match'),
        path: ['confirmPassword'],
      });
  }, [t]);

  useEffect(() => {
    const tokenParam = searchParams.get('token');
    setToken(tokenParam);
    if (!tokenParam) {
      setError(t('auth.reset.invalid_token', 'Invalid or missing reset token'));
    }
  }, [searchParams, t]);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<ResetPasswordFormData>({
    resolver: zodResolver(resetPasswordSchema),
  });

  const onSubmit = async (data: ResetPasswordFormData) => {
    if (!token) {
      setError(t('auth.reset.invalid_token', 'Invalid or missing reset token'));
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      await resetPassword(token, data.password);
      setSuccess(true);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setIsLoading(false);
    }
  };

  if (success) {
    return (
      <AuthPageFrame
        page="reset-password"
        kicker={t('auth.reset.success_kicker', 'ALL SET')}
        title={t('auth.reset.success_title', 'Password Reset Successful!')}
        subtitle={t(
          'auth.reset.success_body',
          'Your password has been reset successfully. You can now log in with your new password.',
        )}
      >
        <div className={skin.form} data-auth="form">
          <Link href="/login" className={skin.submit} data-auth="submit" prefetch={false}>
            {t('auth.reset.go_to_login', 'Go to Login')}
          </Link>
        </div>
      </AuthPageFrame>
    );
  }

  return (
    <AuthPageFrame
      page="reset-password"
      kicker={t('auth.reset.kicker', 'CHOOSE A NEW PASSWORD')}
      title={t('auth.reset.title', 'Reset Password')}
      subtitle={t('auth.reset.subtitle', 'Enter your new password below')}
      topbar={
        <>
          {t('auth.reset.remember', 'Remember your password?')}{' '}
          <Link href="/login" prefetch={false}>
            {t('auth.reset.login_link', 'Log In')}
          </Link>
        </>
      }
    >
      <form onSubmit={handleSubmit(onSubmit)} className={skin.form} data-auth="form">
        {error && <AuthNotice tone="error">{error}</AuthNotice>}

        <AuthField
          id="password"
          label={t('auth.reset.new_password_label', 'New Password')}
          hint={t(
            'auth.reset.new_password_hint',
            'At least 8 characters with uppercase, lowercase, and numbers',
          )}
          error={errors.password?.message}
        >
          <input
            id="password"
            className={errors.password?.message ? skin.inputError : skin.input}
            data-auth="control"
            type="password"
            autoComplete="new-password"
            {...register('password')}
          />
        </AuthField>

        <AuthField
          id="confirmPassword"
          label={t('auth.reset.confirm_password_label', 'Confirm New Password')}
          error={errors.confirmPassword?.message}
        >
          <input
            id="confirmPassword"
            className={errors.confirmPassword?.message ? skin.inputError : skin.input}
            data-auth="control"
            type="password"
            autoComplete="new-password"
            {...register('confirmPassword')}
          />
        </AuthField>

        <button
          type="submit"
          className={skin.submit}
          data-auth="submit"
          disabled={isLoading || !token}
          aria-busy={isLoading}
        >
          {isLoading
            ? t('auth.reset.submitting', 'Resetting…')
            : t('auth.reset.submit', 'Reset Password')}
        </button>
      </form>
    </AuthPageFrame>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<AuthLoading />}>
      <ResetPasswordContent />
    </Suspense>
  );
}
