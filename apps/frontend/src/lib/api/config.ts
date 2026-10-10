// Database-backed application configuration (admin) and backend restart.
//
// Shapes mirror `GET /admin/config` in
// docs/agents/specs/2026-10-10-db-backed-config-first-run-setup-design.md.

import { fetchWithAuth } from './client';
import { readJson } from './detail';
import { config as env } from '@/config/env';

const API_BASE = env.apiBase;

export type ConfigValueType = 'str' | 'text' | 'int' | 'float' | 'bool' | 'list';

/** Where the effective value came from. A database row wins even when empty. */
export type ConfigSource = 'database' | 'environment' | 'default';

/** A value in its typed form; a `list` travels as its comma-separated string. */
export type ConfigScalar = string | number | boolean;

export interface ConfigCategory {
  id: string;
  label: string;
  description: string;
}

export interface ConfigEntry {
  /** The canonical environment-variable name, e.g. `SMTP_PASSWORD`. */
  key: string;
  category: string;
  description: string;
  type: ConfigValueType;
  /** Write-only: `value` and `default` are always `null`. */
  secret: boolean;
  required: boolean;
  /** Required and empty. */
  missing: boolean;
  is_set: boolean;
  value: ConfigScalar | null;
  default: ConfigScalar | null;
  source: ConfigSource;
  /** Captured at startup: a change applies after a restart. */
  restart_required: boolean;
  /** Changed since the process booted, and not applied yet. */
  pending_restart: boolean;
  /** The environment has a different non-empty value that the row overrides. */
  environment_ignored: boolean;
  /** Cannot be changed once set. */
  immutable: boolean;
  /** Shown on the first-run configuration step. */
  setup: boolean;
  /** Added by an administrator; removed by a reset. */
  custom: boolean;
  /** Why the stored value could not be applied, when it could not. */
  invalid: string | null;
  /** Model ids whose routes reference this variable. */
  used_by: string[];
  updated_at: string | null;
  updated_by: string | null;
}

export interface ConfigResponse {
  categories: ConfigCategory[];
  entries: ConfigEntry[];
  /** Keys of the `missing` entries. */
  missing: string[];
  /** Keys of the `pending_restart` entries. */
  pending_restart: string[];
  /** Whether `POST /admin/system/restart` can bring the process back. */
  restart_supported: boolean;
}

export interface ConfigPatchRequest {
  /** A JSON boolean for `bool`, a number for `int`/`float`, a string otherwise. */
  values: Record<string, ConfigScalar>;
  /** The secret flag for keys this request adds as custom variables. */
  secrets?: Record<string, boolean>;
}

export interface RestartResponse {
  restarting: boolean;
}

/**
 * Every setting, grouped by category.
 *
 * Errors carry the server's `detail` verbatim (see `./detail`), so a page can
 * show "SMTP_PORT: must be an integer" rather than a guess at its meaning.
 */
export async function getConfig(): Promise<ConfigResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/config', {
    headers: { accept: 'application/json' },
    cache: 'no-store',
  });
  return readJson<ConfigResponse>(resp);
}

/**
 * Write a batch of values in one transaction; the backend validates the batch
 * as a whole. Returns the full configuration after the write.
 *
 * Errors: 400 `{detail: "KEY: reason"}`, 403 for an environment-only name,
 * 409 for an immutable key that is already set.
 */
export async function patchConfig(request: ConfigPatchRequest): Promise<ConfigResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/config', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(request),
  });
  return readJson<ConfigResponse>(resp);
}

/**
 * Remove a setting's database row: it falls back to the environment, then its
 * default, and a custom variable disappears. 409 for an immutable key.
 */
export async function resetConfigKey(key: string): Promise<ConfigResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/config/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    headers: { accept: 'application/json' },
  });
  return readJson<ConfigResponse>(resp);
}

/**
 * Ask the backend to exit so Docker or systemd starts it again. Answers 202
 * before the process goes down; 409 when it cannot restart itself.
 */
export async function restartBackend(): Promise<RestartResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/system/restart', {
    method: 'POST',
    headers: { accept: 'application/json' },
  });
  return readJson<RestartResponse>(resp);
}

/** Per-attempt deadline, so a connection the dying process holds open counts as down. */
const HEALTH_ATTEMPT_TIMEOUT_MS = 5_000;

/**
 * One liveness probe. Any failure — refused, timed out, or a non-2xx such as
 * the proxy's 502 while the backend is gone — is "down"; it never throws.
 */
export async function checkBackendHealth(): Promise<boolean> {
  try {
    const resp = await fetch(`${API_BASE}/health`, {
      cache: 'no-store',
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(HEALTH_ATTEMPT_TIMEOUT_MS),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

export type RestartWaitResult = 'restarted' | 'timeout' | 'aborted';

export interface WaitForRestartOptions {
  /** Give up after this long. Default two minutes. */
  timeoutMs?: number;
  /** Pause between probes. Default one second. */
  intervalMs?: number;
  signal?: AbortSignal;
  /** The probe; `checkBackendHealth` unless a test supplies one. */
  check?: () => Promise<boolean>;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

export const RESTART_TIMEOUT_MS = 120_000;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Wait for a restart to finish: the health check has to fail at least once and
 * then answer again.
 *
 * Seeing it fail first is the point. The restart request is answered before
 * the process exits, so the first probes usually reach the old process, and
 * "healthy" on its own would declare the restart done before it began.
 */
export async function waitForBackendRestart(
  options: WaitForRestartOptions = {},
): Promise<RestartWaitResult> {
  const {
    timeoutMs = RESTART_TIMEOUT_MS,
    intervalMs = 1_000,
    signal,
    check = checkBackendHealth,
    now = Date.now,
    sleep = delay,
  } = options;
  const deadline = now() + timeoutMs;
  let sawDown = false;

  while (now() < deadline) {
    if (signal?.aborted) return 'aborted';
    const up = await check();
    if (signal?.aborted) return 'aborted';
    if (!up) {
      sawDown = true;
    } else if (sawDown) {
      return 'restarted';
    }
    await sleep(intervalMs);
  }
  return 'timeout';
}
