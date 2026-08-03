import { fetchWithAuth, jsonOrThrow } from './client';
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
 * redirect match, code lifetime. This call just presents the signed-in user's
 * bearer token; a 400 means the request named a client or redirect the gateway
 * refuses to serve.
 */
export async function createAuthorizationCode(
  data: AuthorizationCodeRequest,
): Promise<AuthorizationCodeResponse> {
  const resp = await fetchWithAuth(API_BASE, '/v1/identity/code', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return jsonOrThrow<AuthorizationCodeResponse>(resp);
}
