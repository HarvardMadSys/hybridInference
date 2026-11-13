'use client';

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ssoStartFailure } from '@/components/auth/ssoMessages';
import { useAuth } from '@/components/providers';
import { useT, type Translate } from '@/components/providers/useT';
import { Button } from '@/components/ui/Button';
import {
  ssoErrorCode,
  ssoReauthPurpose,
  ssoRefusalMessage,
  startSsoReauth,
  type SsoErrorCode,
  type SsoProviderRef,
  type SsoReauthPurpose,
} from '@/lib/api/sso';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { navigateTo } from '@/lib/utils/navigation';

/**
 * Confirming a sensitive action through the account's identity provider.
 *
 * An account with no password and no verified phone has neither to confirm
 * with (`user.sso_reauth_provider` names where it signs in instead). It leaves
 * for the provider, signs in there again, and the provider sends the browser
 * back to the page it left with `?reauth=<purpose>`. That confirmation holds
 * once, for five minutes, in this session: the action's own request then
 * carries no password and no code.
 *
 * The page is a new document when it comes back, so whatever it must still
 * know travels two ways: the outcome in the address, read once and removed,
 * and what the visitor had typed in `sessionStorage`, which stays in this tab.
 */

/** What the visitor had typed for an action when the page left for the provider. */
interface ReauthDraft {
  purpose: SsoReauthPurpose;
  userId: string;
  value: string;
  savedAt: number;
}

const DRAFT_KEY = 'sso-reauth-draft';
/** A round trip takes minutes; a draft older than this belongs to an abandoned one. */
const DRAFT_MAX_AGE_MS = 15 * 60 * 1000;

function saveDraft(draft: ReauthDraft): void {
  try {
    window.sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // Without storage the visitor types it again.
  }
}

function readDraft(userId: string | null): ReauthDraft | null {
  if (!userId) return null;
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(DRAFT_KEY) ?? 'null') as unknown;
    if (!parsed || typeof parsed !== 'object') return null;
    const draft = parsed as Partial<ReauthDraft>;
    const purpose = ssoReauthPurpose(typeof draft.purpose === 'string' ? draft.purpose : null);
    if (!purpose || typeof draft.value !== 'string' || typeof draft.savedAt !== 'number') {
      return null;
    }
    // Another account's, or an old one: not this round trip's.
    if (draft.userId !== userId || Date.now() - draft.savedAt > DRAFT_MAX_AGE_MS) return null;
    return { purpose, userId, value: draft.value, savedAt: draft.savedAt };
  } catch {
    return null;
  }
}

function clearDraft(): void {
  try {
    window.sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    // Nothing to clear.
  }
}

/** What a page holds about a confirmation, for the actions drawn on it. */
interface SsoReauthState {
  /** The action a sign-in at the provider has confirmed, until its request uses it. */
  confirmed: SsoReauthPurpose | null;
  /** What the visitor had typed when the page left, whichever way it came back. */
  draft: { purpose: SsoReauthPurpose; value: string } | null;
  /** The confirmation holds once: the action's request has used it. */
  spend: () => void;
  /** The path the provider sends the browser back to. */
  returnTo: string;
}

const SsoReauthContext = createContext<SsoReauthState | null>(null);

export function SsoReauthProvider({
  value,
  children,
}: {
  value: SsoReauthState;
  children: ReactNode;
}) {
  return <SsoReauthContext.Provider value={value}>{children}</SsoReauthContext.Provider>;
}

/** What a round trip to the provider came back to this page with. */
export interface SsoReturn {
  reauth: SsoReauthState;
  /** Why it failed (`?sso_error`), when it did. */
  error: SsoErrorCode | null;
  /** It linked another identity to the account (`?sso_linked=1`). */
  linked: boolean;
}

const RETURN_PARAMS = ['reauth', 'sso_error', 'sso_linked'] as const;

/**
 * Read how a round trip came back, once, and take it out of the address: a
 * reload, or a link copied from the address bar, must not confirm or report
 * anything a second time.
 *
 * For a page that is only drawn for a signed-in account (`userId`), in the
 * browser: it reads `window.location` as it mounts.
 */
export function useSsoReturn(userId: string | null): SsoReturn {
  const [arrival] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const confirmed = ssoReauthPurpose(params.get('reauth'));
    const error = ssoErrorCode(params.get('sso_error'));
    // Only a round trip restores what was typed; an ordinary visit starts clean.
    const draft = confirmed || error ? readDraft(userId) : null;
    return {
      confirmed,
      error,
      linked: params.get('sso_linked') === '1',
      draft: draft ? { purpose: draft.purpose, value: draft.value } : null,
      returnTo: window.location.pathname,
    };
  });
  const [confirmed, setConfirmed] = useState(arrival.confirmed);

  useEffect(() => {
    // Whichever way the round trip ended, it is over.
    clearDraft();
    const url = new URL(window.location.href);
    if (!RETURN_PARAMS.some((name) => url.searchParams.has(name))) return;
    for (const name of RETURN_PARAMS) url.searchParams.delete(name);
    // The router follows the History API, and nothing is fetched for this. It
    // is handed no state of its own back: that is how it knows the call is the
    // page's, and takes the new address as its own.
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  }, []);

  const reauth = useMemo<SsoReauthState>(
    () => ({
      confirmed,
      draft: arrival.draft,
      spend: () => setConfirmed(null),
      returnTo: arrival.returnTo,
    }),
    [confirmed, arrival],
  );

  return { reauth, error: arrival.error, linked: arrival.linked };
}

/**
 * The path a round trip started on this page comes back to: the page's own,
 * without the query a previous round trip may have left.
 */
export function useSsoReturnPath(): string {
  const page = useContext(SsoReauthContext);
  return page?.returnTo ?? window.location.pathname;
}

/** One action's confirmation through the provider, as its form uses it. */
export interface SsoReauth {
  provider: SsoProviderRef;
  purpose: SsoReauthPurpose;
  /** The provider has confirmed this action, and its request has not used that yet. */
  confirmed: boolean;
  /** What the visitor had typed for this action before leaving, or `null`. */
  draft: string | null;
  /**
   * Call once the action's request was answered. A confirmation holds once, so
   * a request the gateway refused has used it as surely as one it accepted;
   * only a request that never arrived (`error` with no status) leaves it.
   */
  settle: (error?: unknown) => void;
  returnTo: string;
  userId: string;
}

/**
 * How the signed-in account confirms `purpose`, when it does so through its
 * identity provider; `null` for an account that has a password or a verified
 * phone, which confirms the way it always has.
 */
export function useSsoReauth(purpose: SsoReauthPurpose): SsoReauth | null {
  const { state } = useAuth();
  const page = useContext(SsoReauthContext);
  const user = state.user;
  const provider = user?.sso_reauth_provider ?? null;

  return useMemo(() => {
    if (!user || !provider) return null;
    return {
      provider,
      purpose,
      confirmed: page?.confirmed === purpose,
      draft: page?.draft?.purpose === purpose ? page.draft.value : null,
      settle: (error?: unknown) => {
        if (error !== undefined && !(error instanceof APIError && error.statusCode)) return;
        page?.spend();
      },
      returnTo: page?.returnTo ?? window.location.pathname,
      userId: user.id,
    };
  }, [user, provider, page, purpose]);
}

/**
 * Where a password or a verification code would be asked for: the button that
 * leaves for the provider, or, once it has confirmed, a line that says so.
 *
 * `draft` is read as the page leaves, so that what the visitor typed is there
 * when it comes back. Never give it a password.
 */
export function SsoReauthConfirm({
  reauth,
  draft,
  disabled = false,
}: {
  reauth: SsoReauth;
  draft?: () => string;
  disabled?: boolean;
}) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (reauth.confirmed) {
    return (
      <p
        role="status"
        className="rounded-md border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-700"
      >
        {t('settings.reauth.sso_confirmed', 'Identity confirmed. You can continue.')}
      </p>
    );
  }

  const leave = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const url = await startSsoReauth(reauth.purpose, reauth.returnTo);
      if (draft) {
        saveDraft({
          purpose: reauth.purpose,
          userId: reauth.userId,
          value: draft(),
          savedAt: Date.now(),
        });
      }
      // The page is on its way out: the button stays busy until it is gone.
      navigateTo(url);
    } catch (err) {
      const status = err instanceof APIError ? err.statusCode : undefined;
      setError(ssoStartFailure(t, status) ?? ssoRefusalMessage(err) ?? getErrorMessage(err));
      setBusy(false);
    }
  };

  return (
    <div>
      <Button
        type="button"
        variant="secondary"
        onClick={leave}
        disabled={disabled || busy}
        aria-busy={busy || undefined}
      >
        {fill(t('settings.reauth.sso_button', 'Confirm with {name}'), {
          name: reauth.provider.name,
        })}
      </Button>
      {error ? (
        <p className="mt-1.5 text-xs text-red-600" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** What a form says when it is submitted before the provider has confirmed. */
export function ssoReauthRequired(t: Translate, provider: SsoProviderRef): string {
  return fill(t('settings.reauth.sso_required', 'Confirm your identity with {name} to continue.'), {
    name: provider.name,
  });
}
