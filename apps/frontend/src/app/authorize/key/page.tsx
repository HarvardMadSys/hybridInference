'use client';

/**
 * Key authorization: an application asks for an API key on the visitor's own
 * account, and receives it by exchanging a one-time code (PKCE, S256).
 *
 * The gateway owns every decision that matters — whether this deployment offers
 * it at all, the callback allowlist, the challenge, the account's standing, the
 * key ceiling — and it builds both addresses this page can leave for:
 * `cancel_url` when it accepts the request, `redirect_url` with the code. The
 * page's own rules are few and hard: it never navigates to a callback the
 * gateway has not accepted and never builds a callback address itself, it names
 * the application only by the host the gateway returned (never by anything the
 * caller chose), and nothing happens without the visitor pressing a button.
 */

import { Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { useT } from '@/components/providers/useT';
import { AuthorizeAccountPicker } from '@/components/auth/AuthorizeAccountPicker';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { resendVerification, switchAccount as accountToken } from '@/lib/api/auth';
import {
  checkKeyAuthorization,
  createKeyAuthorizationCode,
  keyAuthorizationErrorType,
  type KeyAuthorizationRequest,
} from '@/lib/api/keyAuthorization';
import { getMe } from '@/lib/api/user';
import { rememberAuthNext } from '@/lib/utils/authNext';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { navigateTo } from '@/lib/utils/navigation';
import { userAccountLabel } from '@/lib/utils/userLabel';

/** An S256 challenge: base64url of a SHA-256 digest, without padding. */
const CHALLENGE_PATTERN = /^[A-Za-z0-9_-]{43}$/;

/** Where an account's keys are listed, and revoked. */
const API_KEYS_PAGE = '/dashboard/keys';

type ParsedRequest = KeyAuthorizationRequest | 'incomplete' | 'invalid';

/** Vet the query; the gateway checks everything again, this only spares a round trip. */
function parseRequest(searchParams: URLSearchParams): ParsedRequest {
  const callbackUrl = searchParams.get('callback_url');
  const codeChallenge = searchParams.get('code_challenge');
  const method = searchParams.get('code_challenge_method');
  if (!callbackUrl || !codeChallenge || !method) return 'incomplete';
  if (method !== 'S256' || !CHALLENGE_PATTERN.test(codeChallenge)) return 'invalid';
  if (!isAbsoluteUrl(callbackUrl)) return 'invalid';
  return {
    callback_url: callbackUrl,
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
  };
}

function isAbsoluteUrl(value: string): boolean {
  try {
    return Boolean(new URL(value).host);
  } catch {
    return false;
  }
}

/** What the gateway said about the request itself, before any account is involved. */
type Check =
  | { status: 'checking' }
  | { status: 'ready'; app: string; cancelUrl: string }
  | { status: 'refused'; reason: 'invalid' | 'not_offered' }
  | { status: 'failed'; message: string };

type Refusal = 'suspended' | 'pending' | 'inactive';

/** Whether one account may go ahead, as far as this page has learned. */
type Gate =
  | { kind: 'checking' }
  | { kind: 'ok' }
  | { kind: 'unverified'; email: string | null }
  | { kind: 'refused'; reason: Refusal };

/** A note under the consent buttons, for the account it was about. */
type Notice =
  | { accountId: string; kind: 'key_limit' }
  | { accountId: string; kind: 'error'; message: string }
  | { accountId: string; kind: 'resent'; email: string }
  | { accountId: string; kind: 'still_unverified' };

function refusalFromStatus(status: string): Refusal {
  if (status === 'suspended') return 'suspended';
  if (status === 'pending_approval') return 'pending';
  return 'inactive';
}

function refusalFromError(error: unknown): Refusal {
  const message = error instanceof Error ? error.message.toLowerCase() : '';
  if (
    (error instanceof APIError && error.code === 'ACCOUNT_SUSPENDED') ||
    message.includes('suspend')
  ) {
    return 'suspended';
  }
  if (message.includes('pending')) return 'pending';
  return 'inactive';
}

function isUnverified(error: unknown): boolean {
  return (
    (error instanceof APIError && error.code === 'EMAIL_NOT_VERIFIED') ||
    keyAuthorizationErrorType(error) === 'email_not_verified'
  );
}

function statusOf(error: unknown): number | undefined {
  return error instanceof APIError ? error.statusCode : undefined;
}

/** What a refused account check says about the account, or null when it says nothing. */
function gateFromError(error: unknown, email: string | null): Gate | null {
  if (isUnverified(error)) return { kind: 'unverified', email };
  if (statusOf(error) === 403) return { kind: 'refused', reason: refusalFromError(error) };
  return null;
}

/**
 * The standing of the tab's own account, read before Allow is offered.
 *
 * Only an answer about the account counts: the gateway busy or a dropped
 * connection says nothing about it, and Allow asks again and says what went
 * wrong. *email* names the address when the refusal does not.
 */
async function readOwnGate(email: string | null): Promise<Gate> {
  try {
    const me = await getMe();
    if (me.status && me.status !== 'active') {
      return { kind: 'refused', reason: refusalFromStatus(me.status) };
    }
    if (me.email && !me.email_verified) return { kind: 'unverified', email: me.email };
    return { kind: 'ok' };
  } catch (error) {
    return gateFromError(error, email) ?? { kind: 'ok' };
  }
}

function Spinner() {
  return (
    <div className="flex w-full items-center justify-center">
      <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
    </div>
  );
}

function ErrorCard({
  title,
  message,
  onRetry,
  retryLabel,
}: {
  title: string;
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">{title}</h1>
          <p className="mt-4 text-sm text-red-700" role="alert">
            {message}
          </p>
          {onRetry && (
            <Button type="button" variant="secondary" className="mt-6" onClick={onRetry}>
              {retryLabel}
            </Button>
          )}
        </div>
      </Card>
    </div>
  );
}

function KeyAuthorizeContent() {
  const t = useT();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { state, accounts = [], accountsReady = true } = useAuth();
  const query = searchParams.toString();
  const request = useMemo(() => parseRequest(new URLSearchParams(query)), [query]);
  const here = `${pathname}?${query}`;

  const [check, setCheck] = useState<Check>({ status: 'checking' });
  const [attempt, setAttempt] = useState(0);
  const [picked, setPicked] = useState<string | null>(null);
  const [gates, setGates] = useState<Record<string, Gate>>({});
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<'allow' | 'cancel' | 'resend' | 'recheck' | null>(null);
  // Accounts whose standing this page has already asked about, so an answer
  // arriving does not start another question.
  const asked = useRef(new Set<string>());

  const setGate = (accountId: string, gate: Gate) =>
    setGates((current) => ({ ...current, [accountId]: gate }));

  // Ask the gateway about the request before anything else: a link it refuses
  // gets a plain answer here, not a sign-in for something that can never work.
  useEffect(() => {
    if (typeof request === 'string') return;
    let stale = false;
    setCheck({ status: 'checking' });
    checkKeyAuthorization(request).then(
      (result) => {
        if (!stale) setCheck({ status: 'ready', app: result.app, cancelUrl: result.cancel_url });
      },
      (error: unknown) => {
        if (stale) return;
        const type = keyAuthorizationErrorType(error);
        const status = statusOf(error);
        if (type === 'invalid_request' || status === 400) {
          setCheck({ status: 'refused', reason: 'invalid' });
        } else if (type === 'not_configured' || status === 404) {
          setCheck({ status: 'refused', reason: 'not_offered' });
        } else {
          setCheck({ status: 'failed', message: getErrorMessage(error) });
        }
      },
    );
    return () => {
      stale = true;
    };
  }, [request, attempt]);

  const ready = check.status === 'ready';
  // Accounts that can answer now, without a password.
  const signedIn = accounts.filter((account) => account.signed_in);
  const needsSignIn = !state.isAuthenticated && signedIn.length === 0;
  const choosing = signedIn.length > 1 || (!state.isAuthenticated && signedIn.length > 0);
  const preferred = signedIn.some((account) => account.id === state.user?.id)
    ? (state.user?.id ?? null)
    : null;
  const chosen = picked ?? preferred ?? signedIn[0]?.id ?? null;
  const accountId = choosing ? chosen : (state.user?.id ?? null);
  const ownAccount = accountId !== null && accountId === state.user?.id;
  // The tab's own account is asked about before Allow is offered, so it counts
  // as being checked until it has been; another account is known only through
  // the code request itself.
  const gate: Gate | undefined = accountId
    ? (gates[accountId] ?? (ownAccount && state.isAuthenticated ? { kind: 'checking' } : undefined))
    : undefined;

  // Nobody to answer as: round-trip through login and come back to this URL,
  // query and all. Only once the gateway has accepted the request.
  useEffect(() => {
    if (!ready || state.loading || !accountsReady || !needsSignIn) return;
    router.replace(`/login?next=${encodeURIComponent(here)}`);
  }, [ready, state.loading, accountsReady, needsSignIn, here, router]);

  // The tab's own account is asked about before Allow is offered: an address
  // still to verify is said here, not after the visitor has agreed.
  const activeId = state.isAuthenticated ? (state.user?.id ?? null) : null;
  const activeEmail = state.user?.email ?? null;
  useEffect(() => {
    if (!ready || !activeId || asked.current.has(activeId)) return;
    asked.current.add(activeId);
    readOwnGate(activeEmail).then((result) =>
      setGates((current) => ({ ...current, [activeId]: result })),
    );
  }, [ready, activeId, activeEmail]);

  // An account that must verify its address leaves this page for its inbox;
  // the verification page can bring it back here.
  useEffect(() => {
    if (gate?.kind === 'unverified') rememberAuthNext(here);
  }, [gate?.kind, here]);

  if (typeof request === 'string') {
    return (
      <ErrorCard
        title={t('auth.key_authorize.failed_title', 'Cannot create an API key')}
        message={
          request === 'incomplete'
            ? t(
                'auth.key_authorize.incomplete',
                'This link is incomplete. Please start again from the application.',
              )
            : t(
                'auth.key_authorize.invalid',
                'This link is not valid. Please start again from the application.',
              )
        }
      />
    );
  }

  if (check.status === 'refused') {
    return (
      <ErrorCard
        title={t('auth.key_authorize.failed_title', 'Cannot create an API key')}
        message={
          check.reason === 'invalid'
            ? t(
                'auth.key_authorize.not_allowed',
                'This link is not valid for this deployment. Please start again from the application.',
              )
            : t(
                'auth.key_authorize.not_offered',
                'This deployment does not let applications create API keys.',
              )
        }
      />
    );
  }

  if (check.status === 'failed') {
    return (
      <ErrorCard
        title={t('auth.key_authorize.failed_title', 'Cannot create an API key')}
        message={fill(
          t('auth.key_authorize.check_failed', 'This request could not be checked: {reason}'),
          { reason: check.message },
        )}
        onRetry={() => setAttempt((value) => value + 1)}
        retryLabel={t('auth.key_authorize.try_again', 'Try again')}
      />
    );
  }

  if (check.status === 'checking' || state.loading || !accountsReady || needsSignIn) {
    return <Spinner />;
  }

  const { app, cancelUrl } = check;
  const account = accounts.find((entry) => entry.id === accountId) ?? null;
  const accountEmail = ownAccount ? (state.user?.email ?? null) : (account?.email ?? null);

  const onAllow = async () => {
    if (!accountId) return;
    const forAccount = accountId;
    setBusy('allow');
    setNotice(null);
    try {
      // Another account than this tab's gets a token of its own; the browser
      // stays on the account the console is using.
      const { redirect_url: redirectUrl } = ownAccount
        ? await createKeyAuthorizationCode(request)
        : await createKeyAuthorizationCode(
            request,
            (await accountToken(forAccount, { activate: false })).access_token,
          );
      navigateTo(redirectUrl);
      // Deliberately still busy: the page is about to unload, and a button
      // that came back under a slow navigation invites a second code.
    } catch (error) {
      setBusy(null);
      const type = keyAuthorizationErrorType(error);
      const status = statusOf(error);
      if (type === 'invalid_request' || (type === null && status === 400)) {
        // The gateway no longer accepts this request: nothing here may lead
        // to its callback any more, Cancel included.
        setCheck({ status: 'refused', reason: 'invalid' });
        return;
      }
      if (type === 'not_configured' || (type === null && status === 404)) {
        setCheck({ status: 'refused', reason: 'not_offered' });
        return;
      }
      if (type === 'key_limit_reached' || status === 409) {
        setNotice({ accountId: forAccount, kind: 'key_limit' });
        return;
      }
      const refusedGate = gateFromError(error, accountEmail);
      if (refusedGate) {
        if (refusedGate.kind === 'unverified' && gates[forAccount]?.kind === 'unverified') {
          setNotice({ accountId: forAccount, kind: 'still_unverified' });
        }
        setGate(forAccount, refusedGate);
        return;
      }
      setNotice({ accountId: forAccount, kind: 'error', message: getErrorMessage(error) });
    }
  };

  const onCancel = () => {
    setBusy('cancel');
    navigateTo(cancelUrl);
  };

  const onResend = async (email: string) => {
    if (!accountId) return;
    const forAccount = accountId;
    setBusy('resend');
    setNotice(null);
    try {
      await resendVerification(email);
      setNotice({ accountId: forAccount, kind: 'resent', email });
    } catch (error) {
      setNotice({ accountId: forAccount, kind: 'error', message: getErrorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  const onRecheck = async () => {
    if (!accountId) return;
    if (!ownAccount) {
      // Another account is only known through the code request; asking again
      // is asking for the code, which leaves for the application on success.
      await onAllow();
      return;
    }
    const forAccount = accountId;
    setBusy('recheck');
    setNotice(null);
    const result = await readOwnGate(accountEmail);
    setBusy(null);
    if (result.kind === 'unverified') {
      setNotice({ accountId: forAccount, kind: 'still_unverified' });
    }
    setGate(forAccount, result);
  };

  const addAccount = `/login?add=1&next=${encodeURIComponent(here)}`;
  const shownNotice = notice && notice.accountId === accountId ? notice : null;
  const noticeBox = shownNotice ? (
    shownNotice.kind === 'resent' ? (
      <div
        className="rounded border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800"
        role="status"
      >
        {fill(
          t('auth.key_authorize.resent', 'A new verification email is on its way to {email}.'),
          { email: shownNotice.email },
        )}
      </div>
    ) : (
      <div
        className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
        role="alert"
      >
        {shownNotice.kind === 'key_limit' ? (
          <>
            {t(
              'auth.key_authorize.key_limit',
              'This account already holds as many API keys as it may. Revoke one on the API keys page, then press Allow again.',
            )}{' '}
            <a
              className="font-medium underline"
              href={API_KEYS_PAGE}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t('auth.key_authorize.open_keys', 'Open API keys')}
            </a>
          </>
        ) : shownNotice.kind === 'still_unverified' ? (
          t(
            'auth.key_authorize.still_unverified',
            'This email address is not verified yet. Open the link in the verification email, then try again.',
          )
        ) : (
          shownNotice.message
        )}
      </div>
    )
  ) : null;

  const cancelButton = (
    <Button
      type="button"
      variant="secondary"
      className="w-full"
      isLoading={busy === 'cancel'}
      disabled={busy !== null}
      onClick={onCancel}
    >
      {t('auth.key_authorize.cancel', 'Cancel')}
    </Button>
  );

  let body: JSX.Element;
  if (gate?.kind === 'checking') {
    body = (
      <div className="flex justify-center py-4">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
      </div>
    );
  } else if (gate?.kind === 'unverified') {
    const email = gate.email ?? accountEmail;
    body = (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold text-gray-900">
          {t('auth.key_authorize.verify_title', 'Verify your email to continue')}
        </h2>
        <p className="text-sm text-gray-600">
          {email
            ? fill(
                t(
                  'auth.key_authorize.verify_body',
                  'Before an application can get an API key for this account, confirm that {email} is yours: open the link in the verification email, then come back to this page.',
                ),
                { email },
              )
            : t(
                'auth.key_authorize.verify_body_no_email',
                'Before an application can get an API key for this account, verify its email address, then come back to this page.',
              )}
        </p>
        {noticeBox}
        <Button
          type="button"
          className="w-full"
          isLoading={busy === 'recheck' || busy === 'allow'}
          disabled={busy !== null}
          onClick={onRecheck}
        >
          {t('auth.key_authorize.recheck', 'I have verified my email')}
        </Button>
        {email && (
          <Button
            type="button"
            variant="secondary"
            className="w-full"
            isLoading={busy === 'resend'}
            disabled={busy !== null}
            onClick={() => onResend(email)}
          >
            {t('auth.key_authorize.resend', 'Resend verification email')}
          </Button>
        )}
        {cancelButton}
      </div>
    );
  } else if (gate?.kind === 'refused') {
    body = (
      <div className="space-y-4">
        <div
          className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
          role="alert"
        >
          <p className="font-medium">
            {t('auth.key_authorize.refused_title', 'This account cannot create API keys')}
          </p>
          <p className="mt-1">
            {gate.reason === 'suspended'
              ? t(
                  'auth.key_authorize.refused_suspended',
                  'The account is suspended, so no application can get an API key for it.',
                )
              : gate.reason === 'pending'
                ? t(
                    'auth.key_authorize.refused_pending',
                    'The account is waiting for an administrator to approve it. Once it is approved, start again from the application.',
                  )
                : t(
                    'auth.key_authorize.refused_inactive',
                    'The account is not active, so no application can get an API key for it.',
                  )}
          </p>
        </div>
        {cancelButton}
      </div>
    );
  } else {
    body = (
      <div className="space-y-4">
        <p className="text-sm text-gray-700">
          {fill(
            t(
              'auth.key_authorize.explain_key',
              '{app} will get a new API key for this account. The application keeps the key and uses it to make requests for you; what it uses counts against this account’s usage and quota.',
            ),
            { app },
          )}
        </p>
        <p className="text-sm text-gray-600">
          {t(
            'auth.key_authorize.explain_revoke',
            'You can revoke the key at any time on your API keys page.',
          )}
        </p>
        {noticeBox}
        <Button
          type="button"
          className="w-full"
          isLoading={busy === 'allow'}
          disabled={!accountId || busy !== null}
          onClick={onAllow}
        >
          {t('auth.key_authorize.allow', 'Allow')}
        </Button>
        {cancelButton}
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">
            {fill(t('auth.key_authorize.title', 'Allow {app} to create an API key?'), { app })}
          </h1>
          {choosing ? (
            <p className="mt-2 text-sm text-gray-600">
              {fill(
                t(
                  'auth.key_authorize.choose_account',
                  'Choose the account {app} gets an API key for',
                ),
                { app },
              )}
            </p>
          ) : (
            <p className="mt-2 text-sm text-gray-600">
              {fill(t('auth.key_authorize.signed_in_as', 'Signed in as {account}'), {
                account: userAccountLabel(state.user),
              })}
            </p>
          )}
        </div>

        <div className="mt-8 space-y-5">
          {choosing && (
            <AuthorizeAccountPicker
              accounts={accounts}
              chosen={chosen}
              onPick={(id) => {
                setPicked(id);
                setNotice(null);
              }}
              signInAgainHref={(entry) => {
                const identifier = entry.email || entry.login_name;
                return `/login?add=1${identifier ? `&identifier=${encodeURIComponent(identifier)}` : ''}&next=${encodeURIComponent(here)}`;
              }}
            />
          )}

          {body}

          {(choosing || gate?.kind === 'refused' || gate?.kind === 'unverified') && (
            <div className="text-center text-sm">
              <a className="font-medium text-blue-600 hover:text-blue-700" href={addAccount}>
                {t('auth.key_authorize.use_another', 'Use another account')}
              </a>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

export default function KeyAuthorizePage() {
  return (
    <Suspense fallback={<Spinner />}>
      <KeyAuthorizeContent />
    </Suspense>
  );
}
