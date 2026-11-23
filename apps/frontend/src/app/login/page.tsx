'use client';

import { Suspense, useEffect, useMemo, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useForm, useWatch } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import toast from 'react-hot-toast';
import { createAuthSchemas, LoginFormData } from '@/lib/schemas/auth';
import { useAuth } from '@/components/providers';
import { resendVerification } from '@/lib/api/auth';
import {
  cancelSsoLink,
  confirmSsoLink,
  discoverSso,
  getPendingSsoLink,
  ssoErrorCode,
  ssoRefusalMessage,
  ssoStartUrl,
  type PendingSsoLink,
  type SsoErrorCode,
} from '@/lib/api/sso';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { usePageRestored } from '@/lib/hooks/usePageRestored';
import { internalPathOr, navigateTo } from '@/lib/utils/navigation';
import { useT } from '@/components/providers/useT';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { fill } from '@/lib/utils/interpolate';
import { AUTH_DATA, AuthField, AuthLoading, AuthNotice } from '@/components/auth/AuthForm';
import { NEUTRAL_AUTH_APPEARANCE, useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';
import { AuthMethodSelector } from '@/components/auth/AuthMethodSelector';
import { PhoneAuthForm } from '@/components/auth/PhoneAuthForm';
import { AccountChooser } from '@/components/auth/AccountChooser';
import { ssoErrorMessage, ssoOnlyAfterLink, ssoStartFailure } from '@/components/auth/ssoMessages';
import type { AccountSummary } from '@/lib/api/auth';
import { userDisplayName } from '@/lib/utils/userLabel';

/**
 * The row of alternative links under a form. The page spaces it, because a
 * distribution's form class need not space its children.
 */
const UNDER_FORM = 'mt-4 flex items-center justify-center gap-4';

/**
 * The action inside a notice, in the console's own look: a filled button, so
 * that it does not read as one more line of the notice's text.
 */
const NOTICE_BUTTON =
  'mt-3 inline-flex h-9 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-4 text-sm font-medium text-white shadow-sm transition-colors hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50';

/**
 * The identity a single sign-on left waiting for the owner of the account it
 * matched (`?sso=link`): still being read, there, or gone.
 */
type PendingLink =
  | { status: 'loading' }
  | { status: 'waiting'; identity: PendingSsoLink }
  | { status: 'gone'; reason: SsoErrorCode };

/** The name of the single sign-on that a refused password sign-in names, if it names one. */
function ssoSignInName(error: unknown): string | null {
  if (!(error instanceof APIError)) return null;
  // The API client keeps the field only when it names a provider.
  const field = error.details?.sso_sign_in as { provider_name: string } | undefined;
  return field?.provider_name ?? null;
}

function LoginContent() {
  const t = useT();
  const skin = useAuthAppearance();
  const { features, sso } = useSiteConfig();
  const router = useRouter();
  const searchParams = useSearchParams();
  // Where to land after login. Internal paths only — /authorize round-trips
  // through here with its query attached, and a crafted ?next= must never be
  // able to send a fresh session off-site.
  const nextPath = internalPathOr('/dashboard', searchParams.get('next'));
  // ?add=1 comes from "Add another account": the visitor is signed in already
  // and wants the form, not to be sent on.
  const addMode = searchParams.get('add') === '1';
  // How a round trip to an identity provider came back, when this is one. They
  // are states of this page rather than pages of their own, so that a
  // distribution's sign-in frame draws them too.
  //   ?sso=complete   the gateway signed the account in on the redirect
  //   ?sso=link       an account already has this email: its owner signs in
  //                   the usual way, then accepts the identity
  //   ?sso_error=…    it failed, and this is why
  const ssoReturn = searchParams.get('sso');
  const returnedError = ssoErrorCode(searchParams.get('sso_error'));
  // A visitor who is signed in is sent on at once -- except one a round trip
  // brought here, who has something to read or to answer first.
  const heldHere = ssoReturn === 'complete' || ssoReturn === 'link' || returnedError !== null;
  const {
    login,
    loginPhone,
    completeExternalSignIn,
    state,
    accounts = [],
    switchAccount,
    removeAccount,
    refreshUser,
  } = useAuth();
  // Null until the visitor picks: the chooser when this browser has accounts.
  const [view, setView] = useState<'chooser' | 'form' | null>(null);
  const [method, setMethod] = useState<'email' | 'phone'>('email');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Email of an unverified account that just failed to log in. When set, we
  // surface a "resend verification email" action so the user isn't dead-ended.
  const [unverifiedEmail, setUnverifiedEmail] = useState<string | null>(null);
  // A password that was refused for an address of an organization: the name of
  // its single sign-on, and the address that was typed, to sign in there.
  const [ssoRequired, setSsoRequired] = useState<{ name: string; email: string } | null>(null);
  const [isResending, setIsResending] = useState(false);
  const [resendDone, setResendDone] = useState(false);
  // The single sign-on email step, which takes the form's place while open.
  const [ssoStep, setSsoStep] = useState(false);
  const [ssoEmail, setSsoEmail] = useState('');
  const [ssoEmailError, setSsoEmailError] = useState<string | undefined>();
  const [ssoBusy, setSsoBusy] = useState(false);
  usePageRestored(() => setSsoBusy(false));
  const [ssoNotice, setSsoNotice] = useState<{ tone: 'info' | 'error'; text: string } | null>(null);
  // ?sso=complete: the session is being taken up, or could not be.
  const [completion, setCompletion] = useState<'running' | 'failed' | null>(
    ssoReturn === 'complete' ? 'running' : null,
  );
  // ?sso=link: the identity waiting to be linked.
  const [pendingLink, setPendingLink] = useState<PendingLink | null>(
    ssoReturn === 'link' ? { status: 'loading' } : null,
  );
  // The visitor signed in on this page while that identity was waiting.
  const [signedInForLink, setSignedInForLink] = useState(false);
  const [linkBusy, setLinkBusy] = useState<'confirm' | 'cancel' | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [linkExpired, setLinkExpired] = useState(false);
  // Validation copy is interface copy too, so the schemas are rebuilt whenever
  // the resolved translator changes; `t` is stable per content document.
  const { loginSchema, emailSchema } = useMemo(() => createAuthSchemas(t), [t]);

  const {
    register,
    handleSubmit,
    setValue,
    getValues,
    control,
    formState: { errors },
  } = useForm<LoginFormData>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: searchParams.get('identifier') ?? '' },
  });
  const typedEmail = useWatch({ control, name: 'email' });

  const waiting = pendingLink?.status === 'waiting' ? pendingLink.identity : null;
  // The account this browser is using already carries the email the identity
  // was matched by: its owner is here, and only has to say yes.
  const activeEmail = state.isAuthenticated ? state.user?.email : null;
  const ownerIsHere =
    waiting !== null && !!activeEmail && activeEmail.toLowerCase() === waiting.email.toLowerCase();
  const showLinkPanel =
    waiting !== null && state.isAuthenticated && (ownerIsHere || signedInForLink);
  // With an identity waiting, the form comes first: it is what the notice asks for.
  const showChooser =
    !addMode && !ssoStep && accounts.length > 0 && (waiting ? view === 'chooser' : view !== 'form');

  useEffect(() => {
    if (!state.loading && state.isAuthenticated && !addMode && !heldHere) {
      router.replace(nextPath);
    }
  }, [state.loading, state.isAuthenticated, addMode, heldHere, router, nextPath]);

  // The redirect that brought the browser back set the session's cookie. The
  // tab takes that session up first, and only then is the visitor sent on.
  useEffect(() => {
    if (completion !== 'running') return;
    let stale = false;
    completeExternalSignIn().then(
      () => {
        if (!stale) router.replace(nextPath);
      },
      () => {
        if (!stale) setCompletion('failed');
      },
    );
    return () => {
      stale = true;
    };
  }, [completion, completeExternalSignIn, router, nextPath]);

  // Read from the browser's cookie, so the email is never in the address.
  useEffect(() => {
    if (ssoReturn !== 'link') return;
    let stale = false;
    getPendingSsoLink().then(
      (identity) => {
        if (stale) return;
        if (!identity) {
          setPendingLink({ status: 'gone', reason: 'expired' });
          return;
        }
        setPendingLink({ status: 'waiting', identity });
        setValue('email', identity.email);
      },
      () => {
        if (!stale) setPendingLink({ status: 'gone', reason: 'failed' });
      },
    );
    return () => {
      stale = true;
    };
  }, [ssoReturn, setValue]);

  // Where a sign-in on this page leads: on to where the visitor was going, or,
  // with an identity waiting, to the question of linking it.
  const afterSignIn = () => {
    if (waiting) setSignedInForLink(true);
    else router.push(nextPath);
  };

  const chooseAccount = async (account: AccountSummary) => {
    await switchAccount(account.id);
    toast.success(
      fill(t('auth.accounts.switched_toast', 'Continuing as {name}'), {
        name: userDisplayName(account),
      }),
    );
    afterSignIn();
  };

  // An account whose session ended signs in again with its own identifier --
  // or, when it has no password and signs in through an identity provider, there.
  const signInAgain = (account: AccountSummary) => {
    if (account.sso_provider) {
      navigateTo(ssoStartUrl(account.sso_provider.id, nextPath));
      return;
    }
    const identifier = account.email || account.login_name;
    if (identifier) {
      setValue('email', identifier);
      setMethod('email');
    } else if (account.phone) {
      setMethod('phone');
    }
    setView('form');
  };

  const onSubmit = async (data: LoginFormData) => {
    setIsLoading(true);
    setError(null);
    setUnverifiedEmail(null);
    setSsoRequired(null);
    setResendDone(false);

    try {
      await login(data.email, data.password);
      toast.success(t('auth.login.success_toast', 'Login successful!'));
      afterSignIn();
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
      // The gateway says so for the domain of the address, whatever account
      // has it, so the line tells nothing that the address does not.
      // Not while an identity waits for its owner: that account still has its
      // password, and the round trip to the provider is already made.
      const provider = waiting ? null : ssoSignInName(err);
      if (provider) setSsoRequired({ name: provider, email: data.email.trim() });
      // Only an email account can be unverified; a login name has no inbox.
      if (
        err instanceof APIError &&
        err.code === 'EMAIL_NOT_VERIFIED' &&
        data.email.includes('@')
      ) {
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

  const openSsoStep = () => {
    // An address already typed into the form is the likely one.
    const typed = getValues('email').trim();
    if (typed.includes('@')) setSsoEmail(typed);
    setSsoEmailError(undefined);
    setSsoNotice(null);
    setSsoStep(true);
  };

  const submitSso = async (event: FormEvent) => {
    event.preventDefault();
    if (ssoBusy) return;
    const email = ssoEmail.trim();
    const parsed = emailSchema.safeParse(email);
    if (!parsed.success) {
      setSsoEmailError(parsed.error.issues[0]?.message);
      return;
    }
    setSsoEmailError(undefined);
    await startSso(email);
  };

  // The line is about the address that was refused. Once the field holds
  // another one, or another account is picked, it is not said of that one.
  const ssoHint =
    ssoRequired && (typedEmail ?? '').trim() === ssoRequired.email ? ssoRequired : null;

  // From the refused password: the address is the one that was typed, so it is
  // not asked for again. What goes wrong is said on the single sign-on step,
  // where the address can be changed.
  const signInThroughSso = async () => {
    if (ssoBusy || !ssoHint) return;
    setSsoEmail(ssoHint.email);
    setSsoEmailError(undefined);
    if (!(await startSso(ssoHint.email))) setSsoStep(true);
  };

  // The gateway finds the provider by the email's domain and answers with its
  // address. No account is looked up, so the answer says nothing about one.
  // False when the page stays: the notice then says why.
  const startSso = async (email: string): Promise<boolean> => {
    setSsoNotice(null);
    setSsoBusy(true);
    try {
      const url = await discoverSso(email, nextPath);
      if (url) {
        // The page is on its way out: the button stays busy until it is gone.
        navigateTo(url);
        return true;
      }
      setSsoNotice({
        tone: 'info',
        text: t(
          'auth.sso.not_available',
          'Your organization has not set up single sign-on here. Use another sign-in method.',
        ),
      });
    } catch (err) {
      const status = err instanceof APIError ? err.statusCode : undefined;
      setSsoNotice({ tone: 'error', text: ssoStartFailure(t, status) ?? getErrorMessage(err) });
    }
    setSsoBusy(false);
    return false;
  };

  const confirmLink = async () => {
    setLinkBusy('confirm');
    setLinkError(null);
    try {
      await confirmSsoLink();
      // The link can change what the account is: a member of an organization has
      // no password. The pages after this one read that from the profile.
      await refreshUser();
      toast.success(t('auth.sso.linked_toast', 'Single sign-on connected.'));
      // Replaced, not pushed: going back to the question would find nothing
      // waiting any more and say the sign-in took too long.
      router.replace(nextPath);
    } catch (err) {
      if (err instanceof APIError && err.statusCode === 404) {
        // Nothing is waiting any more: the identity was kept for ten minutes.
        setLinkExpired(true);
        setLinkError(ssoErrorMessage(t, 'expired'));
      } else {
        setLinkError(ssoRefusalMessage(err) ?? getErrorMessage(err));
      }
      setLinkBusy(null);
    }
  };

  const declineLink = async () => {
    setLinkBusy('cancel');
    try {
      await cancelSsoLink();
    } catch {
      // Left alone, the waiting identity expires by itself.
    }
    router.replace(nextPath);
  };

  if (
    state.loading ||
    pendingLink?.status === 'loading' ||
    (state.isAuthenticated && !addMode && !heldHere)
  ) {
    return <AuthLoading />;
  }

  const kicker = t('auth.login.kicker', 'GOOD TO SEE YOU AGAIN');
  const legal = (
    <>
      {t('auth.legal.see', 'See')} <Link href="/terms">{t('chrome.footer.terms', 'Terms')}</Link>
      {' · '}
      <Link href="/terms#terms-s5">{t('chrome.footer.privacy', 'Privacy')}</Link>
    </>
  );

  if (completion === 'running') {
    return (
      <AuthPageFrame
        page="login"
        kicker={kicker}
        title={t('auth.login.title', 'Log In')}
        subtitle={t('auth.sso.completing', 'Signing you in…')}
        legal={legal}
      >
        <div className={skin.form} data-auth={AUTH_DATA.form}>
          <AuthLoading />
        </div>
      </AuthPageFrame>
    );
  }

  if (waiting && showLinkPanel) {
    return (
      <AuthPageFrame
        page="login"
        kicker={kicker}
        title={fill(t('auth.sso.link_title', 'Connect your {name} account?'), {
          name: waiting.provider_name,
        })}
        subtitle={fill(
          t(
            'auth.sso.link_body',
            '{email} will be able to sign in to this account through {name}.',
          ),
          { email: waiting.email, name: waiting.provider_name },
        )}
        legal={legal}
      >
        <div className={skin.form} data-auth={AUTH_DATA.form}>
          {linkError && <AuthNotice tone="error">{linkError}</AuthNotice>}
          {linkExpired ? (
            <button
              type="button"
              className={skin.submit}
              data-auth={AUTH_DATA.submit}
              onClick={() => router.push(nextPath)}
            >
              {t('auth.sso.continue', 'Continue')}
            </button>
          ) : (
            <>
              <AuthNotice>
                {t(
                  'auth.sso.link_keys_hint',
                  'If you did not create this account yourself, review its API keys after connecting.',
                )}
              </AuthNotice>
              {waiting.sso_only_after_link && !state.user?.is_admin ? (
                // Said before the button: the account is the organization's from then on.
                // The gateway answers for the provider alone; a platform administrator
                // keeps its password, so it is not told that it loses it.
                <AuthNotice>{ssoOnlyAfterLink(t)}</AuthNotice>
              ) : null}
              <button
                type="button"
                className={skin.submit}
                data-auth={AUTH_DATA.submit}
                disabled={linkBusy !== null}
                aria-busy={linkBusy === 'confirm'}
                onClick={confirmLink}
              >
                {t('auth.sso.link_confirm', 'Connect')}
              </button>
            </>
          )}
        </div>
        {!linkExpired && (
          <div className={UNDER_FORM} data-auth={AUTH_DATA.secondaryActions}>
            <button
              type="button"
              className={skin.linkButton}
              disabled={linkBusy !== null}
              aria-busy={linkBusy === 'cancel'}
              onClick={declineLink}
            >
              {t('auth.sso.link_cancel', 'Not now')}
            </button>
          </div>
        )}
      </AuthPageFrame>
    );
  }

  // The field wraps its action in the `field-action` hook itself.
  const forgotLink = (
    <Link href="/forgot-password" className={skin.linkButton} prefetch={false}>
      {t('auth.login.forgot_password', 'Forgot password?')}
    </Link>
  );

  // Why the round trip that led here ended on this form.
  const returnFailure: SsoErrorCode | null =
    completion === 'failed'
      ? 'failed'
      : pendingLink?.status === 'gone'
        ? pendingLink.reason
        : returnedError;
  // The entry is one, whichever provider answers: the page lists no organization.
  // Not beside an identity that is waiting, which a second round trip would replace.
  const offerSso = sso.enabled && !showChooser && !ssoStep && !waiting;
  const isNeutral = skin === NEUTRAL_AUTH_APPEARANCE;
  const divider = t('auth.sso.divider', 'or');

  return (
    <AuthPageFrame
      page="login"
      kicker={kicker}
      title={
        showChooser
          ? t('auth.accounts.title', 'Choose an account')
          : addMode
            ? t('auth.accounts.add_title', 'Add another account')
            : t('auth.login.title', 'Log In')
      }
      subtitle={
        ssoStep
          ? t(
              'auth.sso.subtitle',
              'Enter your work or school email. We will send you to your organization to sign in.',
            )
          : showChooser
            ? t('auth.accounts.subtitle', 'Accounts signed in on this browser')
            : features.phoneAuth
              ? t('auth.phone.login_subtitle', 'Welcome back, please log in')
              : t('auth.login.subtitle', 'Welcome back, please log in')
      }
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
      legal={legal}
    >
      {/* Above the chooser and both forms, whichever the visitor is on. */}
      {!ssoStep && (returnFailure || waiting) ? (
        <div className="mt-6 space-y-3">
          {returnFailure && (
            <AuthNotice tone="error">{ssoErrorMessage(t, returnFailure)}</AuthNotice>
          )}
          {waiting && (
            <AuthNotice>
              {fill(
                t(
                  'auth.sso.link_notice',
                  'An account already exists for this email. Sign in the way you usually do to connect your {name} account.',
                ),
                { name: waiting.provider_name },
              )}
            </AuthNotice>
          )}
        </div>
      ) : null}
      {ssoStep ? null : showChooser ? (
        <AccountChooser
          accounts={accounts}
          onChoose={chooseAccount}
          onSignIn={signInAgain}
          onRemove={(account) => removeAccount(account.id)}
          onUseAnother={() => {
            setValue('email', '');
            setView('form');
          }}
        />
      ) : (
        <>
          {features.phoneAuth && (
            <AuthMethodSelector purpose="login" value={method} onChange={setMethod} />
          )}
          {addMode ? (
            <button type="button" className={skin.linkButton} onClick={() => router.push(nextPath)}>
              {t('auth.accounts.cancel_add', 'Cancel')}
            </button>
          ) : accounts.length > 0 ? (
            <button type="button" className={skin.linkButton} onClick={() => setView('chooser')}>
              {t('auth.accounts.back', 'Back to your accounts')}
            </button>
          ) : null}
        </>
      )}
      {ssoStep ? (
        <>
          <form onSubmit={submitSso} className={skin.form} data-auth={AUTH_DATA.form} noValidate>
            {ssoNotice && <AuthNotice tone={ssoNotice.tone}>{ssoNotice.text}</AuthNotice>}
            <AuthField
              id="sso-email"
              label={t('auth.sso.email_label', 'Work or school email')}
              error={ssoEmailError}
            >
              <input
                id="sso-email"
                className={ssoEmailError ? skin.inputError : skin.input}
                data-auth="control"
                type="email"
                autoComplete="email"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                value={ssoEmail}
                onChange={(event) => {
                  setSsoEmail(event.target.value);
                  setSsoEmailError(undefined);
                }}
                disabled={ssoBusy}
              />
            </AuthField>
            <button
              type="submit"
              className={skin.submit}
              data-auth={AUTH_DATA.submit}
              disabled={ssoBusy}
              aria-busy={ssoBusy}
            >
              {t('auth.sso.continue', 'Continue')}
            </button>
          </form>
          <div className={UNDER_FORM} data-auth={AUTH_DATA.secondaryActions}>
            <button
              type="button"
              className={skin.linkButton}
              disabled={ssoBusy}
              onClick={() => setSsoStep(false)}
            >
              {t('auth.sso.back', 'Back to log in')}
            </button>
          </div>
        </>
      ) : showChooser ? null : features.phoneAuth && method === 'phone' ? (
        <PhoneAuthForm
          purpose="login"
          onLogin={async (phone, code) => {
            await loginPhone(phone, code);
            toast.success(t('auth.login.success_toast', 'Login successful!'));
            afterSignIn();
          }}
        />
      ) : (
        <form onSubmit={handleSubmit(onSubmit)} className={skin.form} data-auth="form">
          {error && <AuthNotice tone="error">{error}</AuthNotice>}

          {ssoHint && (
            <AuthNotice>
              <p>
                {fill(
                  t(
                    'auth.sso.must_sign_in',
                    'Accounts of {name} must sign in through single sign-on.',
                  ),
                  { name: ssoHint.name },
                )}
              </p>
              <button
                type="button"
                // A module's look has one button, the form's own; the console's
                // look has a second one that fits inside a notice.
                className={isNeutral ? NOTICE_BUTTON : `${skin.submit} mt-3`}
                onClick={() => void signInThroughSso()}
                disabled={ssoBusy}
                aria-busy={ssoBusy}
              >
                {t('auth.sso.button', 'Log in with SSO')}
                <span aria-hidden="true">→</span>
              </button>
            </AuthNotice>
          )}

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

          {/* Email or login name: the first-run administrator has a login name and
            no email, so this is a text field and the schema decides which rule
            applies. The request still sends it as `email`. */}
          <AuthField
            id="email"
            label={t('auth.login.email_label', 'Email or login name')}
            error={errors.email?.message}
          >
            <input
              id="email"
              className={errors.email?.message ? skin.inputError : skin.input}
              data-auth="control"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
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
            {isLoading
              ? t('auth.login.submitting', 'Logging in…')
              : t('auth.login.submit', 'Log In')}
          </button>
        </form>
      )}
      {offerSso ? (
        <div className="mt-6 flex flex-col items-center gap-3" data-auth={AUTH_DATA.sso}>
          {isNeutral ? (
            <div className="flex w-full items-center gap-3 text-xs text-gray-500">
              <span className="h-px flex-1 bg-gray-200" aria-hidden="true" />
              <span>{divider}</span>
              <span className="h-px flex-1 bg-gray-200" aria-hidden="true" />
            </div>
          ) : (
            // A distribution's own small print: its colors are not the page's to pick.
            <span className={skin.hint}>{divider}</span>
          )}
          <button type="button" className={skin.linkButton} onClick={openSsoStep}>
            {t('auth.sso.button', 'Log in with SSO')}
          </button>
        </div>
      ) : null}
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
