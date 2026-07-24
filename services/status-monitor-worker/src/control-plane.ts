import { modelAlertFingerprint } from "./oncall";
import type { ProbeResult } from "./probe";

export type AlertDeliveryOwner = "legacy" | "control-plane";
export type ModelUnavailableStatus = "firing" | "resolved";
export type ModelUnavailabilityReason =
  | "authentication"
  | "rate_limited"
  | "timeout"
  | "unknown"
  | "upstream_error";

export interface ModelUnavailableAlertEvent {
  readonly schema_version: 1;
  readonly event_id: string;
  readonly alert_type: "model_unavailable";
  readonly fingerprint: string;
  readonly status: ModelUnavailableStatus;
  readonly severity: "error" | "info";
  readonly title: string;
  readonly occurred_at: string;
  readonly summary: string;
  readonly context: {
    readonly model_id: string;
    readonly consecutive_failures?: number;
    readonly failure_threshold?: number;
    readonly latency_ms?: number;
    readonly reason?: ModelUnavailabilityReason;
  };
  readonly evidence_refs: readonly string[];
}

export interface StatusMonitorControlPlaneService {
  /**
   * C3b will implement this as a role-specific Service Binding entrypoint.
   * Passing the persisted JSON string preserves the exact producer body across
   * an ambiguous response and its idempotent retry.
   */
  submitStatusMonitorEvent(bodyJson: string): Promise<{ readonly accepted: boolean }>;
}

export interface PendingCanonicalEvent {
  readonly eventId: string;
  readonly fingerprint: string;
  readonly status: ModelUnavailableStatus;
  readonly bodyJson: string;
}

export class ControlPlanePreparationError extends Error {
  constructor(code: "drain_ownership_corrupt" | "pending_event_corrupt") {
    super(code);
    this.name = "ControlPlanePreparationError";
  }
}

const OWNER_KEY_PREFIX = "alert_delivery_owner:v1:";
const PENDING_KEY_PREFIX = "alert_delivery_pending:v1:";
const EVENT_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const EVENT_KEYS = new Set([
  "schema_version",
  "event_id",
  "alert_type",
  "fingerprint",
  "status",
  "severity",
  "title",
  "occurred_at",
  "summary",
  "context",
  "evidence_refs",
]);
const CONTEXT_KEYS = new Set([
  "model_id",
  "consecutive_failures",
  "failure_threshold",
  "latency_ms",
  "reason",
]);
const REASONS = new Set<ModelUnavailabilityReason>([
  "authentication",
  "rate_limited",
  "timeout",
  "unknown",
  "upstream_error",
]);

function metaKey(prefix: string, fingerprint: string, status?: ModelUnavailableStatus): string {
  return status === undefined
    ? `${prefix}${fingerprint}`
    : `${prefix}${status}:${fingerprint}`;
}

async function readMeta(db: D1Database, key: string): Promise<string | null> {
  const row = await db
    .prepare(`SELECT value FROM meta WHERE key = ?`)
    .bind(key)
    .first<{ value: string }>();
  return row?.value ?? null;
}

async function writeMeta(db: D1Database, key: string, value: string): Promise<void> {
  await db
    .prepare(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`)
    .bind(key, value)
    .run();
}

async function deleteMeta(db: D1Database, key: string): Promise<void> {
  await db.prepare(`DELETE FROM meta WHERE key = ?`).bind(key).run();
}

/** Defaults fail closed to the current writer; C3a's deployed config remains unset. */
export function configuredDefaultOwner(value: string | undefined): AlertDeliveryOwner {
  return value?.trim() === "control-plane" ? "control-plane" : "legacy";
}

/**
 * Pin an outage fingerprint to one writer.
 *
 * A recovery without a row predates this table, so it is always legacy-owned.
 * This lets active legacy incidents drain safely after the future default flips.
 */
export async function resolveDrainOwner(
  db: D1Database,
  fingerprint: string,
  status: ModelUnavailableStatus,
  defaultOwner: AlertDeliveryOwner,
): Promise<AlertDeliveryOwner> {
  const key = metaKey(OWNER_KEY_PREFIX, fingerprint);
  const stored = await readMeta(db, key);
  if (stored !== null) {
    if (stored === "legacy" || stored === "control-plane") return stored;
    throw new ControlPlanePreparationError("drain_ownership_corrupt");
  }
  const owner = status === "resolved" ? "legacy" : defaultOwner;
  await writeMeta(db, key, owner);
  return owner;
}

export async function releaseDrainOwner(db: D1Database, fingerprint: string): Promise<void> {
  await deleteMeta(db, metaKey(OWNER_KEY_PREFIX, fingerprint));
}

function failureReason(error: string | null): ModelUnavailabilityReason {
  if (error === null) return "unknown";
  if (/\b(?:401|403|auth|unauthori[sz]ed|forbidden)\b/i.test(error)) return "authentication";
  if (/\b(?:429|rate.?limit)\b/i.test(error)) return "rate_limited";
  if (/\b(?:timeout|timed out|deadline)\b/i.test(error)) return "timeout";
  if (/\b(?:5\d\d|upstream|connection|unavailable)\b/i.test(error)) return "upstream_error";
  return "unknown";
}

function occurredAt(value: string): string {
  return Number.isNaN(Date.parse(value)) ? new Date().toISOString() : value;
}

/**
 * Translate one probe transition into the control plane's platform-neutral wire
 * contract. Raw probe errors, Slack text, environment, source, and deployment
 * identity are deliberately excluded.
 */
export function modelUnavailableEvent(
  result: ProbeResult,
  status: ModelUnavailableStatus,
  threshold: number,
  eventId: string = crypto.randomUUID(),
): ModelUnavailableAlertEvent {
  if (!Number.isSafeInteger(threshold) || threshold < 1) {
    throw new TypeError("failure threshold must be a positive integer");
  }
  const firing = status === "firing";
  const context: ModelUnavailableAlertEvent["context"] = firing
    ? {
        model_id: result.modelId,
        consecutive_failures: threshold,
        failure_threshold: threshold,
        reason: failureReason(result.error),
      }
    : {
        model_id: result.modelId,
        ...(result.latencyMs === null ? {} : { latency_ms: result.latencyMs }),
      };
  return {
    schema_version: 1,
    event_id: eventId,
    alert_type: "model_unavailable",
    fingerprint: modelAlertFingerprint(result.modelId),
    status,
    severity: firing ? "error" : "info",
    title: firing
      ? `Model unavailable: ${result.modelId}`
      : `Model recovered: ${result.modelId}`,
    occurred_at: occurredAt(result.checkedAt),
    summary: firing
      ? `${result.modelId} failed ${threshold} consecutive synthetic probes.`
      : `${result.modelId} accepted a successful synthetic probe.`,
    context,
    evidence_refs: [],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function validOptionalNonNegativeNumber(value: unknown): boolean {
  return value === undefined || (typeof value === "number" && Number.isFinite(value) && value >= 0);
}

function validBoundedString(value: unknown, maximum: number): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    [...value].length <= maximum &&
    !/[\u0000-\u001f\u007f-\u009f]/u.test(value)
  );
}

function parsePending(
  raw: string,
  fingerprint: string,
  status: ModelUnavailableStatus,
): PendingCanonicalEvent {
  try {
    const stored = JSON.parse(raw) as unknown;
    if (!isRecord(stored) || stored.schema_version !== 1 || typeof stored.body_json !== "string") {
      throw new Error("invalid wrapper");
    }
    if (
      Object.keys(stored).length !== 2 ||
      !Object.hasOwn(stored, "schema_version") ||
      !Object.hasOwn(stored, "body_json")
    ) {
      throw new Error("invalid wrapper keys");
    }
    const body = JSON.parse(stored.body_json) as unknown;
    if (!isRecord(body) || !hasExactKeys(body, EVENT_KEYS) || Object.keys(body).length !== 11) {
      throw new Error("invalid event");
    }
    if (
      body.schema_version !== 1 ||
      body.alert_type !== "model_unavailable" ||
      body.fingerprint !== fingerprint ||
      body.status !== status ||
      typeof body.event_id !== "string" ||
      !EVENT_ID_RE.test(body.event_id) ||
      !validBoundedString(body.fingerprint, 512) ||
      !validBoundedString(body.title, 500) ||
      !validBoundedString(body.occurred_at, 64) ||
      Number.isNaN(Date.parse(body.occurred_at)) ||
      !validBoundedString(body.summary, 4_000) ||
      !Array.isArray(body.evidence_refs) ||
      body.evidence_refs.length !== 0 ||
      !isRecord(body.context) ||
      !hasExactKeys(body.context, CONTEXT_KEYS) ||
      body.context.model_id === undefined ||
      !validBoundedString(body.context.model_id, 256) ||
      modelAlertFingerprint(body.context.model_id) !== fingerprint ||
      !validOptionalNonNegativeNumber(body.context.consecutive_failures) ||
      !validOptionalNonNegativeNumber(body.context.failure_threshold) ||
      !validOptionalNonNegativeNumber(body.context.latency_ms) ||
      (body.context.reason !== undefined &&
        !REASONS.has(body.context.reason as ModelUnavailabilityReason))
    ) {
      throw new Error("invalid event fields");
    }
    if (
      (status === "firing" && body.severity !== "error") ||
      (status === "resolved" && body.severity !== "info") ||
      (status === "firing" &&
        (!Number.isSafeInteger(body.context.consecutive_failures) ||
          (body.context.consecutive_failures as number) < 1 ||
          (body.context.consecutive_failures as number) > 1_000_000_000 ||
          !Number.isSafeInteger(body.context.failure_threshold) ||
          (body.context.failure_threshold as number) < 1 ||
          (body.context.failure_threshold as number) > 1_000_000_000 ||
          !REASONS.has(body.context.reason as ModelUnavailabilityReason))) ||
      (status === "resolved" &&
        (body.context.consecutive_failures !== undefined ||
          body.context.failure_threshold !== undefined ||
          body.context.reason !== undefined)) ||
      (typeof body.context.latency_ms === "number" &&
        body.context.latency_ms > 24 * 60 * 60 * 1_000)
    ) {
      throw new Error("invalid lifecycle");
    }
    return {
      eventId: body.event_id,
      fingerprint,
      status,
      bodyJson: stored.body_json,
    };
  } catch {
    // Never include the stored body in this error or in caller logs.
    throw new ControlPlanePreparationError("pending_event_corrupt");
  }
}

/**
 * Persist before attempting delivery. A later attempt returns the exact same
 * JSON bytes and event_id even if its fresh probe result would differ.
 */
export async function getOrCreatePendingCanonicalEvent(
  db: D1Database,
  candidate: ModelUnavailableAlertEvent,
): Promise<PendingCanonicalEvent> {
  const key = metaKey(PENDING_KEY_PREFIX, candidate.fingerprint, candidate.status);
  const stored = await readMeta(db, key);
  if (stored !== null) return parsePending(stored, candidate.fingerprint, candidate.status);

  const bodyJson = JSON.stringify(candidate);
  const wrapper = JSON.stringify({ schema_version: 1, body_json: bodyJson });
  const pending = parsePending(wrapper, candidate.fingerprint, candidate.status);
  await writeMeta(db, key, wrapper);
  return pending;
}

/**
 * Submit only to the control plane. Failure keeps the pending body for an
 * idempotent retry and deliberately has no relay/webhook fallback.
 */
export async function submitPendingCanonicalEvent(
  service: StatusMonitorControlPlaneService | undefined,
  pending: PendingCanonicalEvent,
): Promise<boolean> {
  if (service === undefined) return false;
  try {
    const result = await service.submitStatusMonitorEvent(pending.bodyJson);
    return result.accepted === true;
  } catch {
    console.error("alert control plane service binding submission failed");
    return false;
  }
}

export async function completePendingCanonicalEvent(
  db: D1Database,
  pending: PendingCanonicalEvent,
): Promise<void> {
  await deleteMeta(db, metaKey(PENDING_KEY_PREFIX, pending.fingerprint, pending.status));
  if (pending.status === "resolved") await releaseDrainOwner(db, pending.fingerprint);
}
