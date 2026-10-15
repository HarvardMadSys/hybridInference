'use client';

/**
 * Cross-service sign-in: hand a one-time authorization code to the cloud agent.
 *
 * The gateway owns every security decision — client allowlist, exact redirect
 * match, PKCE binding, code lifetime (see the identity endpoints). This page's
 * one hard rule is that the browser navigates to `redirect_uri` only after the
 * gateway has issued a code for exactly that URI. Nothing here forwards to a
 * destination the server has not accepted, and nothing happens without the
 * user pressing the button.
 */

import { Suspense, useEffect, useMemo, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { switchAccount as accountToken } from '@/lib/api/auth';
import { createAuthorizationCode } from '@/lib/api/identity';
import { getErrorMessage } from '@/lib/utils/errors';
import { navigateTo } from '@/lib/utils/navigation';
import { userAccountLabel, userDisplayName } from '@/lib/utils/userLabel';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { useT } from '@/components/providers/useT';
import type { Translate } from '@/lib/i18n/translate';

// The one client this gateway federates identity to. The server enforces this
// independently; checking here too means an unknown client gets a plain answer
// instead of a form that fails on submit — and this page never has to render
// an attacker-chosen application name to the user.
const KNOWN_CLIENTS: Record<string, string> = {
  'cloud-agent': 'Cloud Agent',
};

interface AuthorizeRequest {
  clientId: string;
  clientLabel: string;
  redirectUri: string;
  redirectHost: string;
  codeChallenge: string;
  requestState: string | null;
}

/** Parse and vet the query, returning a request or the reason there is none. */
function parseRequest(searchParams: URLSearchParams, t: Translate): AuthorizeRequest | string {
  const clientId = searchParams.get('client_id');
  const redirectUri = searchParams.get('redirect_uri');
  const codeChallenge = searchParams.get('code_challenge');

  if (!clientId || !redirectUri || !codeChallenge) {
    return t(
      'auth.authorize.incomplete',
      'This sign-in link is incomplete. Please start again from the application.',
    );
  }
  const clientLabel = KNOWN_CLIENTS[clientId];
  if (!clientLabel) {
    return t(
      'auth.authorize.unknown_client',
      'This sign-in link is for an application this deployment does not know.',
    );
  }
  let redirectHost: string;
  try {
    redirectHost = new URL(redirectUri).host;
  } catch {
    return t(
      'auth.authorize.invalid_redirect',
      'This sign-in link has an invalid return address. Please start again from the application.',
    );
  }
  return {
    clientId,
    clientLabel,
    redirectUri,
    redirectHost,
    codeChallenge,
    requestState: searchParams.get('state'),
  };
}

function AuthorizeContent() {
  const t = useT();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { state, accounts = [], accountsReady = true } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [isContinuing, setIsContinuing] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);

  const request = useMemo(
    () => parseRequest(new URLSearchParams(searchParams.toString()), t),
    [searchParams, t],
  );
  const requestInvalid = typeof request === 'string';
  const here = `${pathname}?${searchParams.toString()}`;

  // Accounts that can be handed to the application now, without a password.
  const usable = accounts.filter((account) => account.signed_in && account.agent_access !== false);
  // OIDC's own vocabulary: the application may ask for the chooser outright,
  // or name the account it expects.
  const loginHint = searchParams.get('login_hint');
  const choosing =
    searchParams.get('prompt') === 'select_account' ||
    accounts.filter((account) => account.signed_in).length > 1 ||
    (!state.isAuthenticated && usable.length > 0) ||
    (!!loginHint && loginHint !== state.user?.id && usable.some((a) => a.id === loginHint));
  const preferred = [loginHint, state.user?.id].find(
    (id) => id && usable.some((account) => account.id === id),
  );
  const chosen = picked ?? preferred ?? usable[0]?.id ?? null;

  // No account to offer and a valid request: round-trip through login and come
  // back to this URL, query and all. Invalid requests stay here — the error is
  // more useful than a login prompt for a link that can never work.
  const needsSignIn = !state.isAuthenticated && usable.length === 0;
  useEffect(() => {
    if (state.loading || !accountsReady || !needsSignIn || requestInvalid) return;
    router.replace(`/login?next=${encodeURIComponent(here)}`);
  }, [state.loading, accountsReady, needsSignIn, requestInvalid, here, router]);

  if (requestInvalid) {
    return (
      <div className="mx-auto w-full max-w-md">
        <Card>
          <div className="text-center">
            <h1 className="text-3xl font-bold tracking-tight text-gray-900">
              {t('auth.authorize.failed_title', 'Sign-in failed')}
            </h1>
            <p className="mt-4 text-sm text-red-700" role="alert">
              {request}
            </p>
          </div>
        </Card>
      </div>
    );
  }

  if (state.loading || !accountsReady || needsSignIn) {
    return (
      <div className="flex w-full items-center justify-center">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
      </div>
    );
  }

  const accountId = choosing ? chosen : (state.user?.id ?? null);

  const onContinue = async () => {
    if (!accountId) return;
    setError(null);
    setIsContinuing(true);
    try {
      // Another account than this tab's gets a token of its own; the browser
      // stays on the account the console is using.
      const scope = {
        client_id: request.clientId,
        redirect_uri: request.redirectUri,
        code_challenge: request.codeChallenge,
        code_challenge_method: 'S256' as const,
      };
      const { code } =
        accountId === state.user?.id
          ? await createAuthorizationCode(scope)
          : await createAuthorizationCode(
              scope,
              (await accountToken(accountId, { activate: false })).access_token,
            );
      const target = new URL(request.redirectUri);
      target.searchParams.set('code', code);
      if (request.requestState !== null) {
        target.searchParams.set('state', request.requestState);
      }
      navigateTo(target.toString());
      // Deliberately still "continuing": the page is about to unload, and a
      // re-enabled button under a slow navigation invites a second code.
    } catch (err) {
      setIsContinuing(false);
      setError(getErrorMessage(err));
    }
  };

  const addAccount = (identifier?: string | null) =>
    `/login?add=1${identifier ? `&identifier=${encodeURIComponent(identifier)}` : ''}&next=${encodeURIComponent(here)}`;

  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">
            {t('auth.authorize.continue_title', 'Continue to')} {request.clientLabel}
          </h1>
          {choosing ? (
            <p className="mt-2 text-sm text-gray-600">
              {t('auth.authorize.choose_account', 'Choose the account to sign in to')}{' '}
              <span className="font-medium">{request.clientLabel}</span> ({request.redirectHost})
            </p>
          ) : (
            <p className="mt-2 text-sm text-gray-600">
              {t('auth.authorize.signin_lead', 'Sign in to')}{' '}
              <span className="font-medium">{request.clientLabel}</span> ({request.redirectHost}){' '}
              {t('auth.authorize.signin_as', 'as')}{' '}
              <span className="font-medium">{userAccountLabel(state.user)}</span>
            </p>
          )}
        </div>

        <div className="mt-8 space-y-5">
          {choosing && (
            <fieldset>
              <legend className="sr-only">{t('auth.authorize.account_legend', 'Account')}</legend>
              <ul className="divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200">
                {accounts.map((account) => {
                  const noAgent = account.signed_in && account.agent_access === false;
                  return (
                    <li key={account.id} className="flex items-center gap-3 px-4 py-3">
                      <input
                        id={`account-${account.id}`}
                        type="radio"
                        name="account"
                        value={account.id}
                        checked={chosen === account.id}
                        disabled={!account.signed_in || noAgent}
                        onChange={() => setPicked(account.id)}
                      />
                      <label htmlFor={`account-${account.id}`} className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium text-gray-900">
                          {userDisplayName(account)}
                        </span>
                        <span className="block truncate text-xs text-gray-500">
                          {userAccountLabel(account)}
                          {noAgent &&
                            ` · ${t('auth.authorize.no_agent_access', 'No access to this application')}`}
                        </span>
                      </label>
                      {!account.signed_in && (
                        <a
                          className="shrink-0 text-sm font-medium text-blue-600 hover:text-blue-700"
                          href={addAccount(account.email || account.login_name)}
                        >
                          {t('auth.authorize.sign_in_again', 'Sign in again')}
                        </a>
                      )}
                    </li>
                  );
                })}
              </ul>
            </fieldset>
          )}

          {error && (
            <div
              className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded"
              role="alert"
            >
              {error}
            </div>
          )}

          <Button
            type="button"
            className="w-full"
            isLoading={isContinuing}
            disabled={!accountId}
            onClick={onContinue}
          >
            {t('auth.authorize.continue', 'Continue')}
          </Button>

          <div className="space-y-2 text-center text-sm text-gray-600">
            {choosing && (
              <a
                className="block font-medium text-blue-600 hover:text-blue-700"
                href={addAccount()}
              >
                {t('auth.authorize.use_another', 'Use another account')}
              </a>
            )}
            <a className="block font-medium text-blue-600 hover:text-blue-700" href="/dashboard">
              {t('auth.authorize.cancel', 'Cancel and return to dashboard')}
            </a>
          </div>
        </div>
      </Card>
    </div>
  );
}

export default function AuthorizePage() {
  return (
    <Suspense
      fallback={
        <div className="flex w-full items-center justify-center">
          <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
        </div>
      }
    >
      <AuthorizeContent />
    </Suspense>
  );
}
