import type { Env } from "./env";

export type AlertStatus = "firing" | "resolved";
export type AlertSeverity = "critical" | "error" | "warn" | "info";

/** Alert envelope accepted by the Codex on-call relay. */
export interface CodexAlertEvent {
  version: "1";
  alert_id: string;
  fingerprint: string;
  source: "status-monitor-worker";
  status: AlertStatus;
  severity: AlertSeverity;
  title: string;
  environment: string;
  occurred_at: string;
  summary: string;
  context: Record<string, unknown>;
  slack_text: string;
  dedupe_window_seconds?: number;
}

/** Producer-neutral event accepted by the Unified Alert Control Plane V2. */
export interface AlertEventV2 {
  version: "2";
  alert_id: string;
  fingerprint: string;
  source: "status-monitor-worker";
  status: AlertStatus;
  severity: AlertSeverity;
  title: string;
  occurred_at: string;
  summary: string;
  context: Record<string, unknown>;
  deployment_sha?: string;
  evidence_refs?: string[];
}

export type NewCodexAlertEvent = Omit<CodexAlertEvent, "version" | "alert_id" | "source">;

export interface CodexRelayConfig {
  baseUrl: string;
  token: string;
}

export interface AlertRelayV2Config {
  baseUrl: string;
  token: string;
}

type V2JsonValue =
  | null
  | boolean
  | number
  | string
  | V2JsonValue[]
  | { [key: string]: V2JsonValue };

const MAX_FINGERPRINT_LENGTH = 512;
const SECRET_KEY_PARTS = [
  "api_key",
  "apikey",
  "authorization",
  "cookie",
  "password",
  "secret",
  "token",
];
const SENSITIVE_CONTEXT_KEYS = new Set([
  "email",
  "key_prefix",
  "offending_users",
  "remote_ip",
  "top_ips",
  "top_key_prefixes",
  "user_id",
  "user_name",
]);
const CONTEXT_CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;
const BEARER_RE = /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi;
const KEY_VALUE_RE = /\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+/gi;
const HYBRID_KEY_RE = /\bhyi-[A-Za-z0-9_-]{20,}\b/g;

function shortHash(value: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < value.length; i++) {
    hash = Math.imul(hash ^ value.charCodeAt(i), 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function boundedFingerprint(value: string): string {
  if (value.length <= MAX_FINGERPRINT_LENGTH) return value;
  const suffix = `:${shortHash(value)}`;
  return `${value.slice(0, MAX_FINGERPRINT_LENGTH - suffix.length)}${suffix}`;
}

/** Stable incident key shared by an individual model's down and recovery events. */
export function modelAlertFingerprint(modelId: string): string {
  return boundedFingerprint(`status-monitor:model:${modelId}`);
}

/** Stable incident key for a storm, independent of model ordering. */
export function stormAlertFingerprint(modelIds: string[]): string {
  const sortedIds = [...modelIds].sort();
  return `status-monitor:storm:${shortHash(JSON.stringify(sortedIds))}`;
}

/** Returns relay settings only when both required bindings are non-empty. */
export function codexRelayConfig(env: Env): CodexRelayConfig | null {
  const baseUrl = env.CODEX_ONCALL_RELAY_URL?.trim();
  const token = env.CODEX_ONCALL_RELAY_TOKEN?.trim();
  return baseUrl && token ? { baseUrl, token } : null;
}

/** Returns V2 relay settings only when both required bindings are non-empty. */
export function alertRelayV2Config(env: Env): AlertRelayV2Config | null {
  const baseUrl = env.ALERT_RELAY_V2_URL?.trim();
  const token = env.ALERT_RELAY_V2_TOKEN?.trim();
  if (!baseUrl || !token || !credentialFreeHttpsUrl(baseUrl)) return null;
  return { baseUrl, token };
}

/** True when at least one complete delivery path is configured. */
export function hasAlertDestination(env: Env): boolean {
  return (
    alertRelayV2Config(env) != null ||
    codexRelayConfig(env) != null ||
    Boolean(env.SLACK_WEBHOOK_URL?.trim())
  );
}

/** Adds worker-owned envelope fields to a oncall event. */
export function createCodexAlertEvent(event: NewCodexAlertEvent): CodexAlertEvent {
  return {
    version: "1",
    alert_id: crypto.randomUUID(),
    source: "status-monitor-worker",
    ...event,
    title: event.title.slice(0, 500),
    summary: event.summary.slice(0, 4_000),
    slack_text: event.slack_text.slice(0, 40_000),
  };
}

function credentialFreeHttpsUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      Boolean(url.hostname) &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash
    );
  } catch {
    return false;
  }
}

function sanitizeContextString(value: string): string {
  return value
    .replace(CONTEXT_CONTROL_RE, " ")
    .replace(BEARER_RE, "Bearer [REDACTED]")
    .replace(KEY_VALUE_RE, (_match, key: string) => `${key}=[REDACTED]`)
    .replace(HYBRID_KEY_RE, "[REDACTED]")
    .slice(0, 2_000);
}

function sanitizeContextValue(value: unknown, key: string, depth: number): V2JsonValue {
  const normalizedKey = key.toLowerCase().replaceAll("-", "_");
  if (
    SENSITIVE_CONTEXT_KEYS.has(normalizedKey) ||
    SECRET_KEY_PARTS.some((part) => normalizedKey.includes(part))
  ) {
    return "[REDACTED]";
  }
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") return sanitizeContextString(value);
  if (depth >= 4) return "[TRUNCATED]";
  if (Array.isArray(value)) {
    return value.slice(0, 50).map((item) => sanitizeContextValue(item, "", depth + 1));
  }
  if (typeof value === "object") {
    const result: Record<string, V2JsonValue> = Object.create(null) as Record<
      string,
      V2JsonValue
    >;
    for (const [rawKey, child] of Object.entries(value).slice(0, 50)) {
      const safeKey = rawKey.replace(CONTEXT_CONTROL_RE, " ").trim().slice(0, 128);
      if (safeKey) result[safeKey] = sanitizeContextValue(child, safeKey, depth + 1);
    }
    return result;
  }
  return sanitizeContextString(String(value));
}

function compactContextValue(
  value: V2JsonValue,
  stringLimit: number,
  collectionLimit: number,
): V2JsonValue {
  if (typeof value === "string") return value.slice(0, stringLimit);
  if (Array.isArray(value)) {
    return value
      .slice(0, collectionLimit)
      .map((item) => compactContextValue(item, stringLimit, collectionLimit));
  }
  if (value !== null && typeof value === "object") {
    const result: Record<string, V2JsonValue> = Object.create(null) as Record<
      string,
      V2JsonValue
    >;
    for (const [key, child] of Object.entries(value).slice(0, collectionLimit)) {
      result[key] = compactContextValue(child, stringLimit, collectionLimit);
    }
    return result;
  }
  return value;
}

/** Truncate and redact monitor context to the strict V2 relay contract. */
export function sanitizeAlertContextV2(context: Record<string, unknown>): Record<string, unknown> {
  let sanitized = sanitizeContextValue(context, "", 0);
  for (const [stringLimit, collectionLimit] of [
    [2_000, 50],
    [512, 25],
    [256, 15],
    [128, 10],
  ]) {
    sanitized = compactContextValue(sanitized, stringLimit, collectionLimit);
    if (new TextEncoder().encode(JSON.stringify(sanitized)).byteLength <= 24_000) {
      return sanitized as Record<string, unknown>;
    }
  }
  return { _truncated: true };
}

/** Convert the existing structured event to V2 without producer Slack/environment fields. */
export function createAlertEventV2(event: CodexAlertEvent, deploymentSha?: string): AlertEventV2 {
  const normalizedSha = deploymentSha?.trim();
  const v2: AlertEventV2 = {
    version: "2",
    alert_id: event.alert_id,
    fingerprint: event.fingerprint,
    source: "status-monitor-worker",
    status: event.status,
    severity: event.severity,
    title: event.title,
    occurred_at: event.occurred_at,
    summary: event.summary,
    context: sanitizeAlertContextV2(event.context),
  };
  if (normalizedSha && /^[a-f0-9]{40}$/i.test(normalizedSha)) {
    v2.deployment_sha = normalizedSha;
  }
  return v2;
}

/** POST an alert to the Codex on-call relay. Returns true only on 2xx. */
export async function postCodexAlert(
  relay: CodexRelayConfig,
  event: CodexAlertEvent,
): Promise<boolean> {
  const endpoint = `${relay.baseUrl.replace(/\/+$/, "")}/v1/alerts`;
  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${relay.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(event),
      redirect: "error",
      signal: AbortSignal.timeout(10_000),
    });
    if (!resp.ok) {
      console.error(`codex oncall relay returned HTTP ${resp.status}`);
    }
    return resp.ok;
  } catch {
    // Keep the relay URL, token, and alert context out of logs.
    console.error("codex oncall relay post failed");
    return false;
  }
}

/** POST a producer-neutral event to the V2 relay. Returns true only on 2xx. */
export async function postAlertEventV2(
  relay: AlertRelayV2Config,
  event: AlertEventV2,
): Promise<boolean> {
  if (!credentialFreeHttpsUrl(relay.baseUrl)) return false;
  const endpoint = `${relay.baseUrl.replace(/\/+$/, "")}/v2/alerts`;
  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${relay.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(event),
      redirect: "error",
      signal: AbortSignal.timeout(10_000),
    });
    if (!resp.ok) {
      console.error(`V2 alert relay returned HTTP ${resp.status}`);
    }
    return resp.ok;
  } catch {
    console.error("V2 alert relay post failed");
    return false;
  }
}
