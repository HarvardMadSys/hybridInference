import { fetchWithAuth, jsonOrThrow, safeFetch } from './client';
import { config } from '@/config/env';
import { APIError } from '@/lib/utils/errors';

/**
 * Single sign-on through an organization's identity provider.
 *
 * The provider answers by navigation, never to a request made here: a call
 * that starts a round trip resolves to the address to send the browser to,
 * and the outcome arrives later in the query of the page it returns to
 * (`sso`, `sso_error`, `reauth`, `sso_linked`).
 */

const API_BASE = config.apiBase;

/** An identity provider, as much of it as an account's own pages may show. */
export interface SsoProviderRef {
  id: string;
  name: string;
}

/**
 * Why a round trip to a provider failed, as the page it returns to is told in
 * `sso_error`. Only the code travels; the provider's own detail stays in the
 * gateway's log.
 */
export const SSO_ERROR_CODES = [
  'unavailable',
  'rate_limited',
  'cancelled',
  'expired',
  'failed',
  'email_untrusted',
  'email_in_use',
  'identity_conflict',
  'identity_in_use',
  'no_account',
  'pending',
  'suspended',
  'inactive',
  'browser_full',
] as const;

export type SsoErrorCode = (typeof SSO_ERROR_CODES)[number];

/**
 * The code an `sso_error` parameter carries: `null` when there is none, and
 * `failed` for one this console does not know, which a later gateway may send.
 */
export function ssoErrorCode(raw: string | null | undefined): SsoErrorCode | null {
  if (!raw) return null;
  return (SSO_ERROR_CODES as readonly string[]).includes(raw) ? (raw as SsoErrorCode) : 'failed';
}

/** The sensitive actions an account can confirm by signing in at its provider again. */
export type SsoReauthPurpose =
  | 'set_password'
  | 'delete_account'
  | 'bind_email'
  | 'link_sso'
  | 'unlink_sso'
  /** One guarded administrator change, in the Admin console. */
  | 'admin_action';

const SSO_REAUTH_PURPOSES: readonly string[] = [
  'set_password',
  'delete_account',
  'bind_email',
  'link_sso',
  'unlink_sso',
  'admin_action',
];

/** The purpose a `reauth` parameter carries, or `null` when it names none. */
export function ssoReauthPurpose(raw: string | null | undefined): SsoReauthPurpose | null {
  return raw && SSO_REAUTH_PURPOSES.includes(raw) ? (raw as SsoReauthPurpose) : null;
}

interface SsoStartResponse {
  available: boolean;
  authorization_url: string | null;
}

/**
 * Where a start response sends the browser, or `null` when no provider answers
 * for the email. The address is a provider's, so an absolute web address is
 * the only thing accepted: it is handed to `location.assign`.
 */
function authorizationUrl(result: SsoStartResponse): string | null {
  if (!result.available) return null;
  const url = result.authorization_url;
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) {
    throw new APIError('UNKNOWN_ERROR', 'Single sign-on could not be started.');
  }
  return url;
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

/**
 * Find the provider that answers for an email's domain and start a sign-in
 * there. Resolves to the provider's address, or `null` when none answers for
 * that domain. The answer is the same whether or not the email has an account.
 *
 * Throws with status 429 after too many attempts, and 503 when the provider
 * cannot be reached.
 */
export async function discoverSso(email: string, returnTo: string): Promise<string | null> {
  const resp = await safeFetch(`${API_BASE}/auth/sso/discover`, {
    ...jsonPost({ email, return_to: returnTo }),
    credentials: 'include',
  });
  return authorizationUrl(await jsonOrThrow<SsoStartResponse>(resp));
}

/**
 * The address that starts a sign-in at one known provider. A plain link, not a
 * request: the gateway answers it with a redirect to the provider.
 */
export function ssoStartUrl(providerId: string, returnTo: string): string {
  return `${API_BASE}/auth/sso/${encodeURIComponent(providerId)}/start?return_to=${encodeURIComponent(returnTo)}`;
}

/** An identity the last sign-in left waiting for the owner of the account it matched. */
export interface PendingSsoLink {
  provider_id: string;
  provider_name: string;
  email: string;
}

/**
 * The identity waiting to be linked in this browser, or `null` when none is,
 * or it expired. Read from the browser's cookie, so it needs no account and
 * the email never travels in an address.
 */
export async function getPendingSsoLink(): Promise<PendingSsoLink | null> {
  const resp = await safeFetch(`${API_BASE}/auth/sso/link`, { credentials: 'include' });
  if (resp.status === 404) return null;
  return jsonOrThrow<PendingSsoLink>(resp);
}

/**
 * Link the waiting identity to the signed-in account. Refused with 404 when
 * nothing is waiting any more, 409 when it does not belong on this account,
 * and 403 when the account is not active; the message says which.
 */
export async function confirmSsoLink(): Promise<{ message: string }> {
  const resp = await fetchWithAuth(API_BASE, '/auth/sso/link/confirm', { method: 'POST' });
  return jsonOrThrow(resp);
}

/** Drop the waiting identity; nothing is linked. Needs no account. */
export async function cancelSsoLink(): Promise<{ message: string }> {
  const resp = await safeFetch(`${API_BASE}/auth/sso/link/cancel`, {
    method: 'POST',
    credentials: 'include',
  });
  return jsonOrThrow(resp);
}

/**
 * Start confirming a sensitive action at the account's provider, for an account
 * with no password and no verified phone. Resolves to the provider's address.
 *
 * The browser comes back to `returnTo` with `reauth=<purpose>`. The
 * confirmation then holds once, for five minutes, in this session: the
 * action's own request is sent without a password or a code.
 *
 * Throws with status 409 when the account has no identity it can confirm with.
 */
export async function startSsoReauth(purpose: SsoReauthPurpose, returnTo: string): Promise<string> {
  const resp = await fetchWithAuth(
    API_BASE,
    '/auth/sso/reauth/start',
    jsonPost({ purpose, return_to: returnTo }),
  );
  const url = authorizationUrl(await jsonOrThrow<SsoStartResponse>(resp));
  if (!url) throw new APIError('UNKNOWN_ERROR', 'Single sign-on could not be started.');
  return url;
}

/** One identity linked to the signed-in account. */
export interface SsoIdentity {
  id: string;
  provider_id: string;
  provider_name: string;
  /** The email the identity carried when it was linked. */
  email: string | null;
  linked_at: string;
  last_login_at: string | null;
  /** False once its provider is no longer offered here. */
  available: boolean;
}

/** The identities that can sign in to the signed-in account. */
export async function listSsoIdentities(): Promise<SsoIdentity[]> {
  const resp = await fetchWithAuth(API_BASE, '/auth/sso/identities');
  const result = await jsonOrThrow<{ identities: SsoIdentity[] }>(resp);
  return result.identities;
}

/**
 * Start linking another identity to the signed-in account: sign in at the
 * provider that answers for `email`. Resolves to the provider's address, or
 * `null` when none answers for that domain.
 *
 * Its owner confirms first: with the account's `password`, or, for an account
 * that signs in only through a provider, by `startSsoReauth('link_sso')` and
 * then no password here. The browser comes back to `returnTo` with
 * `sso_linked=1`, or with `sso_error`.
 */
export async function linkSsoIdentity(data: {
  email: string;
  returnTo: string;
  password?: string;
}): Promise<string | null> {
  const resp = await fetchWithAuth(
    API_BASE,
    '/auth/sso/identities/link',
    jsonPost({
      email: data.email,
      return_to: data.returnTo,
      ...(data.password ? { password: data.password } : {}),
    }),
  );
  return authorizationUrl(await jsonOrThrow<SsoStartResponse>(resp));
}

/**
 * Remove one identity from the signed-in account, confirmed as linking is
 * (`unlink_sso`). Refused with 409 when it is the account's only way to sign in.
 */
export async function unlinkSsoIdentity(
  identityId: string,
  data: { password?: string } = {},
): Promise<{ message: string }> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/auth/sso/identities/${encodeURIComponent(identityId)}/unlink`,
    jsonPost(data.password ? { password: data.password } : {}),
  );
  return jsonOrThrow(resp);
}

/**
 * What a refused single sign-on request said. The gateway's sentence is the
 * one to show for these: it tells which rule refused -- another email, an
 * identity already in use, the only way to sign in -- where the console's own
 * text for the status would not (a 409 reads as "already registered").
 */
export function ssoRefusalMessage(error: unknown): string | null {
  if (!(error instanceof APIError) || !error.message) return null;
  return error.statusCode === 400 || error.statusCode === 403 || error.statusCode === 409
    ? error.message
    : null;
}
