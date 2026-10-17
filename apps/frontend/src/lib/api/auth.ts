import {
  fetchWithAuth,
  getAccessToken,
  jsonOrThrow,
  safeFetch,
  setAccessToken,
  withSessionLock,
} from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface SignupRequest {
  email: string;
  password: string;
  user_name: string;
  use_case?: string;
  accepted_tos: boolean;
  turnstileToken?: string;
}

export interface SignupResponse {
  message: string;
  email: string | null;
  phone?: string | null;
  user_id: string;
  requires_approval: boolean;
}

export interface LoginRequest {
  /**
   * An email address, or the login name of an account created without one
   * (the first-run administrator). The field keeps its name on the wire: the
   * backend reads a value containing `@` as an email and anything else as a
   * login name.
   */
  email: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: {
    id: string;
    /** `null` for an account that signs in with a login name instead. */
    email: string | null;
    login_name?: string | null;
    phone?: string | null;
    phone_verified?: boolean;
    role: string;
    is_admin: boolean;
  };
}

export type PhoneCodePurpose = 'signup' | 'login' | 'bind' | 'delete_account';

export interface PhoneSignupRequest {
  phone: string;
  code: string;
  user_name: string;
  use_case?: string;
  accepted_tos: true;
}

export async function sendPhoneCode(data: {
  phone: string;
  purpose: PhoneCodePurpose;
  turnstileToken?: string;
}): Promise<{ message: string; retry_after: number }> {
  const { turnstileToken, ...rest } = data;
  const options: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...rest,
      ...(turnstileToken ? { turnstile_token: turnstileToken } : {}),
    }),
    credentials: 'include',
  };
  // Binding and deleting act on the signed-in account, so they carry its token.
  const resp =
    data.purpose === 'bind' || data.purpose === 'delete_account'
      ? await fetchWithAuth(API_BASE, '/auth/phone/code', options)
      : await safeFetch(`${API_BASE}/auth/phone/code`, options);
  return jsonOrThrow(resp);
}

export async function signupPhone(data: PhoneSignupRequest): Promise<SignupResponse> {
  const resp = await safeFetch(`${API_BASE}/auth/phone/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
    credentials: 'include',
  });
  return jsonOrThrow(resp);
}

export async function loginPhone(data: { phone: string; code: string }): Promise<LoginResponse> {
  // A sign-in rotates the browser's cookie, like a refresh.
  const resp = await withSessionLock(() =>
    safeFetch(`${API_BASE}/auth/phone/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
      credentials: 'include',
    }),
  );
  const result = await jsonOrThrow<LoginResponse>(resp);
  setAccessToken(result.access_token);
  return result;
}

export interface BindEmailResponse {
  message: string;
  email: string;
  email_verified: boolean;
}

export async function bindEmail(data: { email: string }): Promise<BindEmailResponse> {
  const resp = await fetchWithAuth(API_BASE, '/auth/email/bind', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return jsonOrThrow(resp);
}

export async function bindPhone(data: {
  phone: string;
  code: string;
}): Promise<{ message: string }> {
  const resp = await fetchWithAuth(API_BASE, '/auth/phone/bind', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return jsonOrThrow(resp);
}

export async function signup(data: SignupRequest): Promise<SignupResponse> {
  const { turnstileToken, ...rest } = data;
  const body = {
    ...rest,
    ...(turnstileToken ? { turnstile_token: turnstileToken } : {}),
  };
  const resp = await safeFetch(`${API_BASE}/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'include',
  });

  return jsonOrThrow<SignupResponse>(resp);
}

export async function login(data: LoginRequest): Promise<LoginResponse> {
  // A sign-in rotates the browser's cookie, like a refresh.
  const resp = await withSessionLock(() =>
    safeFetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
      credentials: 'include',
    }),
  );

  const result = await jsonOrThrow<LoginResponse>(resp);
  setAccessToken(result.access_token);
  return result;
}

export async function logout(): Promise<void> {
  const token = getAccessToken();
  if (!token) return;

  try {
    await fetchWithAuth(API_BASE, '/auth/logout', {
      method: 'POST',
    });
  } finally {
    setAccessToken(null);
  }
}

/**
 * Delete the signed-in account. `confirm` is its sign-in identifier, typed by
 * its owner; an account with a password proves itself with it, a phone-only
 * account with a `delete_account` code. The response retires the browser's
 * cookie when no other account is signed in there.
 */
export async function deleteAccount(data: {
  confirm: string;
  password?: string;
  phone_code?: string;
}): Promise<{ message: string }> {
  // Like logout, not under the session lock: fetchWithAuth may refresh.
  const resp = await fetchWithAuth(API_BASE, '/auth/delete-account', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
    credentials: 'include',
  });
  const result = await jsonOrThrow<{ message: string }>(resp);
  setAccessToken(null);
  return result;
}

/** One account signed in on this browser, as the account chooser shows it. */
export interface AccountSummary {
  id: string;
  email: string | null;
  login_name?: string | null;
  phone?: string | null;
  user_name?: string | null;
  role: string;
  /** The account the browser is using. */
  active: boolean;
  /** False once its session has ended: choosing it asks for its password again. */
  signed_in: boolean;
  /** Whether it may use the Cloud Agent; null when signed out or unknown. */
  agent_access?: boolean | null;
}

export interface AccountsResponse {
  active_user_id: string | null;
  accounts: AccountSummary[];
  max_accounts: number;
}

/**
 * The accounts signed in on this browser. Needs only the cookie, no account.
 *
 * Under the session lock because the first answer to a cookie issued before
 * browser sessions existed replaces it.
 */
export async function listAccounts(): Promise<AccountsResponse> {
  const resp = await withSessionLock(() =>
    safeFetch(`${API_BASE}/auth/accounts`, { credentials: 'include' }),
  );
  return jsonOrThrow<AccountsResponse>(resp);
}

/**
 * Get a token for another account signed in on this browser, without a password.
 *
 * With `activate` (the default) it becomes the browser's account and this tab's
 * token. Without, the token is only returned — the consent page uses one to
 * sign the Cloud Agent in as an account the console is not showing.
 */
export async function switchAccount(
  userId: string,
  { activate = true }: { activate?: boolean } = {},
): Promise<LoginResponse> {
  const send = () =>
    safeFetch(`${API_BASE}/auth/switch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, activate }),
      credentials: 'include',
    });
  const resp = await (activate ? withSessionLock(send) : send());
  const result = await jsonOrThrow<LoginResponse>(resp);
  if (activate) setAccessToken(result.access_token);
  return result;
}

/** Sign one account out of this browser, whether or not its session is live. */
export async function removeAccount(userId: string): Promise<AccountsResponse> {
  const resp = await withSessionLock(() =>
    safeFetch(`${API_BASE}/auth/accounts/${encodeURIComponent(userId)}`, {
      method: 'DELETE',
      credentials: 'include',
    }),
  );
  return jsonOrThrow<AccountsResponse>(resp);
}

/** Sign every account out of this browser. */
export async function logoutAll(): Promise<void> {
  try {
    const resp = await withSessionLock(() =>
      safeFetch(`${API_BASE}/auth/logout-all`, { method: 'POST', credentials: 'include' }),
    );
    await jsonOrThrow(resp);
  } finally {
    setAccessToken(null);
  }
}

/**
 * The account the Cloud Agent is signed in as in this browser.
 *
 * `null` when it has no session; `undefined` when that cannot be told — the
 * Agent served from another origin, or not answering — so a caller deciding
 * whether to end that session must assume it might be anyone's.
 */
export async function agentSessionUser(agentsUrl: string): Promise<string | null | undefined> {
  if (!agentsUrl) return null;
  try {
    const resp = await fetch(`${agentsUrl.replace(/\/+$/, '')}/api/v1/session/me`, {
      credentials: 'include',
      signal: AbortSignal.timeout(AGENT_LOGOUT_TIMEOUT_MS),
    });
    if (resp.status === 401) return null;
    if (!resp.ok) return undefined;
    const body = (await resp.json()) as { id?: unknown };
    return typeof body.id === 'string' ? body.id : undefined;
  } catch {
    return undefined;
  }
}

// Wait for 3s for request to complete
export const AGENT_LOGOUT_TIMEOUT_MS = 3_000;

export async function endAgentSession(agentsUrl: string): Promise<void> {
  if (!agentsUrl) return;
  try {
    await fetch(`${agentsUrl.replace(/\/+$/, '')}/api/v1/session/logout`, {
      method: 'POST',
      mode: 'no-cors',
      credentials: 'include',
      signal: AbortSignal.timeout(AGENT_LOGOUT_TIMEOUT_MS),
    });
  } catch {
    // Agent unreachable or timed out
  }
}

export async function verifyEmail(token: string): Promise<{ message: string }> {
  const resp = await safeFetch(`${API_BASE}/auth/verify-email?token=${encodeURIComponent(token)}`, {
    method: 'GET',
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}

export async function forgotPassword(email: string): Promise<{ message: string }> {
  const resp = await safeFetch(`${API_BASE}/auth/forgot-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}

export async function resetPassword(
  token: string,
  newPassword: string,
): Promise<{ message: string }> {
  const resp = await safeFetch(`${API_BASE}/auth/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, new_password: newPassword }),
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}

export async function resendVerification(email: string): Promise<{ message: string }> {
  const resp = await fetch(`${API_BASE}/auth/resend-verification`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}
