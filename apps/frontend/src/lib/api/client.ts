// Centralized HTTP client with auth handling and token refresh.

import { APIError, ERROR_MESSAGES, httpStatusToErrorCode } from '@/lib/utils/errors';

let accessToken: string | null = null;
let refreshPromise: Promise<boolean> | null = null;
// Set once a refresh proves the browser's session now belongs to another
// account; cleared only by a sign-in in this tab. See refreshAccessToken.
let accountChanged = false;

// These events are ephemeral within one loaded console bundle, not persisted
// distribution state. Keeping a stable neutral name lets storageKeyPrefix be
// supplied entirely by runtime branding without coupling the API client to a
// build-time value.
export const AUTH_EXPIRED_EVENT = 'hybridinference:auth-expired';
export const ACCOUNT_CHANGED_EVENT = 'hybridinference:account-changed';

/** Web Lock every tab of this origin takes around a request that rotates the session cookie. */
const SESSION_LOCK = 'hybridinference:auth-refresh';
/** A refresh holds that lock for every tab, so a hung one must not hold it forever. */
const REFRESH_TIMEOUT_MS = 20_000;

// Wrap fetch so a genuine connectivity failure (DNS, offline, connection
// refused/reset) — which rejects with a TypeError before any response exists —
// surfaces as a NETWORK_ERROR APIError instead of a raw, unmapped TypeError.
// This is the ONLY path that should produce NETWORK_ERROR; once a response is
// received the connection is fine and errors are classified by status instead.
export async function safeFetch(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (err) {
    if (err instanceof APIError) throw err;
    // An intentional abort (AbortController on unmount, debounced search, etc.)
    // isn't a connectivity failure — let it propagate so callers can ignore it
    // rather than surface "check your connection".
    if (err instanceof Error && err.name === 'AbortError') throw err;
    const message = err instanceof Error ? err.message : 'Network request failed';
    throw new APIError('NETWORK_ERROR', message);
  }
}

function notifyAuthExpired(): void {
  setAccessToken(null);
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
}

/** Why a tab's account stopped being the browser's: another account, or none. */
export type AccountChangeReason = 'switched' | 'signed-out';

/**
 * Another tab moved the browser to a different account, or signed this one out.
 *
 * This tab still shows the previous account, so it sends nothing until it is
 * reloaded: a request made now would act as the new account on a page built
 * for the old one. The token is dropped so the reload starts from the cookie.
 * Dispatches `ACCOUNT_CHANGED_EVENT` with the reason as its `detail`.
 */
export function markAccountChanged(reason: AccountChangeReason): void {
  accountChanged = true;
  accessToken = null;
  sessionStorage.removeItem('access_token');
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(ACCOUNT_CHANGED_EVENT, { detail: reason }));
  }
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  if (token) {
    // Only a sign-in in this tab hands a token in from outside; the account
    // this tab now shows is the one it chose.
    accountChanged = false;
    sessionStorage.setItem('access_token', token);
  } else {
    sessionStorage.removeItem('access_token');
  }
}

export function getAccessToken(): string | null {
  if (accessToken) return accessToken;
  const cached = sessionStorage.getItem('access_token');
  accessToken = cached;
  return accessToken;
}

function tokenPayload(token: string | null): Record<string, unknown> | null {
  if (!token) return null;
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
    return payload && typeof payload === 'object' ? payload : null;
  } catch {
    return null;
  }
}

function isAccessTokenExpired(token: string | null): boolean {
  const exp = tokenPayload(token)?.exp;
  if (typeof exp !== 'number') return true;
  // 30s skew to avoid races with server clock
  return exp * 1000 < Date.now() + 30_000;
}

/** The account a token was issued to, or null when it names none. */
function tokenSubject(token: string | null): string | null {
  const sub = tokenPayload(token)?.sub;
  return typeof sub === 'string' && sub ? sub : null;
}

/**
 * Run a request that rotates the session cookie with every other tab's held off.
 *
 * A refresh, a sign-in and an account switch each replace the shared cookie
 * and retire the value they were sent with. Two tabs doing so at once both send
 * the same value, and the one that lands second is refused and signed out.
 * Taking a lock across the origin makes the second wait until the first
 * response has replaced the cookie, so it sends the new one. Without Web Locks
 * the request runs as before. Not reentrant: `run` must not refresh.
 */
export async function withSessionLock<T>(run: () => Promise<T>): Promise<T> {
  if (typeof navigator === 'undefined' || !navigator.locks) return run();
  return await navigator.locks.request(SESSION_LOCK, run);
}

async function refreshAccessToken(apiBase: string): Promise<boolean> {
  if (accountChanged) return false;
  // If refresh is already in progress, wait for it to prevent race conditions
  if (refreshPromise) {
    return refreshPromise;
  }

  refreshPromise = (async () => {
    try {
      return await withSessionLock(async () => {
        // The account this tab is showing, read before the refresh can replace it.
        const shownSubject = tokenSubject(getAccessToken());
        const resp = await safeFetch(`${apiBase}/auth/refresh`, {
          method: 'POST',
          credentials: 'include',
          signal: AbortSignal.timeout(REFRESH_TIMEOUT_MS),
        });
        if (!resp.ok) {
          notifyAuthExpired();
          return false;
        }
        const data = await resp.json();
        const token = data?.access_token as string | undefined;
        if (!token) {
          notifyAuthExpired();
          return false;
        }
        // The refresh cookie is the browser's, not this tab's: after a sign-in
        // elsewhere it belongs to another account, and installing its token
        // would quietly turn this page into that account's.
        const refreshedSubject = tokenSubject(token);
        if (shownSubject && refreshedSubject && shownSubject !== refreshedSubject) {
          markAccountChanged('switched');
          return false;
        }
        accessToken = token;
        sessionStorage.setItem('access_token', token);
        return true;
      });
    } finally {
      // Clear the promise so future refreshes can proceed
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

function accountChangedError(): APIError {
  return new APIError('ACCOUNT_CHANGED', ERROR_MESSAGES.ACCOUNT_CHANGED);
}

export async function fetchWithAuth(
  apiBase: string,
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  if (accountChanged) throw accountChangedError();
  let token = getAccessToken();
  if (isAccessTokenExpired(token)) {
    await refreshAccessToken(apiBase);
    // Sending without a token now would earn a 401 whose retry runs as the
    // other account, the very switch the refresh just refused.
    if (accountChanged) throw accountChangedError();
    token = getAccessToken();
  }

  const headers = new Headers(init.headers || {});
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const resp = await safeFetch(`${apiBase}${input}`, {
    ...init,
    headers,
    credentials: 'include',
  });

  if (resp.status !== 401) return resp;

  const refreshed = await refreshAccessToken(apiBase);
  if (!refreshed) {
    if (accountChanged) throw accountChangedError();
    notifyAuthExpired();
    return resp;
  }

  const headersRetry = new Headers(init.headers || {});
  const newToken = getAccessToken();
  if (newToken) headersRetry.set('Authorization', `Bearer ${newToken}`);

  return safeFetch(`${apiBase}${input}`, {
    ...init,
    headers: headersRetry,
    credentials: 'include',
  });
}

export async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (resp.ok) return resp.json() as Promise<T>;

  let errorData: unknown = null;
  try {
    errorData = await resp.json();
  } catch {
    // Body isn't JSON — typically an edge proxy / load balancer HTML error page,
    // not a connectivity failure (the response arrived). Classify by status so
    // the user sees a timeout / unavailable / server-error message instead of a
    // misleading "check your connection".
    throw new APIError(
      httpStatusToErrorCode(resp.status),
      `HTTP ${resp.status}: ${resp.statusText}`,
      resp.status,
    );
  }

  // Handle backend error format: { error: { type: "...", message: "...", code: 500 } }
  // or { detail: "..." } (FastAPI validation errors)
  let errorCode = 'UNKNOWN_ERROR';
  let errorMessage = '';
  let errorDetails: Record<string, unknown> | undefined;

  if (errorData && typeof errorData === 'object') {
    const data = errorData as Record<string, unknown>;

    if (data.error && typeof data.error === 'object') {
      // Backend error format: { error: { message: "...", type: "...", code: 400 } }
      const error = data.error as Record<string, unknown>;
      errorMessage = (error.message as string) || errorMessage;
      const lowerMessage = errorMessage.toLowerCase();

      // Extract error patterns from message
      if (
        lowerMessage.includes('reset link has already been used') ||
        (lowerMessage.includes('reset token') && lowerMessage.includes('used'))
      ) {
        errorCode = 'RESET_TOKEN_USED';
      } else if (lowerMessage.includes('reset link has expired')) {
        errorCode = 'RESET_TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid or expired reset token')) {
        errorCode = 'RESET_TOKEN_INVALID';
      } else if (lowerMessage.includes('already been used')) {
        errorCode = 'TOKEN_ALREADY_USED';
      } else if (lowerMessage.includes('expired')) {
        errorCode = 'TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid') && lowerMessage.includes('token')) {
        errorCode = 'INVALID_TOKEN';
      } else if (error.type === 'validation_error') {
        errorCode = 'VALIDATION_ERROR';
      } else if (resp.status === 409) {
        errorCode = 'USER_ALREADY_EXISTS';
      } else if (resp.status === 401) {
        errorCode = 'INVALID_CREDENTIALS';
      } else if (
        resp.status === 403 &&
        ((lowerMessage.includes('email') && lowerMessage.includes('not verified')) ||
          lowerMessage.includes('verify your email'))
      ) {
        // Only a genuine unverified-email 403 should drive the login page's
        // "resend verification" flow (mirrors the { detail } branch below).
        // Other typed 403s — suspended account, insufficient permissions,
        // plan/quota forbidden — must NOT be mislabeled as EMAIL_NOT_VERIFIED;
        // they fall through so their real message is surfaced verbatim.
        errorCode = 'EMAIL_NOT_VERIFIED';
      } else if (resp.status === 403 && lowerMessage.includes('suspend')) {
        errorCode = 'ACCOUNT_SUSPENDED';
      }
    } else if (data.detail) {
      // FastAPI validation error format: { detail: "..." }
      errorMessage = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      const lowerMessage = errorMessage.toLowerCase();

      // Try to extract more specific error from detail message
      if (lowerMessage.includes('already registered')) {
        errorCode = 'USER_ALREADY_EXISTS';
      } else if (
        (lowerMessage.includes('email') && lowerMessage.includes('not verified')) ||
        lowerMessage.includes('verify your email')
      ) {
        // Login raises a plain HTTPException ({ detail: "Email not verified..." }),
        // not the typed { error: {...} } shape, so match on the message here so the
        // login page can offer a "resend verification email" action.
        errorCode = 'EMAIL_NOT_VERIFIED';
      } else if (
        lowerMessage.includes('reset link has already been used') ||
        (lowerMessage.includes('reset token') && lowerMessage.includes('used'))
      ) {
        errorCode = 'RESET_TOKEN_USED';
      } else if (lowerMessage.includes('reset link has expired')) {
        errorCode = 'RESET_TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid or expired reset token')) {
        errorCode = 'RESET_TOKEN_INVALID';
      } else if (lowerMessage.includes('already been used')) {
        errorCode = 'TOKEN_ALREADY_USED';
      } else if (lowerMessage.includes('expired')) {
        errorCode = 'TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid') && lowerMessage.includes('token')) {
        errorCode = 'INVALID_TOKEN';
      } else if (lowerMessage.includes('password') && lowerMessage.includes('characters')) {
        errorCode = 'WEAK_PASSWORD';
      } else if (lowerMessage.includes('invalid email or password')) {
        errorCode = 'INVALID_CREDENTIALS';
      }
      // If no specific pattern matched, use the detail message directly
      // Don't force it into a predefined error code
    } else if (data.error_code && typeof data.error_code === 'string') {
      // Legacy format
      errorCode = data.error_code;
      errorMessage = (data.message as string) || errorMessage;
      // Carry the admin-authored suspension message (ACCOUNT_SUSPENDED) so the
      // login page can show it in place of the generic suspended text.
      if (typeof data.suspension_message === 'string' && data.suspension_message) {
        errorDetails = { suspension_message: data.suspension_message };
      }
    }
  }

  throw new APIError(errorCode, errorMessage, resp.status, errorDetails);
}
