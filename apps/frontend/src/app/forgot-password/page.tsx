'use client';

import { useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { createAuthSchemas } from '@/lib/schemas/auth';
import { forgotPassword } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import Link from 'next/link';
import { useT } from '@/components/providers/useT';
import { AuthField, AuthNotice } from '@/components/auth/AuthForm';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';

/** The forgot-password form is the shared email field on its own. */
type ForgotPasswordFormData = {
  email: z.infer<ReturnType<typeof createAuthSchemas>['emailSchema']>;
};

export default function ForgotPasswordPage() {
  const t = useT();
  const skin = useAuthAppearance();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  // The shared factory rather than a local copy of its message: one slot, one
  // English default, one place to translate.
  const forgotPasswordSchema = useMemo(
    () => z.object({ email: createAuthSchemas(t).emailSchema }),
    [t],
  );

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<ForgotPasswordFormData>({
    resolver: zodResolver(forgotPasswordSchema),
  });

  const onSubmit = async (data: ForgotPasswordFormData) => {
    setIsLoading(true);
    setError(null);

    try {
      await forgotPassword(data.email);
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
        page="forgot-password"
        kicker={t('auth.forgot.success_kicker', 'CHECK YOUR INBOX')}
        title={t('auth.forgot.success_title', 'Check Your Email')}
        subtitle={t(
          'auth.forgot.success_body',
          "If an account exists with this email, we've sent a password reset link. Please check your inbox.",
        )}
      >
        <div className={skin.form} data-auth="form">
          <Link href="/login" className={skin.submit} data-auth="submit" prefetch={false}>
            {t('auth.forgot.back_to_login', 'Back to Login')}
          </Link>
        </div>
      </AuthPageFrame>
    );
  }

  return (
    <AuthPageFrame
      page="forgot-password"
      kicker={t('auth.forgot.kicker', 'RESET YOUR PASSWORD')}
      title={t('auth.forgot.title', 'Forgot Password')}
      subtitle={t(
        'auth.forgot.subtitle',
        "Enter your email address and we'll send you a link to reset your password",
      )}
      topbar={
        <>
          {t('auth.forgot.remember', 'Remember your password?')}{' '}
          <Link href="/login" prefetch={false}>
            {t('auth.forgot.login_link', 'Log In')}
          </Link>
        </>
      }
    >
      <form onSubmit={handleSubmit(onSubmit)} className={skin.form} data-auth="form">
        {error && <AuthNotice tone="error">{error}</AuthNotice>}

        <AuthField
          id="email"
          label={t('auth.forgot.email_label', 'Email')}
          error={errors.email?.message}
        >
          <input
            id="email"
            className={errors.email?.message ? skin.inputError : skin.input}
            data-auth="control"
            type="email"
            autoComplete="email"
            {...register('email')}
          />
        </AuthField>

        <button
          type="submit"
          className={skin.submit}
          data-auth="submit"
          disabled={isLoading}
          aria-busy={isLoading}
        >
          {isLoading
            ? t('auth.forgot.submitting', 'Sending…')
            : t('auth.forgot.submit', 'Send Reset Link')}
        </button>
      </form>
    </AuthPageFrame>
  );
}
