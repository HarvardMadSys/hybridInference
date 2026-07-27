import type { CycleStatus } from "./db";
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
export type StatusMonitorRpcErrorCode =
  | "control_plane_dormant"
  | "control_plane_unavailable"
  | "deployment_mismatch"
  | "event_id_conflict"
  | "invalid_event"
  | "invalid_producer_identity"
  | "retired_deployment"
  | "unknown_deployment";

export type StatusMonitorRpcResult =
  | {
      readonly accepted: true;
      readonly acknowledgement: {
        readonly accepted: true;
        readonly incident_id: string | null;
        readonly generation: number | null;
        readonly lifecycle_state: string | null;
        readonly action: string;
        readonly occurrence_count: number;
        readonly state_version: number | null;
      };
    }
  | {
      readonly accepted: false;
      readonly errorCode: StatusMonitorRpcErrorCode;
    };

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

export type MonitoringCycleReason =
  | "account_rejected"
  | "discovery_failed"
  | "not_configured"
  | "unknown";

/**
 * Wire body for a whole-cycle monitoring failure. Deliberately carries a
 * closed reason enum and no free-text error: cycle errors embed upstream
 * response fragments, which stay on the monitor's own dashboard/health
 * surfaces instead of crossing the trust boundary.
 */
export interface MonitoringCycleAlertEvent {
  readonly schema_version: 1;
  readonly event_id: string;
  readonly alert_type: "monitoring_cycle_failure";
  readonly fingerprint: string;
  readonly status: ModelUnavailableStatus;
  readonly severity: "critical" | "info";
  readonly title: string;
  readonly occurred_at: string;
  readonly summary: string;
  readonly context: { readonly reason?: MonitoringCycleReason };
  readonly evidence_refs: readonly string[];
}

export interface StatusMonitorControlPlaneService {
  submitStatusMonitorEvent(
    bodyJson: string,
    deploymentId: string,
  ): Promise<StatusMonitorRpcResult>;
}

export interface PendingCanonicalEvent {
  /** Which durable pipeline owns the row: per-model incidents or the cycle. */
  readonly kind: "model" | "cycle";
  readonly eventId: string;
  readonly fingerprint: string;
  /** Empty for `kind: "cycle"` — filter by kind before using it. */
  readonly modelId: string;
  readonly status: ModelUnavailableStatus;
  readonly bodyJson: string;
}

export class ControlPlanePreparationError extends Error {
  constructor(
    code:
      | "drain_ownership_corrupt"
      | "pending_event_corrupt"
      | "pending_state_conflict",
  ) {
    super(code);
    this.name = "ControlPlanePreparationError";
  }
}

const OWNER_KEY_PREFIX = "alert_delivery_owner:v1:";
const PENDING_KEY_PREFIX = "alert_delivery_pending:v1:";
const EVENT_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const CLOUDFLARE_VERSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RPC_ERROR_CODES = new Set<StatusMonitorRpcErrorCode>([
  "control_plane_dormant",
  "control_plane_unavailable",
  "deployment_mismatch",
  "event_id_conflict",
  "invalid_event",
  "invalid_producer_identity",
  "retired_deployment",
  "unknown_deployment",
]);
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

/** Fixed incident fingerprint for the whole-cycle monitoring failure. */
export const CYCLE_FINGERPRINT = "status-monitor:cycle";
const CYCLE_CONTEXT_KEYS = new Set(["reason"]);
const CYCLE_REASONS = new Set<MonitoringCycleReason>([
  "account_rejected",
  "discovery_failed",
  "not_configured",
  "unknown",
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

export async function readDrainOwner(
  db: D1Database,
  fingerprint: string,
): Promise<AlertDeliveryOwner | null> {
  const stored = await readMeta(db, metaKey(OWNER_KEY_PREFIX, fingerprint));
  if (stored === null || stored === "legacy" || stored === "control-plane") {
    return stored;
  }
  throw new ControlPlanePreparationError("drain_ownership_corrupt");
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
  const stored = await readDrainOwner(db, fingerprint);
  if (stored !== null) {
    return stored;
  }
  const owner = status === "resolved" ? "legacy" : defaultOwner;
  await writeMeta(db, key, owner);
  return owner;
}

export async function releaseDrainOwner(db: D1Database, fingerprint: string): Promise<void> {
  await deleteMeta(db, metaKey(OWNER_KEY_PREFIX, fingerprint));
}

export async function hasControlPlaneDrainOwner(db: D1Database): Promise<boolean> {
  const row = await db
    .prepare(
      `SELECT 1 AS present FROM meta WHERE key GLOB ? AND value = ? LIMIT 1`,
    )
    .bind(`${OWNER_KEY_PREFIX}*`, "control-plane")
    .first<{ present: number }>();
  return row !== null;
}

export function prepareDrainOwnerRelease(
  db: D1Database,
  fingerprint: string,
): D1PreparedStatement {
  return db.prepare(`DELETE FROM meta WHERE key = ?`).bind(metaKey(OWNER_KEY_PREFIX, fingerprint));
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

/**
 * Classify a cycle failure without shipping the raw error text. Order matters:
 * "PROBER_API_KEY not set" would otherwise match the account patterns.
 */
export function cycleFailureReason(error: string | null): MonitoringCycleReason {
  if (error === null) return "unknown";
  if (/PROBER_API_KEY not set/i.test(error)) return "not_configured";
  if (/\b(?:401|403|429)\b|api key|verif|quota|account-wide/i.test(error)) {
    return "account_rejected";
  }
  if (/discovery failed|\/models response|probe targets/i.test(error)) {
    return "discovery_failed";
  }
  return "unknown";
}

/** Translate one cycle transition into the platform-neutral wire contract. */
export function monitoringCycleEvent(
  cycle: CycleStatus,
  transition: ModelUnavailableStatus,
  eventId: string = crypto.randomUUID(),
): MonitoringCycleAlertEvent {
  const firing = transition === "firing";
  return {
    schema_version: 1,
    event_id: eventId,
    alert_type: "monitoring_cycle_failure",
    fingerprint: CYCLE_FINGERPRINT,
    status: transition,
    severity: firing ? "critical" : "info",
    title: firing ? "Monitoring cycle failing" : "Monitoring cycle recovered",
    occurred_at: occurredAt(cycle.checkedAt ?? ""),
    summary: firing
      ? "The monitoring cycle failed; no models could be probed."
      : "The monitoring cycle recovered.",
    context: firing ? { reason: cycleFailureReason(cycle.error) } : {},
    evidence_refs: [],
  };
}

export function modelUnavailableDepartureEvent(
  modelId: string,
  checkedAt: string,
  eventId: string = crypto.randomUUID(),
): ModelUnavailableAlertEvent {
  return {
    schema_version: 1,
    event_id: eventId,
    alert_type: "model_unavailable",
    fingerprint: modelAlertFingerprint(modelId),
    status: "resolved",
    severity: "info",
    title: `Model monitoring ended: ${modelId}`,
    occurred_at: occurredAt(checkedAt),
    summary: `${modelId} is no longer present in the monitored model catalog.`,
    context: {
      model_id: modelId,
    },
    evidence_refs: [],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function nullableInteger(value: unknown): boolean {
  return value === null || Number.isSafeInteger(value);
}

export function isStatusMonitorRpcResult(value: unknown): value is StatusMonitorRpcResult {
  if (!isRecord(value) || typeof value.accepted !== "boolean") return false;
  if (value.accepted === false) {
    return (
      Object.keys(value).length === 2 &&
      typeof value.errorCode === "string" &&
      RPC_ERROR_CODES.has(value.errorCode as StatusMonitorRpcErrorCode)
    );
  }
  if (Object.keys(value).length !== 2 || !isRecord(value.acknowledgement)) return false;
  const acknowledgement = value.acknowledgement;
  return (
    Object.keys(acknowledgement).length === 7 &&
    acknowledgement.accepted === true &&
    (acknowledgement.incident_id === null ||
      validBoundedString(acknowledgement.incident_id, 256)) &&
    nullableInteger(acknowledgement.generation) &&
    (acknowledgement.lifecycle_state === null ||
      validBoundedString(acknowledgement.lifecycle_state, 64)) &&
    validBoundedString(acknowledgement.action, 64) &&
    Number.isSafeInteger(acknowledgement.occurrence_count) &&
    (acknowledgement.occurrence_count as number) >= 0 &&
    nullableInteger(acknowledgement.state_version)
  );
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
      !isRecord(body.context)
    ) {
      throw new Error("invalid event fields");
    }
    if (body.alert_type === "monitoring_cycle_failure") {
      if (
        fingerprint !== CYCLE_FINGERPRINT ||
        !hasExactKeys(body.context, CYCLE_CONTEXT_KEYS) ||
        (status === "firing" &&
          (body.severity !== "critical" ||
            !CYCLE_REASONS.has(body.context.reason as MonitoringCycleReason))) ||
        (status === "resolved" &&
          (body.severity !== "info" || body.context.reason !== undefined))
      ) {
        throw new Error("invalid cycle event");
      }
      return {
        kind: "cycle",
        eventId: body.event_id,
        fingerprint,
        modelId: "",
        status,
        bodyJson: stored.body_json,
      };
    }
    if (
      body.alert_type !== "model_unavailable" ||
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
      kind: "model",
      eventId: body.event_id,
      fingerprint,
      modelId: body.context.model_id,
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
  candidate: ModelUnavailableAlertEvent | MonitoringCycleAlertEvent,
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

function parseListedPending(key: string, raw: string): PendingCanonicalEvent {
  try {
    const stored = JSON.parse(raw) as unknown;
    if (!isRecord(stored) || typeof stored.body_json !== "string") {
      throw new Error("invalid wrapper");
    }
    const body = JSON.parse(stored.body_json) as unknown;
    if (
      !isRecord(body) ||
      typeof body.fingerprint !== "string" ||
      (body.status !== "firing" && body.status !== "resolved")
    ) {
      throw new Error("invalid event identity");
    }
    const pending = parsePending(raw, body.fingerprint, body.status);
    if (key !== metaKey(PENDING_KEY_PREFIX, pending.fingerprint, pending.status)) {
      throw new Error("pending key mismatch");
    }
    return pending;
  } catch (error) {
    if (error instanceof ControlPlanePreparationError) throw error;
    throw new ControlPlanePreparationError("pending_event_corrupt");
  }
}

/**
 * Count durable transitions still awaiting Control Plane acceptance.
 *
 * Deliberately a raw count with no parsing: the health endpoint must be able to
 * observe a stuck pipeline even when a stored row is corrupt (where
 * {@link listPendingCanonicalEvents} would throw). A row is created before each
 * submission and deleted in the post-delivery batch, so a count that stays
 * above zero across cycles (~20 minutes apart) means the Control Plane keeps
 * rejecting or is unreachable — the log-only rejection reports otherwise
 * require `wrangler tail` to see.
 */
export async function countPendingCanonicalEvents(db: D1Database): Promise<number> {
  const row = await db
    .prepare(`SELECT COUNT(*) AS n FROM meta WHERE key GLOB ?`)
    .bind(`${PENDING_KEY_PREFIX}*`)
    .first<{ n: number }>();
  return row?.n ?? 0;
}

/** Returns every durable transition that must be retried before new edges. */
export async function listPendingCanonicalEvents(
  db: D1Database,
): Promise<PendingCanonicalEvent[]> {
  const result = await db
    .prepare(`SELECT key, value FROM meta WHERE key GLOB ? ORDER BY key`)
    .bind(`${PENDING_KEY_PREFIX}*`)
    .all<{ key: string; value: string }>();
  return result.results.map((row) => parseListedPending(row.key, row.value));
}

/**
 * Log why a submission did not land.
 *
 * Because there is no relay/webhook fallback, an unreported rejection is a
 * silent alerting outage: the pending body is retried forever while nothing
 * reaches Slack. Only the fingerprint and a closed-set reason code are emitted —
 * the fingerprint is a bounded, control-character-free model identifier, and the
 * canonical body is never logged.
 */
function reportUndelivered(pending: PendingCanonicalEvent, reason: string): void {
  console.error(
    `alert control plane did not accept ${pending.status} ${pending.fingerprint}: ${reason}`,
  );
}

/**
 * Submit only to the control plane. Failure keeps the pending body for an
 * idempotent retry and deliberately has no relay/webhook fallback.
 */
export async function submitPendingCanonicalEvent(
  service: StatusMonitorControlPlaneService | undefined,
  pending: PendingCanonicalEvent,
  versionMetadata: WorkerVersionMetadata | undefined,
): Promise<boolean> {
  const deploymentId = versionMetadata?.id;
  if (service === undefined) {
    reportUndelivered(pending, "service_binding_missing");
    return false;
  }
  if (typeof deploymentId !== "string" || !CLOUDFLARE_VERSION_ID_RE.test(deploymentId)) {
    reportUndelivered(pending, "deployment_identity_invalid");
    return false;
  }
  try {
    const result: unknown = await service.submitStatusMonitorEvent(
      pending.bodyJson,
      deploymentId,
    );
    if (!isStatusMonitorRpcResult(result)) {
      reportUndelivered(pending, "malformed_rpc_result");
      return false;
    }
    if (!result.accepted) {
      // `errorCode` is a closed enum of stable codes (RPC_ERROR_CODES), so it
      // carries no producer data and is safe to log verbatim.
      reportUndelivered(pending, result.errorCode);
      return false;
    }
    return true;
  } catch {
    reportUndelivered(pending, "service_binding_threw");
    return false;
  }
}

export async function completePendingCanonicalEvent(
  db: D1Database,
  pending: PendingCanonicalEvent,
): Promise<void> {
  await db.batch(preparePendingCanonicalEventCompletion(db, pending));
}

export function preparePendingCanonicalEventCompletion(
  db: D1Database,
  pending: PendingCanonicalEvent,
): D1PreparedStatement[] {
  const statements = [
    db
      .prepare(`DELETE FROM meta WHERE key = ?`)
      .bind(metaKey(PENDING_KEY_PREFIX, pending.fingerprint, pending.status)),
  ];
  if (pending.status === "resolved") {
    statements.push(prepareDrainOwnerRelease(db, pending.fingerprint));
  }
  return statements;
}
