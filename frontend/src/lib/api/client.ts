// Centralized HTTP client with auth handling and token refresh.

import { APIError } from '@/lib/utils/errors';

export interface APIErrorResponse {
  error_code: string;
  message: string;
  timestamp: string;
  [key: string]: unknown;
}

let accessToken: string | null = null;
let refreshPromise: Promise<boolean> | null = null;

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

async function refreshAccessToken(apiBase: string): Promise<boolean> {
  // If refresh is already in progress, wait for it to prevent race conditions
  if (refreshPromise) {
    return refreshPromise;
  }

  refreshPromise = (async () => {
    try {
      const resp = await fetch(`${apiBase}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
      });
      if (!resp.ok) {
        setAccessToken(null);
        return false;
      }
      const data = await resp.json();
      const token = data?.access_token as string | undefined;
      if (!token) return false;
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
  const headers = new Headers(init.headers || {});
  const token = getAccessToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const resp = await fetch(`${apiBase}${input}`, {
    ...init,
    headers,
    credentials: 'include',
  });

  if (resp.status !== 401) return resp;

  const refreshed = await refreshAccessToken(apiBase);
  if (!refreshed) return resp;

  const headersRetry = new Headers(init.headers || {});
  const newToken = getAccessToken();
  if (newToken) headersRetry.set('Authorization', `Bearer ${newToken}`);

  return fetch(`${apiBase}${input}`, {
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
    // JSON parsing failed, use status code
    throw new APIError('NETWORK_ERROR', `HTTP ${resp.status}: ${resp.statusText}`, resp.status);
  }

  // Handle backend error format: { error: { type: "...", message: "...", code: 500 } }
  // or { detail: "..." } (FastAPI validation errors)
  let errorCode = 'UNKNOWN_ERROR';
  let errorMessage = 'An unknown error occurred';

  if (errorData && typeof errorData === 'object') {
    const data = errorData as Record<string, unknown>;

    if (data.error && typeof data.error === 'object') {
      // Backend error format: { error: { message: "...", type: "...", code: 400 } }
      const error = data.error as Record<string, unknown>;
      errorMessage = (error.message as string) || errorMessage;

      // Extract error patterns from message
      if (errorMessage.includes('already been used')) {
        errorCode = 'TOKEN_ALREADY_USED';
      } else if (errorMessage.includes('expired')) {
        errorCode = 'TOKEN_EXPIRED';
      } else if (errorMessage.includes('Invalid') && errorMessage.includes('token')) {
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

      // Try to extract more specific error from detail message
      if (errorMessage.includes('already registered')) {
        errorCode = 'USER_ALREADY_EXISTS';
      } else if (
        errorMessage.includes('reset link has already been used') ||
        (errorMessage.includes('reset token') && errorMessage.includes('used'))
      ) {
        errorCode = 'RESET_TOKEN_USED';
      } else if (errorMessage.includes('Reset link has expired')) {
        errorCode = 'RESET_TOKEN_EXPIRED';
      } else if (errorMessage.includes('Invalid or expired reset token')) {
        errorCode = 'RESET_TOKEN_INVALID';
      } else if (errorMessage.includes('already been used')) {
        errorCode = 'TOKEN_ALREADY_USED';
      } else if (errorMessage.includes('expired')) {
        errorCode = 'TOKEN_EXPIRED';
      } else if (errorMessage.includes('Invalid') && errorMessage.includes('token')) {
        errorCode = 'INVALID_TOKEN';
      } else if (errorMessage.includes('password') && errorMessage.includes('characters')) {
        errorCode = 'WEAK_PASSWORD';
      } else if (errorMessage.includes('Invalid email or password')) {
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
