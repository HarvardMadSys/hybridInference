import { fetchWithAuth, jsonOrThrow, safeFetch } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface AuthorizationCodeRequest {
  client_id: string;
  redirect_uri: string;
  code_challenge: string;
  code_challenge_method: 'S256';
}

export interface AuthorizationCodeResponse {
  code: string;
  expires_in: number;
}

/**
 * Ask the gateway for a one-time cross-service authorization code.
 *
 * The server owns every decision that matters here — client allowlist, exact
 * redirect match, code lifetime. This call just presents a bearer token; a 400
 * means the request named a client or redirect the gateway refuses to serve.
 *
 * The code is issued to the account the token names: this tab's own unless
 * `accessToken` is given, which the consent page does for another account
 * signed in on the browser.
 */
export async function createAuthorizationCode(
  data: AuthorizationCodeRequest,
  accessToken?: string,
): Promise<AuthorizationCodeResponse> {
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  };
  const resp = accessToken
    ? await safeFetch(`${API_BASE}/v1/identity/code`, {
        ...init,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
        credentials: 'include',
      })
    : await fetchWithAuth(API_BASE, '/v1/identity/code', init);
  return jsonOrThrow<AuthorizationCodeResponse>(resp);
}
