import { fetchWithAuth, jsonOrThrow, safeFetch } from './client';
import { config } from '@/config/env';
import { APIError } from '@/lib/utils/errors';

const API_BASE = config.apiBase;

/**
 * An application's request to have an API key created on the visitor's
 * account, as it arrives in the consent page's address.
 */
export interface KeyAuthorizationRequest {
  callback_url: string;
  code_challenge: string;
  code_challenge_method: 'S256';
}

/** What the gateway says about a request it would accept. */
export interface KeyAuthorizationCheck {
  /** The callback's host: the only name the page shows for the application. */
  app: string;
  /** Where Cancel goes: the callback with `error=access_denied`, built by the gateway. */
  cancel_url: string;
}

/** A one-time code for the application, and where to deliver it. */
export interface KeyAuthorizationCode {
  code: string;
  /** The callback with `code=` added, built by the gateway. */
  redirect_url: string;
  expires_in: number;
  app: string;
}

/**
 * Read a key-authorization answer, keeping the gateway's `error.type`.
 *
 * `jsonOrThrow` classifies a refusal by its message and status; the consent
 * page also needs the type the gateway named (`invalid_request`,
 * `not_configured`, `key_limit_reached`, ...), so it rides along in the
 * error's `details.type`.
 */
async function keyAuthorizationResult<T>(resp: Response): Promise<T> {
  if (resp.ok) return jsonOrThrow<T>(resp);
  let type: string | undefined;
  try {
    const body = (await resp.clone().json()) as { error?: unknown };
    const error = body?.error;
    if (
      error &&
      typeof error === 'object' &&
      typeof (error as { type?: unknown }).type === 'string'
    ) {
      type = (error as { type: string }).type;
    }
  } catch {
    // Not JSON: jsonOrThrow classifies it by its status instead.
  }
  try {
    return await jsonOrThrow<T>(resp);
  } catch (err) {
    if (err instanceof APIError && type) {
      throw new APIError(err.code, err.message, err.statusCode, { ...err.details, type });
    }
    throw err;
  }
}

/** The gateway's `error.type` for a refused key-authorization request, if it named one. */
export function keyAuthorizationErrorType(error: unknown): string | null {
  if (error instanceof APIError && typeof error.details?.type === 'string') {
    return error.details.type;
  }
  return null;
}

/**
 * Ask the gateway whether it would accept this request, before anyone signs in.
 *
 * Public: it answers for the request alone — the callback against the
 * deployment's allowlist, the PKCE challenge, whether the feature is on — and
 * names no account. A 400 means the gateway refuses the request; a 404 that
 * this deployment does not let applications create keys at all.
 */
export async function checkKeyAuthorization(
  request: KeyAuthorizationRequest,
): Promise<KeyAuthorizationCheck> {
  const resp = await safeFetch(`${API_BASE}/v1/auth/keys/check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
    credentials: 'omit',
  });
  return keyAuthorizationResult<KeyAuthorizationCheck>(resp);
}

/**
 * Issue the application a one-time code that it exchanges for a new API key on
 * the signed-in account.
 *
 * The code is issued to the account the token names: this tab's own unless
 * `accessToken` is given, which the consent page does for another account
 * signed in on the browser.
 */
export async function createKeyAuthorizationCode(
  request: KeyAuthorizationRequest,
  accessToken?: string,
): Promise<KeyAuthorizationCode> {
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  };
  const resp = accessToken
    ? await safeFetch(`${API_BASE}/v1/auth/keys/code`, {
        ...init,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
        credentials: 'include',
      })
    : await fetchWithAuth(API_BASE, '/v1/auth/keys/code', init);
  return keyAuthorizationResult<KeyAuthorizationCode>(resp);
}
