// First-run setup: create the first administrator with the one-time code the
// backend prints in its startup log.
//
// Under `/auth/setup/*` so the console's existing `/auth/*` rewrite reaches it;
// the console owns the `/setup` page itself.

import { safeFetch, setAccessToken } from './client';
import { apiErrorFromResponse, readJson } from './detail';
import type { LoginResponse } from './auth';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface SetupStatus {
  setup_required: boolean;
  database_enabled: boolean;
}

export interface SetupAdminRequest {
  /** As printed in the backend log, `XXXX-XXXX-XXXX`. */
  setup_code: string;
  login_name: string;
  password: string;
  display_name?: string;
}

/**
 * Whether this deployment still needs its first administrator.
 *
 * A gateway that predates first-run setup answers 404, and it has nothing to
 * set up: the console must keep working against it during a rolling upgrade.
 */
export async function getSetupStatus(): Promise<SetupStatus> {
  const resp = await safeFetch(`${API_BASE}/auth/setup/status`, {
    method: 'GET',
    headers: { accept: 'application/json' },
    credentials: 'include',
    cache: 'no-store',
  });
  if (resp.status === 404) return { setup_required: false, database_enabled: false };
  const status = await readJson<Partial<SetupStatus>>(resp);
  return {
    setup_required: status.setup_required === true,
    database_enabled: status.database_enabled === true,
  };
}

/**
 * Create the first administrator.
 *
 * Success is a login: the body is a `LoginResponse` and the backend sets the
 * refresh cookie, so the access token is stored exactly as `login()` stores it
 * and the browser is signed in as the new administrator.
 *
 * Failures keep their status for the page to explain: 403 wrong code, 409
 * already set up, 422 invalid input (`details.fields` by request field), 429
 * too many attempts, 503 database unavailable.
 */
export async function createSetupAdmin(request: SetupAdminRequest): Promise<LoginResponse> {
  const body: SetupAdminRequest = {
    setup_code: request.setup_code,
    login_name: request.login_name,
    password: request.password,
    ...(request.display_name ? { display_name: request.display_name } : {}),
  };
  const resp = await safeFetch(`${API_BASE}/auth/setup/admin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'include',
  });
  if (!resp.ok) throw await apiErrorFromResponse(resp);

  const result = (await resp.json()) as LoginResponse;
  setAccessToken(result.access_token);
  return result;
}

/** Characters in a setup code, not counting its dashes. */
export const SETUP_CODE_LENGTH = 12;

/**
 * The code in the form the log prints it.
 *
 * Accepts what an operator is likely to paste — lower case, spaces, missing or
 * extra dashes — and returns `XXXX-XXXX-XXXX` when it has the twelve
 * characters a code has, otherwise the trimmed input unchanged so validation
 * can say what is wrong.
 */
export function normalizeSetupCode(raw: string): string {
  const compact = raw.toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (compact.length !== SETUP_CODE_LENGTH) return raw.trim().toUpperCase();
  return `${compact.slice(0, 4)}-${compact.slice(4, 8)}-${compact.slice(8)}`;
}

export function isWellFormedSetupCode(raw: string): boolean {
  return raw.toUpperCase().replace(/[^A-Z0-9]/g, '').length === SETUP_CODE_LENGTH;
}
