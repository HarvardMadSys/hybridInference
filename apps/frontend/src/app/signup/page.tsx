'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import Script from 'next/script';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { buildCombinedUseCase, createAuthSchemas, SignupFormData } from '@/lib/schemas/auth';
import { signup, type SignupResponse } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';
import { AuthField, AuthNotice } from '@/components/auth/AuthForm';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';
import { SignupConsentStep } from './SignupConsentStep';

const TURNSTILE_CALLBACK = '__signupTurnstileCallback';

declare global {
  interface Window {
    [TURNSTILE_CALLBACK]?: (token: string) => void;
  }
}

export default function SignupPage() {
  const t = useT();
  const skin = useAuthAppearance();
  const { branding, features } = useSiteConfig();
  const turnstileSiteKey = branding.turnstileSiteKey;
  const [step, setStep] = useState<'consent' | 'form'>('consent');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signupResult, setSignupResult] = useState<SignupResponse | null>(null);
  const turnstileTokenRef = useRef<string | null>(null);
  // Validation copy is interface copy too: the schemas are rebuilt per
  // translator, and the signup one also takes the runtime site host because it
  // measures the text buildCombinedUseCase will submit.
  const runtimeSignupSchema = useMemo(
    () => createAuthSchemas(t).createSignupSchema(branding.siteHost),
    [t, branding.siteHost],
  );

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<SignupFormData>({
    resolver: zodResolver(runtimeSignupSchema),
  });

  useEffect(() => {
    turnstileTokenRef.current = null;
    if (!turnstileSiteKey) return;
    window[TURNSTILE_CALLBACK] = (token: string) => {
      turnstileTokenRef.current = token;
    };
    return () => {
      delete window[TURNSTILE_CALLBACK];
    };
  }, [turnstileSiteKey]);

  const onSubmit = async (data: SignupFormData) => {
    setIsLoading(true);
    setError(null);

    if (turnstileSiteKey && !turnstileTokenRef.current) {
      setError(t('auth.signup.captcha_required', 'Please complete the captcha.'));
      setIsLoading(false);
      return;
    }

    try {
      const combinedUseCase = buildCombinedUseCase(
        data.useCase,
        data.discoverySource,
        branding.siteHost,
      );

      const result = await signup({
        email: data.email,
        password: data.password,
        user_name: data.userName.trim(),
        use_case: combinedUseCase || undefined,
        // The account form is only reachable after every confirmation on the
        // preceding step was checked — the console's four, or the module's
        // `consentItems` when it publishes its own terms — and the backend
        // stores one flag for all of them.
        accepted_tos: true,
        turnstileToken: turnstileTokenRef.current ?? undefined,
      });
      setSignupResult(result);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setIsLoading(false);
    }
  };

  if (!features.publicSignup) {
    return (
      <AuthPageFrame
        page="signup"
        kicker={t('auth.signup.kicker_unavailable', 'REGISTRATION CLOSED')}
        title={t('auth.signup.unavailable_title', 'Public signup is unavailable')}
        subtitle={t(
          'auth.signup.unavailable_body',
          'This distribution does not currently accept public registrations.',
        )}
      >
        <div className={skin.form} data-auth="form">
          <Link href="/login" className={skin.submit} data-auth="submit">
            {t('auth.signup.signin_link', 'Sign in')}
          </Link>
        </div>
      </AuthPageFrame>
    );
  }

  if (signupResult) {
    const isPendingApproval = signupResult.requires_approval;

    return (
      <AuthPageFrame
        page="signup"
        kicker={t('auth.signup.kicker_result', 'ALMOST THERE')}
        title={
          isPendingApproval
            ? t('auth.signup.pending_title', 'Registration Submitted')
            : t('auth.signup.success_title', 'Registration Successful!')
        }
        subtitle={t('auth.signup.result_subtitle', 'Here is what happens with your account next.')}
      >
        <div className={skin.form} data-auth="form">
          <AuthNotice tone={isPendingApproval ? 'info' : 'ok'}>{signupResult.message}</AuthNotice>
          <Link href="/login" className={skin.submit} data-auth="submit" prefetch={false}>
            {t('auth.signup.back_to_login', 'Back to Login')}
          </Link>
        </div>
      </AuthPageFrame>
    );
  }

  if (step === 'consent') {
    return <SignupConsentStep onContinue={() => setStep('form')} />;
  }

  return (
    <AuthPageFrame
      page="signup"
      kicker={t('auth.signup.kicker', 'START BUILDING')}
      title={t('auth.signup.title', 'Sign Up')}
      subtitle={t('auth.signup.subtitle', 'Create an account to manage API keys and usage')}
      topbar={
        <>
          {t('auth.signup.have_account', 'Already have an account?')}{' '}
          <Link href="/login" prefetch={false}>
            {t('auth.signup.login_link', 'Log In')}
          </Link>
        </>
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

        {branding.fastTrackDomain && (
          <AuthNotice>
            {fill(
              t(
                'auth.signup.fast_track',
                'Open to {org} students — sign up with your @{domain} email for instant access. Everyone else: please describe your use case below — we review and approve manually.',
              ),
              { org: branding.fastTrackOrg, domain: branding.fastTrackDomain },
            )}
          </AuthNotice>
        )}

        <AuthField
          id="email"
          label={t('auth.signup.email_label', 'Email')}
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
          id="userName"
          label={t('auth.signup.username_label', 'Username')}
          hint={t(
            'auth.signup.username_hint',
            'This name is shown in your account and admin review.',
          )}
          error={errors.userName?.message}
        >
          <input
            id="userName"
            className={errors.userName?.message ? skin.inputError : skin.input}
            data-auth="control"
            type="text"
            autoComplete="username"
            {...register('userName')}
          />
        </AuthField>

        <AuthField
          id="password"
          label={t('auth.signup.password_label', 'Password')}
          hint={t(
            'auth.signup.password_hint',
            'At least 8 characters with uppercase, lowercase, and numbers.',
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
          label={t('auth.signup.confirm_password_label', 'Confirm Password')}
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

        <AuthField
          id="discoverySource"
          label={t('auth.signup.discovery_label', 'How did you find us?')}
          hint={t('auth.signup.discovery_hint', 'Optional, but helpful for improving outreach.')}
          error={errors.discoverySource?.message}
        >
          <textarea
            id="discoverySource"
            rows={2}
            maxLength={500}
            placeholder={t(
              'auth.signup.discovery_placeholder',
              'Friend/classmate, search engine, social media, course link, etc.',
            )}
            className={`${errors.discoverySource?.message ? skin.inputError : skin.input} auth-textarea`}
            data-auth="control"
            {...register('discoverySource')}
          />
        </AuthField>

        <AuthField
          id="useCase"
          label={t('auth.signup.use_case_label', 'Use case')}
          hint={t('auth.signup.use_case_hint', 'Helps admins review signups faster.')}
          error={errors.useCase?.message}
        >
          <textarea
            id="useCase"
            rows={4}
            maxLength={2000}
            placeholder={t(
              'auth.signup.use_case_placeholder',
              'Tell us briefly what you plan to use the service for (research project, course, app prototype, etc.).',
            )}
            className={`${errors.useCase?.message ? skin.inputError : skin.input} auth-textarea`}
            data-auth="control"
            {...register('useCase')}
          />
        </AuthField>

        {turnstileSiteKey && (
          <>
            <Script
              src="https://challenges.cloudflare.com/turnstile/v0/api.js"
              strategy="afterInteractive"
            />
            <div
              className="cf-turnstile"
              data-sitekey={turnstileSiteKey}
              data-callback={TURNSTILE_CALLBACK}
            />
          </>
        )}

        <button
          type="submit"
          className={skin.submit}
          data-auth="submit"
          disabled={isLoading}
          aria-busy={isLoading}
        >
          {isLoading
            ? t('auth.signup.submitting', 'Creating your account…')
            : t('auth.signup.submit', 'Sign Up')}
        </button>
      </form>
    </AuthPageFrame>
  );
}
