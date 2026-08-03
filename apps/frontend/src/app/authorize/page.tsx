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
import { createAuthorizationCode } from '@/lib/api/identity';
import { getErrorMessage } from '@/lib/utils/errors';
import { navigateTo } from '@/lib/utils/navigation';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';

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
function parseRequest(searchParams: URLSearchParams): AuthorizeRequest | string {
  const clientId = searchParams.get('client_id');
  const redirectUri = searchParams.get('redirect_uri');
  const codeChallenge = searchParams.get('code_challenge');

  if (!clientId || !redirectUri || !codeChallenge) {
    return 'This sign-in link is incomplete. Please start again from the application.';
  }
  const clientLabel = KNOWN_CLIENTS[clientId];
  if (!clientLabel) {
    return 'This sign-in link is for an application this deployment does not know.';
  }
  let redirectHost: string;
  try {
    redirectHost = new URL(redirectUri).host;
  } catch {
    return 'This sign-in link has an invalid return address. Please start again from the application.';
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
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { state } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [isContinuing, setIsContinuing] = useState(false);

  const request = useMemo(
    () => parseRequest(new URLSearchParams(searchParams.toString())),
    [searchParams],
  );
  const requestInvalid = typeof request === 'string';

  // Unauthenticated with a valid request: round-trip through login and come
  // back to this URL, query and all. Invalid requests stay here — the error is
  // more useful than a login prompt for a link that can never work.
  useEffect(() => {
    if (state.loading || state.isAuthenticated || requestInvalid) return;
    const here = `${pathname}?${searchParams.toString()}`;
    router.replace(`/login?next=${encodeURIComponent(here)}`);
  }, [state.loading, state.isAuthenticated, requestInvalid, pathname, searchParams, router]);

  if (requestInvalid) {
    return (
      <div className="mx-auto w-full max-w-md">
        <Card>
          <div className="text-center">
            <h1 className="text-3xl font-bold tracking-tight text-gray-900">Sign-in failed</h1>
            <p className="mt-4 text-sm text-red-700" role="alert">
              {request}
            </p>
          </div>
        </Card>
      </div>
    );
  }

  if (state.loading || !state.isAuthenticated) {
    return (
      <div className="flex w-full items-center justify-center">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
      </div>
    );
  }

  const onContinue = async () => {
    setError(null);
    setIsContinuing(true);
    try {
      const { code } = await createAuthorizationCode({
        client_id: request.clientId,
        redirect_uri: request.redirectUri,
        code_challenge: request.codeChallenge,
        code_challenge_method: 'S256',
      });
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

  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">
            Continue to {request.clientLabel}
          </h1>
          <p className="mt-2 text-sm text-gray-600">
            Sign in to <span className="font-medium">{request.clientLabel}</span> (
            {request.redirectHost}) as <span className="font-medium">{state.user?.email}</span>
          </p>
        </div>

        <div className="mt-8 space-y-5">
          {error && (
            <div
              className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded"
              role="alert"
            >
              {error}
            </div>
          )}

          <Button type="button" className="w-full" isLoading={isContinuing} onClick={onContinue}>
            Continue
          </Button>

          <div className="text-center text-sm text-gray-600">
            <a className="font-medium text-blue-600 hover:text-blue-700" href="/dashboard">
              Cancel and return to dashboard
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
