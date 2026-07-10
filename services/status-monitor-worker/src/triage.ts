import type { Env } from "./env";

export type AlertStatus = "firing" | "resolved";
export type AlertSeverity = "critical" | "error" | "warn" | "info";

/** Alert envelope accepted by the Codex triage relay. */
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

export type NewCodexAlertEvent = Omit<CodexAlertEvent, "version" | "alert_id" | "source">;

export interface CodexRelayConfig {
  baseUrl: string;
  token: string;
}

const MAX_FINGERPRINT_LENGTH = 512;

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
  const baseUrl = env.CODEX_TRIAGE_RELAY_URL?.trim();
  const token = env.CODEX_TRIAGE_RELAY_TOKEN?.trim();
  return baseUrl && token ? { baseUrl, token } : null;
}

/** True when at least one complete delivery path is configured. */
export function hasAlertDestination(env: Env): boolean {
  return codexRelayConfig(env) != null || Boolean(env.SLACK_WEBHOOK_URL?.trim());
}

/** Adds worker-owned envelope fields to a triage event. */
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

/** POST an alert to the Codex triage relay. Returns true only on 2xx. */
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
      console.error(`codex triage relay returned HTTP ${resp.status}`);
    }
    return resp.ok;
  } catch {
    // Keep the relay URL, token, and alert context out of logs.
    console.error("codex triage relay post failed");
    return false;
  }
}
