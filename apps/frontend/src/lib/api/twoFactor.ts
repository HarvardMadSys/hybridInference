// The account's second factor: an authenticator app and its recovery codes.
import { fetchWithAuth } from './client';
import { apiErrorFromResponse } from './detail';
import { config } from '@/config/env';
import { APIError } from '@/lib/utils/errors';

const API_BASE = config.apiBase;
const JSON_HEADERS = { 'Content-Type': 'application/json' };

/**
 * The answer, or the gateway's own sentence for a refusal. These say what to
 * do next -- a wrong code, a setup that was replaced -- where the shared
 * mapping would read a 409 as a signup conflict.
 */
async function read<T>(resp: Response): Promise<T> {
  if (resp.ok) return resp.json() as Promise<T>;
  const error = await apiErrorFromResponse(resp);
  // Codes held off after wrong ones is not the generic rate limit: its text
  // says a recovery code still works.
  if (resp.status === 429 && error.message) {
    throw new APIError('TWO_FACTOR_HELD_OFF', error.message, resp.status);
  }
  throw error;
}

export interface TwoFactorStatus {
  /** The account has an authenticator app. */
  enabled: boolean;
  /** A secret was shown and awaits its first code. */
  pending: boolean;
  /** This account has administrator access only from a verified session. */
  required: boolean;
  /** This session has given a code, from the browser it is used in now. */
  session_verified: boolean;
  recovery_codes_left: number;
}

export interface TwoFactorSetup {
  /** Base32, for typing into the app by hand. */
  secret: string;
  /** The same as an `otpauth://` link, for a QR code. */
  otpauth_uri: string;
}

export interface TwoFactorRecoveryCodes {
  recovery_codes: string[];
}

/** Whether `code` is long enough to be an authenticator code or a recovery code. */
export function isCompleteTwoFactorCode(code: string): boolean {
  return code.replace(/[\s-]/g, '').length >= 6;
}

export async function getTwoFactorStatus(): Promise<TwoFactorStatus> {
  return read(await fetchWithAuth(API_BASE, '/auth/two-factor'));
}

/**
 * Show a new secret. A first authenticator needs the account's password;
 * replacing one needs a recent code instead, which the console asks for.
 */
export async function startTwoFactorSetup(password?: string): Promise<TwoFactorSetup> {
  const resp = await fetchWithAuth(API_BASE, '/auth/two-factor/setup', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(password ? { password } : {}),
  });
  return read(resp);
}

/** Drop the setup this session started and did not confirm. */
export async function cancelTwoFactorSetup(): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, '/auth/two-factor/setup', { method: 'DELETE' });
  if (!resp.ok) throw await apiErrorFromResponse(resp);
}

/**
 * Turn the shown secret on with its first code; answers with the recovery
 * codes, once. The gateway takes the code only from the session that started
 * the setup, for a few minutes and a few tries: after that it answers 409 and
 * the setup is started again.
 */
export async function confirmTwoFactor(code: string): Promise<TwoFactorRecoveryCodes> {
  const resp = await fetchWithAuth(API_BASE, '/auth/two-factor/confirm', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ code }),
  });
  return read(resp);
}

/** Give this session a code from the app, or a recovery code. */
export async function verifyTwoFactor(
  code: string,
): Promise<{ verified: boolean; recovery_codes_left: number }> {
  const resp = await fetchWithAuth(API_BASE, '/auth/two-factor/verify', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ code }),
  });
  return read(resp);
}

/** Replace every recovery code with a new set, shown once. */
export async function newRecoveryCodes(): Promise<TwoFactorRecoveryCodes> {
  const resp = await fetchWithAuth(API_BASE, '/auth/two-factor/recovery-codes', {
    method: 'POST',
  });
  return read(resp);
}

export async function removeTwoFactor(): Promise<TwoFactorStatus> {
  return read(await fetchWithAuth(API_BASE, '/auth/two-factor', { method: 'DELETE' }));
}
