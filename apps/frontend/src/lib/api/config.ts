// Database-backed application configuration (admin) and backend restart.
//
// Shapes mirror `GET /admin/config` in
// docs/agents/specs/2026-10-10-db-backed-config-first-run-setup-design.md.

import { fetchWithAuth } from './client';
import { readJson } from './detail';
import { config as env } from '@/config/env';

const API_BASE = env.apiBase;

export type ConfigValueType = 'str' | 'text' | 'int' | 'float' | 'bool' | 'list';

/**
 * Where the value in force comes from. A database row wins even when empty; one
 * that cannot apply reports the environment or default in its place.
 */
export type ConfigSource = 'database' | 'environment' | 'default';

/**
 * Where an entry comes from: built in (`static`), a `${VAR}` reference in a
 * configuration file (`discovered`), a numbered provider key, or added by an
 * administrator (`custom`). Only a static entry's secret flag is fixed.
 */
export type ConfigOrigin = 'static' | 'discovered' | 'numbered' | 'custom';

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
  /** Write-only: `value` and `default` are always `null` (the client enforces it). */
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
  /**
   * Accepts no change now. Usually an immutable setting that is set, but not
   * only: `ERASURE_FENCE_SECRET` is locked, though empty, once the erasure
   * fence runs on `API_KEY_SECRET`. An older backend omits it; see `isLocked`.
   */
  locked?: boolean;
  /** Shown on the first-run configuration step. */
  setup: boolean;
  /**
   * Usually set once, when the gateway is deployed: listed after the settings
   * changed more often. An older backend omits it, and its order stays.
   */
  one_time?: boolean;
  /** Absent from an older backend, which cannot mark a stored variable secret. */
  origin?: ConfigOrigin;
  /** Added by an administrator (`origin` is `custom`); removed by a reset. */
  custom: boolean;
  /**
   * Why the stored value could not be applied, when it could not. The entry
   * then reports the environment value or default in force instead.
   */
  invalid: string | null;
  /** Model ids whose routes reference this variable. */
  used_by: string[];
  /** When the database row was last written; `null` when there is none. */
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
  /**
   * The secret flag for keys this request adds as custom variables, and — on a
   * backend that supports it — `true` to mark a stored discovered, numbered or
   * custom variable secret. One-way: `false` never makes a secret readable.
   */
  secrets?: Record<string, boolean>;
}

export type AlertNotificationChannel = 'slack' | 'wecom';

export interface AlertNotificationTestRequest {
  channel: AlertNotificationChannel;
  /** Omit to test the latest saved URL, including changes pending restart. */
  webhook_url?: string;
}

export interface AlertNotificationTestResponse {
  channel: AlertNotificationChannel;
  sent: true;
  test_id: string;
}

/** Send a test without saving settings or changing alert state. */
export async function testAlertNotification(
  request: AlertNotificationTestRequest,
): Promise<AlertNotificationTestResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/alerts/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(request),
  });
  return readJson<AlertNotificationTestResponse>(resp);
}

export interface RestartResponse {
  restarting: boolean;
  /** The process that is exiting; `/health` names a different one once it is back. */
  instance?: string;
}

/**
 * The configuration with no secret's value or default in it.
 *
 * The backend sends `null` for both. Dropping whatever a misbehaving or older
 * backend sent instead keeps a secret out of the console's state altogether,
 * so no component can render it by mistake.
 */
function withoutSecretValues(config: ConfigResponse): ConfigResponse {
  return {
    ...config,
    entries: config.entries.map((entry) =>
      entry.secret && (entry.value != null || entry.default != null)
        ? { ...entry, value: null, default: null }
        : entry,
    ),
  };
}

async function readConfig(resp: Response): Promise<ConfigResponse> {
  return withoutSecretValues(await readJson<ConfigResponse>(resp));
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
  return readConfig(resp);
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
  return readConfig(resp);
}

/**
 * Mark a stored variable secret, leaving its value as it is: from then on the
 * console shows only whether it is set. One-way — nothing in the API makes a
 * secret readable again.
 *
 * For a discovered, numbered or custom variable with a row stored, a value or
 * a reset. 400 otherwise, verbatim: "K has no stored value to protect yet: …",
 * "K: a built-in setting keeps the registry's secret flag." A backend that
 * predates this sends entries without `origin`, and would answer 200 without
 * marking anything.
 */
export async function markConfigSecret(key: string): Promise<ConfigResponse> {
  return patchConfig({ values: {}, secrets: { [key]: true } });
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
  return readConfig(resp);
}

export interface ConfigBackupPayload {
  schema_version: number;
  exported_at?: string | null;
  app_config?: Array<{ key: string; value: string; secret?: boolean; source?: string }>;
  site_settings?: Array<{ key: string; value: string; value_type?: string }>;
  provider_definitions?: Array<Record<string, unknown>>;
  provider_keys?: Array<Record<string, unknown>>;
  disabled_provider_env_keys?: Array<Record<string, unknown>>;
  provider_env_key_min_roles?: Array<Record<string, unknown>>;
  disabled_providers?: string[];
  provider_route_states?: Array<Record<string, unknown>>;
  provider_route_candidates?: Array<Record<string, unknown>>;
  provider_route_configs?: Array<Record<string, unknown>>;
  provider_route_capabilities?: Array<Record<string, unknown>>;
  provider_weight_overrides?: Array<Record<string, unknown>>;
  model_visibility_overrides?: Array<Record<string, unknown>>;
  model_concurrency_exemptions?: string[];
  batch_model_limits?: Record<string, number>;
  signup_allowed_domains?: Array<Record<string, unknown>>;
}

export interface ConfigRestoreResponse {
  config: ConfigResponse;
  immutable_keys_preserved: string[];
}

/**
 * Export all administrator-configurable state, including plaintext secrets, as a
 * portable JSON backup bundle.
 */
export async function exportConfigBackup(): Promise<ConfigBackupPayload> {
  const resp = await fetchWithAuth(API_BASE, '/admin/config/backup', {
    headers: { accept: 'application/json' },
    cache: 'no-store',
  });
  return readJson<ConfigBackupPayload>(resp);
}

/**
 * Restore administrator-configurable state from a backup bundle.
 */
export async function restoreConfigBackup(backup: unknown): Promise<ConfigRestoreResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/config/restore', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(backup),
  });
  const result = await readJson<ConfigRestoreResponse>(resp);
  return {
    ...result,
    config: withoutSecretValues(result.config),
  };
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

/** What one liveness probe found. */
export interface BackendHealth {
  up: boolean;
  /** The process that answered, when it says (an older backend does not). */
  instance: string | null;
}

/**
 * One liveness probe. Any failure — refused, timed out, or a non-2xx such as
 * the proxy's 502 while the backend is gone — is "down"; it never throws.
 */
export async function checkBackendHealth(): Promise<BackendHealth> {
  try {
    const resp = await fetch(`${API_BASE}/health`, {
      cache: 'no-store',
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(HEALTH_ATTEMPT_TIMEOUT_MS),
    });
    if (!resp.ok) return { up: false, instance: null };
    let instance: string | null = null;
    try {
      const body: unknown = await resp.json();
      if (body && typeof body === 'object' && 'instance' in body) {
        const value = (body as { instance: unknown }).instance;
        if (typeof value === 'string' && value) instance = value;
      }
    } catch {
      // An answer without a readable body is still an answer.
    }
    return { up: true, instance };
  } catch {
    return { up: false, instance: null };
  }
}

export type RestartWaitResult = 'restarted' | 'timeout' | 'aborted';

/** What the waits for one restart know about it. */
export interface RestartProgress {
  /** The health check has failed since the restart was requested. */
  sawDown: boolean;
  /** The process asked to restart (`RestartResponse.instance`), when the backend named it. */
  from?: string | null;
}

export interface WaitForRestartOptions {
  /** Give up after this long. Default two minutes. */
  timeoutMs?: number;
  /** Pause between probes. Default one second. */
  intervalMs?: number;
  signal?: AbortSignal;
  /** The probe; `checkBackendHealth` unless a test supplies one. */
  check?: () => Promise<BackendHealth>;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  /**
   * Shared by every wait for the same restart. A wait that resumes after a
   * timeout ("Keep waiting") must remember that the backend already went down,
   * or a backend that came back in between would never count as restarted.
   */
  progress?: RestartProgress;
}

const RESTART_TIMEOUT_MS = 120_000;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Wait for a restart to finish: a different process answers the health check.
 *
 * The restart request is answered before the process exits, so the first
 * probes usually reach the old process, and "healthy" on its own would declare
 * the restart done before it began. When the backend names its process
 * (`progress.from`, and the probe's `instance`), the restart is over once
 * another one answers, however short the outage: a backend back within one
 * polling interval is never seen down. Otherwise the health check has to fail
 * at least once and then answer again. Pass the same `progress` to a wait that
 * resumes an earlier one for the same restart.
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
    progress = { sawDown: false },
  } = options;
  const deadline = now() + timeoutMs;

  while (now() < deadline) {
    if (signal?.aborted) return 'aborted';
    const probe = await check();
    if (signal?.aborted) return 'aborted';
    if (!probe.up) {
      progress.sawDown = true;
    } else if (progress.from && probe.instance) {
      if (probe.instance !== progress.from) return 'restarted';
    } else if (progress.sawDown) {
      return 'restarted';
    }
    await sleep(intervalMs);
  }
  return 'timeout';
}
