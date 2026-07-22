import { deliveryRefEquals, parseDeliveryRef } from "./notification";
import type { CanonicalAlertEnvelope } from "./types";
import {
  type AnalysisJob,
  type ApplyEventAcknowledgement,
  computeDesiredAlarmAt,
  type IncidentGeneration,
  type IncidentStore,
  type LifecycleOrder,
  type NextGenerationCandidate,
  type PendingAction,
  type PendingActionType,
  refreshDesiredAlarmAt,
  type ReceiptAction,
} from "./store";

export class EventIdConflictError extends Error {
  readonly code = "event_id_conflict";
  readonly statusCode = 409;

  constructor(eventId: string) {
    super(`event_id ${eventId} was already accepted with a different canonical digest`);
    this.name = "EventIdConflictError";
  }
}

export class IncidentInvariantError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "IncidentInvariantError";
  }
}

export type ControlPlaneIdKind = "incident" | "action" | "job";

export interface IncidentStateMachineOptions {
  idFactory?: (kind: ControlPlaneIdKind, material: string) => string;
  repeatUpdateDelayMs?: number;
  actionDeadlineMs?: number;
  analysisDeadlineMs?: number;
}

const DEFAULT_REPEAT_UPDATE_DELAY_MS = 5_000;
const DEFAULT_ACTION_DEADLINE_MS = 30 * 60_000;
const DEFAULT_ANALYSIS_DEADLINE_MS = 20 * 60_000;

function defaultIdFactory(kind: ControlPlaneIdKind): string {
  return `${kind}_${crypto.randomUUID()}`;
}

function compareUtf8(left: string, right: string): number {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  const length = Math.min(leftBytes.length, rightBytes.length);
  for (let index = 0; index < length; index += 1) {
    if (leftBytes[index] !== rightBytes[index]) return leftBytes[index] - rightBytes[index];
  }
  return leftBytes.length - rightBytes.length;
}

export function lifecycleOrder(envelope: CanonicalAlertEnvelope): LifecycleOrder {
  const occurredAtMs = Date.parse(envelope.event.occurred_at);
  if (!Number.isFinite(occurredAtMs)) {
    throw new TypeError("occurred_at must be a valid RFC3339 timestamp");
  }
  return {
    occurredAt: envelope.event.occurred_at,
    occurredAtMs,
    statusPrecedence: envelope.event.status === "resolved" ? 1 : 0,
    eventId: envelope.event.event_id,
  };
}

export function compareLifecycleOrder(left: LifecycleOrder, right: LifecycleOrder): number {
  if (left.occurredAtMs !== right.occurredAtMs) {
    return left.occurredAtMs - right.occurredAtMs;
  }
  if (left.statusPrecedence !== right.statusPrecedence) {
    return left.statusPrecedence - right.statusPrecedence;
  }
  return compareUtf8(left.eventId, right.eventId);
}

function parentPayload(generation: IncidentGeneration): Record<string, unknown> {
  return {
    incident_id: generation.incidentId,
    generation: generation.generation,
    state_version: generation.stateVersion,
    occurrence_count: generation.occurrenceCount,
    first_seen: generation.firstSeen,
    last_seen: generation.lastSeen,
    envelope: generation.latestEnvelope,
  };
}

function updatePayload(generation: IncidentGeneration): Record<string, unknown> {
  return {
    ...parentPayload(generation),
    delivery_ref: generation.deliveryRef,
  };
}

function recoveryPayload(generation: IncidentGeneration): Record<string, unknown> {
  return {
    incident_id: generation.incidentId,
    generation: generation.generation,
    state_version: generation.stateVersion,
    resolution_epoch: generation.resolutionEpoch,
    occurrence_count: generation.occurrenceCount,
    first_seen: generation.firstSeen,
    last_seen: generation.lastSeen,
    delivery_ref: generation.deliveryRef,
    envelope: generation.resolutionEnvelope,
  };
}

function quotaReservationPayload(
  generation: IncidentGeneration,
): Record<string, unknown> {
  return {
    environment: generation.latestEnvelope.trusted.environment,
    principal: generation.latestEnvelope.trusted.principal,
    incident_id: generation.incidentId,
    generation: generation.generation,
  };
}

function quotaReleasePayload(generation: IncidentGeneration): Record<string, unknown> {
  return {
    ...quotaReservationPayload(generation),
    lease_epoch: generation.quotaLeaseEpoch,
  };
}

export class IncidentStateMachine {
  private readonly idFactory: (kind: ControlPlaneIdKind, material: string) => string;
  private readonly repeatUpdateDelayMs: number;
  private readonly actionDeadlineMs: number;
  private readonly analysisDeadlineMs: number;

  constructor(
    readonly store: IncidentStore,
    options: IncidentStateMachineOptions = {},
  ) {
    this.idFactory = options.idFactory ?? defaultIdFactory;
    this.repeatUpdateDelayMs =
      options.repeatUpdateDelayMs ?? DEFAULT_REPEAT_UPDATE_DELAY_MS;
    this.actionDeadlineMs = options.actionDeadlineMs ?? DEFAULT_ACTION_DEADLINE_MS;
    this.analysisDeadlineMs = options.analysisDeadlineMs ?? DEFAULT_ANALYSIS_DEADLINE_MS;
  }

  applyEvent(
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    if (bodyDigest.length === 0) throw new TypeError("bodyDigest must not be empty");
    if (!Number.isFinite(receivedAtMs)) throw new TypeError("receivedAtMs must be finite");
    const order = lifecycleOrder(envelope);

    return this.store.transaction(() => {
      const duplicate = this.store.getReceipt(envelope.event.event_id);
      if (duplicate) {
        if (duplicate.bodyDigest !== bodyDigest) {
          throw new EventIdConflictError(envelope.event.event_id);
        }
        return duplicate.acknowledgement;
      }

      const scheduler = this.store.getSchedulerState();
      if (
        scheduler.lifecycleHighWatermark &&
        compareLifecycleOrder(order, scheduler.lifecycleHighWatermark) <= 0
      ) {
        const latest = this.store.getLatestGeneration();
        return this.recordReceipt(
          envelope,
          bodyDigest,
          receivedAtMs,
          "stale",
          latest?.incidentId ?? null,
          latest?.generation ?? null,
          latest?.state ?? null,
          latest?.occurrenceCount ?? 0,
          latest?.stateVersion ?? null,
        );
      }

      const latest = this.store.getLatestGeneration();
      let acknowledgement: ApplyEventAcknowledgement;
      if (!latest || latest.state === "resolved") {
        acknowledgement =
          envelope.event.status === "resolved"
            ? this.applyOrphanResolution(envelope, bodyDigest, order, receivedAtMs)
            : this.openGeneration(envelope, bodyDigest, order, receivedAtMs);
      } else if (latest.state === "suppressed") {
        acknowledgement =
          envelope.event.status === "firing"
            ? this.applySuppressedRepeat(
                latest,
                envelope,
                bodyDigest,
                order,
                receivedAtMs,
              )
            : this.resolveSuppressed(
                latest,
                envelope,
                bodyDigest,
                order,
                receivedAtMs,
              );
      } else if (latest.state === "resolving") {
        acknowledgement =
          envelope.event.status === "firing"
            ? this.applyFiringWhileResolving(
                latest,
                envelope,
                bodyDigest,
                order,
                receivedAtMs,
              )
            : this.applyResolutionWhileResolving(
                latest,
                envelope,
                bodyDigest,
                order,
                receivedAtMs,
              );
      } else {
        acknowledgement =
          envelope.event.status === "firing"
            ? this.applyRepeat(latest, envelope, bodyDigest, order, receivedAtMs)
            : this.beginResolution(latest, envelope, bodyDigest, order, receivedAtMs);
      }

      this.setLifecycleHighWatermark(order);
      refreshDesiredAlarmAt(this.store);
      return acknowledgement;
    });
  }

  onActionCompleted(
    action: PendingAction,
    result: Record<string, unknown>,
    nowMs: number,
  ): void {
    const generation = this.store.getGeneration(action.generation);
    if (!generation || generation.incidentId !== action.incidentId) return;

    if (action.type === "post_parent") {
      if (generation.quotaState !== "confirmed") {
        throw new IncidentInvariantError(
          "post_parent cannot complete without confirmed principal quota",
        );
      }
      const receipt = (result as { receipt?: unknown }).receipt;
      if (receipt === null || typeof receipt !== "object") {
        throw new IncidentInvariantError("post_parent success must include a delivery receipt");
      }
      const deliveryRef = parseDeliveryRef((receipt as { deliveryRef: unknown }).deliveryRef);
      if (generation.deliveryRef && !deliveryRefEquals(generation.deliveryRef, deliveryRef)) {
        throw new IncidentInvariantError("parent action returned a different delivery reference");
      }
      generation.deliveryRef = deliveryRef;
      if (generation.state === "opening") generation.state = "firing";
      generation.stateVersion += 1;
      generation.updatedAtMs = nowMs;
      this.store.putGeneration(generation);
      this.unblockParentDependents(generation, action.actionId, nowMs);
      this.queueAnalysis(generation, nowMs);
      return;
    }

    if (action.type === "post_recovery") {
      if (
        generation.state !== "resolving" ||
        action.resolutionEpoch !== generation.resolutionEpoch
      ) {
        return;
      }
      const candidate = generation.nextGenerationCandidate;
      generation.state = "resolved";
      generation.stateVersion += 1;
      generation.nextGenerationCandidate = null;
      generation.updatedAtMs = nowMs;
      this.store.putGeneration(generation);
      const release = this.queueQuotaRelease(generation, nowMs);
      if (candidate) this.materializeCandidate(candidate, nowMs, release?.actionId ?? null);
      return;
    }

    if (action.type === "reserve_quota") {
      if (generation.quotaState !== "pending") return;
      if (result.admitted === true) {
        const leaseEpoch = result.lease_epoch;
        if (!Number.isSafeInteger(leaseEpoch) || Number(leaseEpoch) < 1) {
          throw new IncidentInvariantError(
            "quota admission success must include a positive lease_epoch",
          );
        }
        generation.quotaState = "confirmed";
        generation.quotaLeaseEpoch = Number(leaseEpoch);
        generation.stateVersion += 1;
        generation.updatedAtMs = nowMs;
        this.store.putGeneration(generation);
        this.unblockQuotaDependentParent(generation, action.actionId, nowMs);
        return;
      }
      if (result.admitted === false) {
        this.suppressForQuota(generation, "principal active quota exceeded", nowMs);
        return;
      }
      throw new IncidentInvariantError(
        "quota admission result must include an admitted boolean",
      );
    }

    if (action.type === "release_quota") {
      if (generation.quotaState !== "confirmed") return;
      generation.quotaState = "released";
      generation.stateVersion += 1;
      generation.updatedAtMs = nowMs;
      this.store.putGeneration(generation);
      this.unblockQuotaReleaseDependents(action.actionId, nowMs);
      return;
    }

    if (action.type === "dispatch_analysis") {
      const jobId = action.payload.job_id;
      if (typeof jobId !== "string") return;
      const job = this.store.getAnalysisJob(jobId);
      if (!job || job.generation !== action.generation || job.status !== "queued") return;
      job.status = "dispatched";
      job.version += 1;
      job.githubRunId =
        typeof result.githubRunId === "string" ? result.githubRunId : job.githubRunId;
      job.updatedAtMs = nowMs;
      this.store.putAnalysisJob(job);
    }
  }

  onActionTerminal(action: PendingAction, error: string, nowMs: number): void {
    if (action.type === "reserve_quota") {
      const generation = this.store.getGeneration(action.generation);
      if (
        generation &&
        generation.incidentId === action.incidentId &&
        generation.quotaState === "pending"
      ) {
        this.suppressForQuota(generation, `quota admission failed: ${error}`, nowMs);
      }
      return;
    }
    if (action.type !== "dispatch_analysis") return;
    const jobId = action.payload.job_id;
    if (typeof jobId !== "string") return;
    const job = this.store.getAnalysisJob(jobId);
    if (!job || job.generation !== action.generation) return;
    if (job.status === "succeeded" || job.status === "failed") return;
    job.status = "failed";
    job.version += 1;
    job.deadlineAtMs = null;
    job.leaseExpiresAtMs = null;
    job.lastError = error;
    job.updatedAtMs = nowMs;
    this.store.putAnalysisJob(job);
  }

  private applyOrphanResolution(
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    _order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    return this.recordReceipt(
      envelope,
      bodyDigest,
      receivedAtMs,
      "orphan_resolution",
      null,
      null,
      null,
      0,
      null,
    );
  }

  private openGeneration(
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    const generationNumber = this.nextGenerationNumber();
    const incidentId = this.newId("incident", `generation:${generationNumber}`);
    const generation: IncidentGeneration = {
      incidentId,
      generation: generationNumber,
      state: "opening",
      stateVersion: 1,
      resolutionEpoch: 0,
      quotaState: "pending",
      quotaLeaseEpoch: null,
      occurrenceCount: 1,
      firstSeen: envelope.event.occurred_at,
      lastSeen: envelope.event.occurred_at,
      highWatermark: order,
      latestEnvelope: envelope,
      resolutionEnvelope: null,
      deliveryRef: null,
      nextGenerationCandidate: null,
      createdAtMs: receivedAtMs,
      updatedAtMs: receivedAtMs,
    };
    this.store.putGeneration(generation);
    const releaseFence = this.latestQuotaReleaseFence();
    const quota = this.putNewAction(
      generation,
      "reserve_quota",
      quotaReservationPayload(generation),
      releaseFence ? "blocked" : "pending",
      receivedAtMs,
      receivedAtMs,
      releaseFence?.actionId ?? null,
    );
    this.putNewAction(
      generation,
      "post_parent",
      parentPayload(generation),
      "blocked",
      receivedAtMs,
      receivedAtMs,
      quota.actionId,
    );
    this.putAnalysisJob(generation, receivedAtMs);
    return this.recordReceipt(
      envelope,
      bodyDigest,
      receivedAtMs,
      "opened",
      incidentId,
      generationNumber,
      generation.state,
      1,
      generation.stateVersion,
    );
  }

  private applyRepeat(
    generation: IncidentGeneration,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    generation.occurrenceCount += 1;
    generation.stateVersion += 1;
    generation.lastSeen = envelope.event.occurred_at;
    generation.latestEnvelope = envelope;
    generation.highWatermark = order;
    generation.updatedAtMs = receivedAtMs;
    this.store.putGeneration(generation);
    this.coalesceParentRefresh(generation, receivedAtMs);
    return this.recordReceiptForGeneration(
      generation,
      envelope,
      bodyDigest,
      receivedAtMs,
      "repeated",
    );
  }

  private applySuppressedRepeat(
    generation: IncidentGeneration,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    generation.occurrenceCount += 1;
    generation.stateVersion += 1;
    generation.lastSeen = envelope.event.occurred_at;
    generation.latestEnvelope = envelope;
    generation.highWatermark = order;
    generation.updatedAtMs = receivedAtMs;
    this.store.putGeneration(generation);
    return this.recordReceiptForGeneration(
      generation,
      envelope,
      bodyDigest,
      receivedAtMs,
      "quota_suppressed",
    );
  }

  private resolveSuppressed(
    generation: IncidentGeneration,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    generation.state = "resolved";
    generation.stateVersion += 1;
    generation.resolutionEpoch += 1;
    generation.lastSeen = envelope.event.occurred_at;
    generation.latestEnvelope = envelope;
    generation.resolutionEnvelope = envelope;
    generation.highWatermark = order;
    generation.updatedAtMs = receivedAtMs;
    this.store.putGeneration(generation);
    return this.recordReceiptForGeneration(
      generation,
      envelope,
      bodyDigest,
      receivedAtMs,
      "resolved_suppressed",
    );
  }

  private beginResolution(
    generation: IncidentGeneration,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    generation.state = "resolving";
    generation.stateVersion += 1;
    generation.resolutionEpoch += 1;
    generation.lastSeen = envelope.event.occurred_at;
    generation.latestEnvelope = envelope;
    generation.resolutionEnvelope = envelope;
    generation.highWatermark = order;
    generation.updatedAtMs = receivedAtMs;
    this.store.putGeneration(generation);
    const updateFence = this.prepareParentUpdatesForResolution(generation, receivedAtMs);
    this.upsertRecoveryAction(generation, receivedAtMs, updateFence);
    return this.recordReceiptForGeneration(
      generation,
      envelope,
      bodyDigest,
      receivedAtMs,
      "resolving",
    );
  }

  private applyFiringWhileResolving(
    generation: IncidentGeneration,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    const recovery = this.recoveryAction(generation.generation);
    if (!recovery) {
      throw new IncidentInvariantError("resolving generation is missing its recovery action");
    }

    if (
      (recovery.status === "pending" || recovery.status === "blocked") &&
      generation.nextGenerationCandidate === null
    ) {
      recovery.status = "cancelled";
      recovery.version += 1;
      recovery.nextRunAtMs = null;
      recovery.reconcileAtMs = null;
      recovery.leaseExpiresAtMs = null;
      recovery.lastError = "cancelled by a newer firing event before claim";
      recovery.updatedAtMs = receivedAtMs;
      this.store.putAction(recovery);

      generation.state = generation.deliveryRef ? "firing" : "opening";
      generation.stateVersion += 1;
      generation.resolutionEpoch += 1;
      generation.occurrenceCount += 1;
      generation.lastSeen = envelope.event.occurred_at;
      generation.latestEnvelope = envelope;
      generation.resolutionEnvelope = null;
      generation.nextGenerationCandidate = null;
      generation.highWatermark = order;
      generation.updatedAtMs = receivedAtMs;
      this.store.putGeneration(generation);
      this.coalesceParentRefresh(generation, receivedAtMs);
      return this.recordReceiptForGeneration(
        generation,
        envelope,
        bodyDigest,
        receivedAtMs,
        "reopened",
      );
    }

    let candidate = generation.nextGenerationCandidate;
    if (candidate) {
      candidate.envelope = envelope;
      candidate.lastSeen = envelope.event.occurred_at;
      candidate.occurrenceCount += 1;
      candidate.highWatermark = order;
    } else {
      const candidateGeneration = this.nextGenerationNumber();
      candidate = {
        incidentId: this.newId("incident", `generation:${candidateGeneration}`),
        generation: candidateGeneration,
        envelope,
        firstSeen: envelope.event.occurred_at,
        lastSeen: envelope.event.occurred_at,
        occurrenceCount: 1,
        highWatermark: order,
      };
    }
    generation.nextGenerationCandidate = candidate;
    generation.stateVersion += 1;
    generation.highWatermark = order;
    generation.updatedAtMs = receivedAtMs;
    this.store.putGeneration(generation);
    return this.recordReceipt(
      envelope,
      bodyDigest,
      receivedAtMs,
      "queued_next_generation",
      candidate.incidentId,
      candidate.generation,
      "resolving",
      candidate.occurrenceCount,
      generation.stateVersion,
    );
  }

  private applyResolutionWhileResolving(
    generation: IncidentGeneration,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    order: LifecycleOrder,
    receivedAtMs: number,
  ): ApplyEventAcknowledgement {
    const cancelledCandidate = generation.nextGenerationCandidate !== null;
    generation.nextGenerationCandidate = null;
    generation.stateVersion += 1;
    generation.lastSeen = envelope.event.occurred_at;
    generation.latestEnvelope = envelope;
    generation.resolutionEnvelope = envelope;
    generation.highWatermark = order;
    generation.updatedAtMs = receivedAtMs;
    this.store.putGeneration(generation);

    const recovery = this.recoveryAction(generation.generation);
    if (recovery && (recovery.status === "pending" || recovery.status === "blocked")) {
      recovery.payload = recoveryPayload(generation);
      recovery.version += 1;
      recovery.stateVersion = generation.stateVersion;
      recovery.updatedAtMs = receivedAtMs;
      this.store.putAction(recovery);
    }
    return this.recordReceiptForGeneration(
      generation,
      envelope,
      bodyDigest,
      receivedAtMs,
      cancelledCandidate ? "cancelled_next_generation" : "resolving",
    );
  }

  private materializeCandidate(
    candidate: NextGenerationCandidate,
    nowMs: number,
    quotaDependency: string | null,
  ): void {
    if (this.store.getGeneration(candidate.generation)) {
      throw new IncidentInvariantError("next generation candidate was already materialized");
    }
    const generation: IncidentGeneration = {
      incidentId: candidate.incidentId,
      generation: candidate.generation,
      state: "opening",
      stateVersion: 1,
      resolutionEpoch: 0,
      quotaState: "pending",
      quotaLeaseEpoch: null,
      occurrenceCount: candidate.occurrenceCount,
      firstSeen: candidate.firstSeen,
      lastSeen: candidate.lastSeen,
      highWatermark: candidate.highWatermark,
      latestEnvelope: candidate.envelope,
      resolutionEnvelope: null,
      deliveryRef: null,
      nextGenerationCandidate: null,
      createdAtMs: nowMs,
      updatedAtMs: nowMs,
    };
    this.store.putGeneration(generation);
    const quota = this.putNewAction(
      generation,
      "reserve_quota",
      quotaReservationPayload(generation),
      quotaDependency ? "blocked" : "pending",
      nowMs,
      nowMs,
      quotaDependency,
    );
    this.putNewAction(
      generation,
      "post_parent",
      parentPayload(generation),
      "blocked",
      nowMs,
      nowMs,
      quota.actionId,
    );
    this.putAnalysisJob(generation, nowMs);
  }

  private unblockQuotaDependentParent(
    generation: IncidentGeneration,
    quotaActionId: string,
    nowMs: number,
  ): void {
    const parent = this.store
      .listActions()
      .find(
        (action) =>
          action.generation === generation.generation &&
          action.type === "post_parent" &&
          action.status === "blocked" &&
          action.dependsOnActionId === quotaActionId,
      );
    if (!parent) {
      throw new IncidentInvariantError(
        "quota admission is missing its blocked parent action",
      );
    }
    parent.status = "pending";
    parent.version += 1;
    parent.stateVersion = generation.stateVersion;
    parent.dependsOnActionId = null;
    parent.payload = parentPayload(generation);
    parent.nextRunAtMs = nowMs;
    parent.finalDeadlineAtMs = nowMs + this.actionDeadlineMs;
    parent.updatedAtMs = nowMs;
    this.store.putAction(parent);
  }

  private suppressForQuota(
    generation: IncidentGeneration,
    error: string,
    nowMs: number,
  ): void {
    generation.quotaState = "suppressed";
    generation.quotaLeaseEpoch = null;
    generation.state = generation.resolutionEnvelope ? "resolved" : "suppressed";
    generation.stateVersion += 1;
    generation.updatedAtMs = nowMs;
    this.store.putGeneration(generation);

    for (const action of this.store.listActions()) {
      if (
        action.generation !== generation.generation ||
        action.type === "reserve_quota" ||
        (action.status !== "pending" && action.status !== "blocked")
      ) {
        continue;
      }
      action.status = "cancelled";
      action.version += 1;
      action.dependsOnActionId = null;
      action.nextRunAtMs = null;
      action.reconcileAtMs = null;
      action.leaseExpiresAtMs = null;
      action.lastError = error;
      action.updatedAtMs = nowMs;
      this.store.putAction(action);
    }

    const job = this.store
      .listAnalysisJobs()
      .find((candidate) => candidate.generation === generation.generation);
    if (job && job.status !== "succeeded" && job.status !== "failed") {
      job.status = "failed";
      job.version += 1;
      job.deadlineAtMs = null;
      job.leaseExpiresAtMs = null;
      job.lastError = error;
      job.updatedAtMs = nowMs;
      this.store.putAnalysisJob(job);
    }
  }

  private queueQuotaRelease(
    generation: IncidentGeneration,
    nowMs: number,
  ): PendingAction | null {
    if (generation.quotaState !== "confirmed") return null;
    if (
      generation.quotaLeaseEpoch === null ||
      !Number.isSafeInteger(generation.quotaLeaseEpoch) ||
      generation.quotaLeaseEpoch < 1
    ) {
      throw new IncidentInvariantError(
        "confirmed principal quota is missing its lease epoch",
      );
    }
    const existing = this.store
      .listActions()
      .find(
        (action) =>
          action.generation === generation.generation &&
          action.type === "release_quota",
      );
    if (existing) return existing;
    return this.putNewAction(
      generation,
      "release_quota",
      quotaReleasePayload(generation),
      "pending",
      nowMs,
      nowMs,
      null,
    );
  }

  private latestQuotaReleaseFence(): PendingAction | null {
    return (
      this.store
        .listActions()
        .filter(
          (action) =>
            action.type === "release_quota" &&
            action.status !== "completed" &&
            action.status !== "cancelled",
        )
        .at(-1) ?? null
    );
  }

  private unblockQuotaReleaseDependents(
    releaseActionId: string,
    nowMs: number,
  ): void {
    for (const action of this.store.listActions()) {
      if (
        action.type !== "reserve_quota" ||
        action.status !== "blocked" ||
        action.dependsOnActionId !== releaseActionId
      ) {
        continue;
      }
      action.status = "pending";
      action.version += 1;
      action.dependsOnActionId = null;
      action.nextRunAtMs = nowMs;
      action.finalDeadlineAtMs = nowMs + this.actionDeadlineMs;
      action.updatedAtMs = nowMs;
      this.store.putAction(action);
    }
  }

  private coalesceParentRefresh(generation: IncidentGeneration, nowMs: number): void {
    const parent = this.store
      .listActions()
      .find(
        (action) =>
          action.generation === generation.generation && action.type === "post_parent",
      );
    if (!parent) throw new IncidentInvariantError("active generation is missing parent action");

    if (
      generation.state === "opening" &&
      (parent.status === "pending" || parent.status === "blocked")
    ) {
      parent.payload = parentPayload(generation);
      parent.version += 1;
      parent.stateVersion = generation.stateVersion;
      parent.updatedAtMs = nowMs;
      this.store.putAction(parent);
      return;
    }

    const coalescible = this.store
      .listActions()
      .filter(
        (action) =>
          action.generation === generation.generation &&
          action.type === "update_parent" &&
          (action.status === "pending" || action.status === "blocked"),
      )
      .at(-1);
    if (coalescible) {
      coalescible.payload = updatePayload(generation);
      coalescible.version += 1;
      coalescible.stateVersion = generation.stateVersion;
      coalescible.updatedAtMs = nowMs;
      this.store.putAction(coalescible);
      return;
    }

    const updateFence = this.store
      .listActions()
      .filter(
        (action) =>
          action.generation === generation.generation &&
          action.type === "update_parent" &&
          (action.status === "claimed" || action.status === "uncertain"),
      )
      .at(-1);
    const dependency = generation.deliveryRef
      ? (updateFence?.actionId ?? null)
      : parent.actionId;
    this.putNewAction(
      generation,
      "update_parent",
      updatePayload(generation),
      dependency ? "blocked" : "pending",
      nowMs + this.repeatUpdateDelayMs,
      nowMs,
      dependency,
    );
  }

  private upsertRecoveryAction(
    generation: IncidentGeneration,
    nowMs: number,
    updateFence: PendingAction | null,
  ): void {
    const existing = this.recoveryAction(generation.generation);
    const parent = this.store
      .listActions()
      .find(
        (action) =>
          action.generation === generation.generation && action.type === "post_parent",
      );
    if (!parent) throw new IncidentInvariantError("resolving generation is missing parent action");
    const dependency = generation.deliveryRef
      ? (updateFence?.actionId ?? null)
      : parent.actionId;
    if (existing) {
      if (
        existing.status !== "cancelled" &&
        existing.status !== "failed" &&
        existing.status !== "manual_reconciliation_required" &&
        existing.status !== "completed"
      ) {
        throw new IncidentInvariantError("generation already has a live recovery action");
      }
      existing.payload = recoveryPayload(generation);
      existing.status = dependency ? "blocked" : "pending";
      existing.version += 1;
      existing.stateVersion = generation.stateVersion;
      existing.resolutionEpoch = generation.resolutionEpoch;
      existing.attempt = 0;
      existing.claimMode = null;
      existing.startedAtMs = null;
      existing.leaseExpiresAtMs = null;
      existing.nextRunAtMs = nowMs;
      existing.reconcileAtMs = null;
      existing.finalDeadlineAtMs = nowMs + this.actionDeadlineMs;
      existing.dependsOnActionId = dependency;
      existing.lastError = null;
      existing.result = null;
      existing.updatedAtMs = nowMs;
      this.store.putAction(existing);
      return;
    }
    this.putNewAction(
      generation,
      "post_recovery",
      recoveryPayload(generation),
      dependency ? "blocked" : "pending",
      nowMs,
      nowMs,
      dependency,
    );
  }

  private prepareParentUpdatesForResolution(
    generation: IncidentGeneration,
    nowMs: number,
  ): PendingAction | null {
    let fence: PendingAction | null = null;
    for (const action of this.store.listActions()) {
      if (action.generation !== generation.generation || action.type !== "update_parent") {
        continue;
      }
      if (action.status === "pending" || action.status === "blocked") {
        action.status = "cancelled";
        action.version += 1;
        action.nextRunAtMs = null;
        action.dependsOnActionId = null;
        action.lastError = "superseded by incident resolution before claim";
        action.updatedAtMs = nowMs;
        this.store.putAction(action);
      } else if (action.status === "claimed" || action.status === "uncertain") {
        if (fence) {
          throw new IncidentInvariantError(
            "multiple in-flight parent updates violate outbox serialization",
          );
        }
        fence = action;
      }
    }
    return fence;
  }

  private unblockParentDependents(
    generation: IncidentGeneration,
    parentActionId: string,
    nowMs: number,
  ): void {
    for (const dependent of this.store.listActions()) {
      if (
        dependent.status !== "blocked" ||
        dependent.dependsOnActionId !== parentActionId ||
        dependent.generation !== generation.generation
      ) {
        continue;
      }
      dependent.status = "pending";
      dependent.version += 1;
      dependent.stateVersion = generation.stateVersion;
      dependent.dependsOnActionId = null;
      dependent.payload =
        dependent.type === "post_recovery"
          ? recoveryPayload(generation)
          : updatePayload(generation);
      dependent.nextRunAtMs = nowMs;
      dependent.finalDeadlineAtMs = nowMs + this.actionDeadlineMs;
      dependent.updatedAtMs = nowMs;
      this.store.putAction(dependent);
    }
  }

  private queueAnalysis(generation: IncidentGeneration, nowMs: number): void {
    const job = this.store
      .listAnalysisJobs()
      .find((candidate) => candidate.generation === generation.generation);
    if (!job || job.status !== "not_requested") return;
    job.status = "queued";
    job.version += 1;
    job.deadlineAtMs = nowMs + this.analysisDeadlineMs;
    job.updatedAtMs = nowMs;
    this.store.putAnalysisJob(job);
    this.putNewAction(
      generation,
      "dispatch_analysis",
      {
        incident_id: generation.incidentId,
        generation: generation.generation,
        job_id: job.jobId,
      },
      "pending",
      nowMs,
      nowMs,
      null,
    );
  }

  private putAnalysisJob(generation: IncidentGeneration, nowMs: number): void {
    const job: AnalysisJob = {
      jobId: this.newId("job", generation.incidentId),
      incidentId: generation.incidentId,
      generation: generation.generation,
      version: 1,
      status: "not_requested",
      checkoutSha: generation.latestEnvelope.trusted.deployment_sha,
      executionLeaseNonce: null,
      githubRunId: null,
      githubRunAttempt: null,
      leaseExpiresAtMs: null,
      deadlineAtMs: null,
      completion: null,
      lastError: null,
      createdAtMs: nowMs,
      updatedAtMs: nowMs,
    };
    this.store.putAnalysisJob(job);
  }

  private putNewAction(
    generation: IncidentGeneration,
    type: PendingActionType,
    payload: Record<string, unknown>,
    status: "blocked" | "pending",
    nextRunAtMs: number,
    nowMs: number,
    dependsOnActionId: string | null,
  ): PendingAction {
    const action: PendingAction = {
      actionId: this.newId(
        "action",
        `${generation.incidentId}:${type}:${generation.stateVersion}`,
      ),
      incidentId: generation.incidentId,
      generation: generation.generation,
      type,
      payload,
      status,
      version: 1,
      stateVersion: generation.stateVersion,
      resolutionEpoch: generation.resolutionEpoch,
      attempt: 0,
      claimEpoch: 0,
      claimMode: null,
      startedAtMs: null,
      leaseExpiresAtMs: null,
      nextRunAtMs,
      reconcileAtMs: null,
      finalDeadlineAtMs: nowMs + this.actionDeadlineMs,
      dependsOnActionId,
      lastError: null,
      result: null,
      createdAtMs: nowMs,
      updatedAtMs: nowMs,
    };
    this.store.putAction(action);
    return action;
  }

  private recoveryAction(generation: number): PendingAction | null {
    return (
      this.store
        .listActions()
        .filter(
          (action) => action.generation === generation && action.type === "post_recovery",
        )
        .at(-1) ?? null
    );
  }

  private recordReceiptForGeneration(
    generation: IncidentGeneration,
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    receivedAtMs: number,
    action: ReceiptAction,
  ): ApplyEventAcknowledgement {
    return this.recordReceipt(
      envelope,
      bodyDigest,
      receivedAtMs,
      action,
      generation.incidentId,
      generation.generation,
      generation.state,
      generation.occurrenceCount,
      generation.stateVersion,
    );
  }

  private recordReceipt(
    envelope: CanonicalAlertEnvelope,
    bodyDigest: string,
    receivedAtMs: number,
    action: ReceiptAction,
    incidentId: string | null,
    generation: number | null,
    state: IncidentGeneration["state"] | null,
    occurrenceCount: number,
    stateVersion: number | null,
  ): ApplyEventAcknowledgement {
    const acknowledgement: ApplyEventAcknowledgement = {
      accepted: true,
      incident_id: incidentId,
      generation,
      lifecycle_state: state,
      action,
      occurrence_count: occurrenceCount,
      state_version: stateVersion,
    };
    this.store.putReceipt({
      eventId: envelope.event.event_id,
      bodyDigest,
      generation,
      action,
      receivedAtMs,
      acknowledgement,
    });
    return acknowledgement;
  }

  private setLifecycleHighWatermark(order: LifecycleOrder): void {
    const scheduler = this.store.getSchedulerState();
    this.store.putSchedulerState({ ...scheduler, lifecycleHighWatermark: order });
  }

  private nextGenerationNumber(): number {
    const snapshot = this.store.snapshot();
    const generations = snapshot.generations.map((generation) => generation.generation);
    const receiptGenerations = snapshot.receipts.flatMap((receipt) =>
      receipt.generation === null ? [] : [receipt.generation],
    );
    const candidate = snapshot.generations.at(-1)?.nextGenerationCandidate;
    const allocated = [...generations, ...receiptGenerations];
    if (candidate) allocated.push(candidate.generation);
    return (allocated.length === 0 ? 0 : Math.max(...allocated)) + 1;
  }

  private newId(kind: ControlPlaneIdKind, material: string): string {
    const value = this.idFactory(kind, material);
    if (value.length === 0) throw new IncidentInvariantError(`${kind} ID must not be empty`);
    return value;
  }

  desiredAlarmAt(): number | null {
    return computeDesiredAlarmAt(this.store);
  }
}
