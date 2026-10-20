'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
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
import { NO_CONSENT, SignupConsentStep, type ConsentProgress } from './SignupConsentStep';
import { SignupSteps, type SignupStep } from './SignupSteps';
import { AuthMethodSelector } from '@/components/auth/AuthMethodSelector';
import { PasswordInput, PasswordRules } from '@/components/auth/PasswordInput';
import { PhoneAuthForm } from '@/components/auth/PhoneAuthForm';
import { SignupDetailsHeading } from '@/components/auth/SignupDetailsHeading';
import { TurnstileChallenge } from '@/components/auth/TurnstileChallenge';

/** The result's mark: a tick for an account that is ready, a clock for one in review. */
function ResultIcon({ pending }: { pending: boolean }): JSX.Element {
  return (
    <div
      aria-hidden="true"
      className={`mx-auto flex h-12 w-12 items-center justify-center rounded-full ${
        pending ? 'bg-blue-50 text-blue-600' : 'bg-emerald-50 text-emerald-600'
      }`}
    >
      <svg
        className="h-6 w-6"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {pending ? (
          <>
            <circle cx="12" cy="12" r="9" />
            <path d="M12 7v5l3 2" />
          </>
        ) : (
          <path d="m5 12.5 4.5 4.5L19 7.5" />
        )}
      </svg>
    </div>
  );
}

function Spinner(): JSX.Element {
  return (
    <svg className="mr-2 h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export default function SignupPage() {
  const t = useT();
  const skin = useAuthAppearance();
  const { branding, features } = useSiteConfig();
  const turnstileSiteKey = branding.turnstileSiteKey;
  const [step, setStep] = useState<SignupStep>('consent');
  const [accountStarted, setAccountStarted] = useState(false);
  const accountFormRef = useRef<HTMLDivElement>(null);
  const [consent, setConsent] = useState<ConsentProgress>(NO_CONSENT);
  const [method, setMethod] = useState<'email' | 'phone'>('email');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signupResult, setSignupResult] = useState<SignupResponse | null>(null);
  const turnstileTokenRef = useRef<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);
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
    watch,
    formState: { errors },
  } = useForm<SignupFormData>({
    resolver: zodResolver(runtimeSignupSchema),
    // A field reports a problem once the visitor leaves it, and from then on
    // as they fix it, rather than all at once on submit.
    mode: 'onTouched',
  });
  const password = watch('password') ?? '';

  useEffect(() => {
    if (step === 'form') {
      accountFormRef.current?.querySelector<HTMLElement>('[data-auth="control"]')?.focus();
    }
  }, [step]);

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
      turnstileTokenRef.current = null;
      setCaptchaReset((value) => value + 1);
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
          <ResultIcon pending={isPendingApproval} />
          <AuthNotice tone={isPendingApproval ? 'info' : 'ok'}>{signupResult.message}</AuthNotice>
          <Link href="/login" className={skin.submit} data-auth="submit" prefetch={false}>
            {t('auth.signup.back_to_login', 'Back to Login')}
          </Link>
        </div>
      </AuthPageFrame>
    );
  }

  const accountForm = (
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
      <SignupSteps current="form" onBack={() => setStep('consent')} />
      {features.phoneAuth && <AuthMethodSelector value={method} onChange={setMethod} />}
      {features.phoneAuth && method === 'phone' ? (
        <PhoneAuthForm purpose="signup" onSuccess={setSignupResult} />
      ) : (
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
              // The visitor just pressed Continue to reach this form.
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
              {...register('email')}
            />
          </AuthField>

          <AuthField
            id="userName"
            label={t('auth.signup.username_label', 'Nickname')}
            hint={t(
              'auth.signup.username_hint',
              'Shown in your account and admin review. This is not a login name.',
            )}
            error={errors.userName?.message}
          >
            <input
              id="userName"
              className={errors.userName?.message ? skin.inputError : skin.input}
              data-auth="control"
              type="text"
              autoComplete="nickname"
              {...register('userName')}
            />
          </AuthField>

          <AuthField
            id="password"
            label={t('auth.signup.password_label', 'Password')}
            hint={
              <PasswordRules
                value={password}
                hint={t(
                  'auth.signup.password_hint',
                  'At least 8 characters with uppercase, lowercase, and numbers.',
                )}
              />
            }
            error={errors.password?.message}
          >
            <PasswordInput
              id="password"
              className={errors.password?.message ? skin.inputError : skin.input}
              data-auth="control"
              autoComplete="new-password"
              {...register('password')}
            />
          </AuthField>

          <AuthField
            id="confirmPassword"
            label={t('auth.signup.confirm_password_label', 'Confirm Password')}
            error={errors.confirmPassword?.message}
          >
            <PasswordInput
              id="confirmPassword"
              className={errors.confirmPassword?.message ? skin.inputError : skin.input}
              data-auth="control"
              autoComplete="new-password"
              {...register('confirmPassword')}
            />
          </AuthField>

          <SignupDetailsHeading />

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
            // The widget's height, held before it loads so the button does not jump.
            <div className="min-h-[65px]">
              <TurnstileChallenge
                siteKey={turnstileSiteKey}
                onToken={(token) => {
                  turnstileTokenRef.current = token;
                }}
                resetKey={captchaReset}
              />
            </div>
          )}

          <button
            type="submit"
            className={skin.submit}
            data-auth="submit"
            disabled={isLoading}
            aria-busy={isLoading}
          >
            {isLoading && <Spinner />}
            {isLoading
              ? t('auth.signup.submitting', 'Creating your account…')
              : t('auth.signup.submit', 'Sign Up')}
          </button>
        </form>
      )}
    </AuthPageFrame>
  );

  return (
    <>
      {step === 'consent' && (
        <SignupConsentStep
          progress={consent}
          onProgress={setConsent}
          onContinue={() => {
            setAccountStarted(true);
            setStep('form');
            window.scrollTo({ top: 0 });
          }}
        />
      )}
      {/* Retain the phone draft, pending request and absolute resend deadline while reviewing. */}
      {accountStarted && (
        <div ref={accountFormRef} hidden={step !== 'form'}>
          {accountForm}
        </div>
      )}
    </>
  );
}
