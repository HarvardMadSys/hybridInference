// Centralized HTTP client with auth handling and token refresh.

import { APIError, httpStatusToErrorCode } from '@/lib/utils/errors';
import { branding } from '@/config/branding';

let accessToken: string | null = null;
let refreshPromise: Promise<boolean> | null = null;

export const AUTH_EXPIRED_EVENT = `${branding.storageKeyPrefix}:auth-expired`;

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

export function setAccessToken(token: string | null): void {
  accessToken = token;
  if (token) {
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

function isAccessTokenExpired(token: string | null): boolean {
  if (!token) return true;
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return true;
    const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
    const exp = payload?.exp;
    if (typeof exp !== 'number') return true;
    // 30s skew to avoid races with server clock
    return exp * 1000 < Date.now() + 30_000;
  } catch {
    return true;
  }
}

async function refreshAccessToken(apiBase: string): Promise<boolean> {
  // If refresh is already in progress, wait for it to prevent race conditions
  if (refreshPromise) {
    return refreshPromise;
  }

  refreshPromise = (async () => {
    try {
      const resp = await safeFetch(`${apiBase}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
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
      setAccessToken(token);
      return true;
    } finally {
      // Clear the promise so future refreshes can proceed
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

export async function fetchWithAuth(
  apiBase: string,
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  let token = getAccessToken();
  if (isAccessTokenExpired(token)) {
    await refreshAccessToken(apiBase);
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
      } else if (resp.status === 403) {
        errorCode = 'EMAIL_NOT_VERIFIED';
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
    }
  }

  throw new APIError(errorCode, errorMessage, resp.status);
}
