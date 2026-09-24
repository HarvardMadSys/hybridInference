'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import toast from 'react-hot-toast';
import { createAuthSchemas, LoginFormData } from '@/lib/schemas/auth';
import { useAuth } from '@/components/providers';
import { resendVerification } from '@/lib/api/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { internalPathOr } from '@/lib/utils/navigation';
import { useT } from '@/components/providers/useT';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { fill } from '@/lib/utils/interpolate';
import { AuthField, AuthLoading, AuthNotice } from '@/components/auth/AuthForm';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';

function LoginContent() {
  const t = useT();
  const skin = useAuthAppearance();
  const { features } = useSiteConfig();
  const router = useRouter();
  const searchParams = useSearchParams();
  // Where to land after login. Internal paths only — /authorize round-trips
  // through here with its query attached, and a crafted ?next= must never be
  // able to send a fresh session off-site.
  const nextPath = internalPathOr('/dashboard', searchParams.get('next'));
  const { login, state } = useAuth();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Email of an unverified account that just failed to log in. When set, we
  // surface a "resend verification email" action so the user isn't dead-ended.
  const [unverifiedEmail, setUnverifiedEmail] = useState<string | null>(null);
  const [isResending, setIsResending] = useState(false);
  const [resendDone, setResendDone] = useState(false);
  // Validation copy is interface copy too, so the schemas are rebuilt whenever
  // the resolved translator changes; `t` is stable per content document.
  const { loginSchema } = useMemo(() => createAuthSchemas(t), [t]);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginFormData>({
    resolver: zodResolver(loginSchema),
  });

  useEffect(() => {
    if (!state.loading && state.isAuthenticated) {
      router.replace(nextPath);
    }
  }, [state.loading, state.isAuthenticated, router, nextPath]);

  const onSubmit = async (data: LoginFormData) => {
    setIsLoading(true);
    setError(null);
    setUnverifiedEmail(null);
    setResendDone(false);

    try {
      await login(data.email, data.password);
      toast.success(t('auth.login.success_toast', 'Login successful!'));
      router.push(nextPath);
    } catch (err) {
      // For a suspended account, prefer the admin-authored message (when set)
      // over the generic "account suspended" text.
      const suspensionMessage =
        err instanceof APIError && err.code === 'ACCOUNT_SUSPENDED'
          ? (err.details?.suspension_message as string | undefined)
          : undefined;
      const errorMsg = suspensionMessage || getErrorMessage(err);
      setError(errorMsg);
      toast.error(errorMsg);
      if (err instanceof APIError && err.code === 'EMAIL_NOT_VERIFIED') {
        setUnverifiedEmail(data.email);
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleResend = async () => {
    if (!unverifiedEmail) return;
    setIsResending(true);
    try {
      await resendVerification(unverifiedEmail);
      setResendDone(true);
      toast.success(
        t('auth.login.resent_toast', 'Verification email sent. Please check your inbox.'),
      );
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setIsResending(false);
    }
  };

  if (state.loading || state.isAuthenticated) {
    return <AuthLoading />;
  }

  // The field wraps its action in the `field-action` hook itself.
  const forgotLink = (
    <Link href="/forgot-password" className={skin.linkButton} prefetch={false}>
      {t('auth.login.forgot_password', 'Forgot password?')}
    </Link>
  );

  return (
    <AuthPageFrame
      page="login"
      kicker={t('auth.login.kicker', 'GOOD TO SEE YOU AGAIN')}
      title={t('auth.login.title', 'Log In')}
      subtitle={t('auth.login.subtitle', 'Welcome back, please log in with your email')}
      topbar={
        features.publicSignup ? (
          <>
            {t('auth.login.no_account', "Don't have an account?")}{' '}
            <Link href="/signup" prefetch={false}>
              {t('auth.login.signup_link', 'Sign Up')}
            </Link>
          </>
        ) : undefined
      }
      legal={
        <>
          {t('auth.legal.see', 'See')}{' '}
          <Link href="/terms">{t('chrome.footer.terms', 'Terms')}</Link>
          {' · '}
          <Link href="/terms#terms-s5">{t('chrome.footer.privacy', 'Privacy')}</Link>
        </>
      }
    >
      <form onSubmit={handleSubmit(onSubmit)} className={skin.form} data-auth="form">
        {error && <AuthNotice tone="error">{error}</AuthNotice>}

        {unverifiedEmail &&
          (resendDone ? (
            <AuthNotice tone="ok">
              {fill(
                t(
                  'auth.login.resent_notice',
                  'A new verification email is on its way to {email}. Please check your inbox (and spam folder).',
                ),
                { email: unverifiedEmail },
              )}
            </AuthNotice>
          ) : (
            <AuthNotice>
              <p>{t('auth.login.resend_prompt', "Didn't get the verification email?")}</p>
              <button
                type="button"
                className={skin.linkButton}
                onClick={handleResend}
                disabled={isResending}
              >
                {t('auth.login.resend_button', 'Resend verification email')}
              </button>
            </AuthNotice>
          ))}

        <AuthField
          id="email"
          label={t('auth.login.email_label', 'Email')}
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

        <AuthField
          id="password"
          label={t('auth.login.password_label', 'Password')}
          error={errors.password?.message}
          // The action belongs to the field, and where it sits is the
          // module's `fieldLayout`. Nothing here decides that.
          action={forgotLink}
        >
          <input
            id="password"
            className={errors.password?.message ? skin.inputError : skin.input}
            data-auth="control"
            type="password"
            autoComplete="current-password"
            {...register('password')}
          />
        </AuthField>

        <button
          type="submit"
          className={skin.submit}
          data-auth="submit"
          disabled={isLoading}
          aria-busy={isLoading}
        >
          {isLoading ? t('auth.login.submitting', 'Logging in…') : t('auth.login.submit', 'Log In')}
        </button>
      </form>
    </AuthPageFrame>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<AuthLoading />}>
      <LoginContent />
    </Suspense>
  );
}
