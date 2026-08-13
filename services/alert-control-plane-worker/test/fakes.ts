import type { CanonicalAlertEnvelope } from "../src/types";
import type {
  ActionClaim,
  ActionExecutionResult,
  ActionExecutor,
} from "../src/outbox";
import type {
  DeliveryRef,
  NotificationAction,
  NotificationActionResult,
  NotificationAttemptMode,
  NotificationPlatform,
  NotificationSink,
} from "../src/notification";
import type { IncidentGeneration, PendingAction } from "../src/store";

export function envelope(
  eventId: string,
  status: "firing" | "resolved",
  occurredAt: string,
): CanonicalAlertEnvelope {
  return {
    event: {
      schema_version: 1,
      event_id: eventId,
      alert_type: "provider_circuit_open",
      fingerprint: "provider-circuit:test:8000",
      status,
      severity: "error",
      title: status === "firing" ? "Provider circuit opened" : "Provider recovered",
      occurred_at: occurredAt,
      summary: status === "firing" ? "Provider is unavailable" : "Provider is healthy",
      context: {
        provider: "test:8000",
        availability: status === "firing" ? 0 : 1,
        reason: status === "firing" ? "connection_refused" : "unknown",
      },
      evidence_refs: ["config/models.yaml"],
    },
    trusted: {
      environment: "staging",
      target_environment: "staging",
      source: "gateway",
      principal: "staging-gateway",
      deployment_id: "deploy-1",
      deployment_sha: "a".repeat(40),
      artifact_digest: `sha256:${"b".repeat(64)}`,
      registry_version: 1,
    },
  };
}

export function deterministicIds(): (
  kind: "incident" | "action" | "job",
  material: string,
) => string {
  let sequence = 0;
  return (kind, material) => `${kind}:${sequence++}:${material}`;
}

export interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

type Script = ActionExecutionResult | Error | Promise<ActionExecutionResult>;

export class ScriptedExecutor implements ActionExecutor {
  readonly claims: ActionClaim[] = [];
  private readonly scripts: Script[] = [];

  enqueue(...scripts: Script[]): void {
    this.scripts.push(...scripts);
  }

  async execute(claim: ActionClaim): Promise<ActionExecutionResult> {
    this.claims.push(structuredClone(claim));
    const script = this.scripts.shift();
    if (!script) throw new Error(`missing script for ${claim.action.type}`);
    if (script instanceof Error) throw script;
    return script;
  }
}

export function pendingAction(
  overrides: Partial<PendingAction> = {},
): PendingAction {
  return {
    actionId: "action-1",
    incidentId: "incident-1",
    generation: 1,
    type: "post_parent",
    payload: {},
    status: "pending",
    version: 1,
    stateVersion: 1,
    resolutionEpoch: 0,
    attempt: 0,
    claimEpoch: 0,
    claimMode: null,
    startedAtMs: null,
    leaseExpiresAtMs: null,
    nextRunAtMs: 100,
    reconcileAtMs: null,
    finalDeadlineAtMs: 10_000,
    dependsOnActionId: null,
    lastError: null,
    result: null,
    createdAtMs: 0,
    updatedAtMs: 0,
    ...overrides,
  };
}

/** Build a valid {@link DeliveryRef} with a threaded default shape. */
export function deliveryRef(overrides: Partial<DeliveryRef> = {}): DeliveryRef {
  return {
    schemaVersion: 1,
    sinkId: "slack-primary",
    platform: "slack",
    destinationId: "C123",
    messageId: "100.001",
    conversationId: "100.001",
    ...overrides,
  };
}

/** Build a complete {@link IncidentGeneration} fixture for executor unit tests. */
export function incidentGeneration(
  overrides: Partial<IncidentGeneration> = {},
): IncidentGeneration {
  const latest = envelope("gen-event", "firing", "2026-07-20T00:00:00.000Z");
  return {
    incidentId: "incident-1",
    generation: 1,
    state: "firing",
    stateVersion: 1,
    resolutionEpoch: 0,
    quotaState: "confirmed",
    quotaLeaseEpoch: 1,
    occurrenceCount: 1,
    firstSeen: latest.event.occurred_at,
    lastSeen: latest.event.occurred_at,
    highWatermark: {
      occurredAt: latest.event.occurred_at,
      occurredAtMs: Date.parse(latest.event.occurred_at),
      statusPrecedence: 0,
      eventId: latest.event.event_id,
    },
    latestEnvelope: latest,
    resolutionEnvelope: null,
    deliveryRef: null,
    nextGenerationCandidate: null,
    createdAtMs: 0,
    updatedAtMs: 0,
    ...overrides,
  };
}

export interface FakeSinkCall {
  readonly action: NotificationAction;
  readonly mode: NotificationAttemptMode;
}

/**
 * A fake sink whose parent messages carry a `conversationId` (thread root),
 * modelling a Slack-like platform. It never throws and records every call.
 */
export class ThreadedFakeSink implements NotificationSink {
  readonly sinkId = "slack-primary";
  readonly platform: NotificationPlatform = "slack";
  readonly calls: FakeSinkCall[] = [];

  async execute(
    action: NotificationAction,
    mode: NotificationAttemptMode,
  ): Promise<NotificationActionResult> {
    this.calls.push({ action, mode });
    if (action.type === "post_parent") {
      const messageId = `ts-${action.actionId}`;
      return {
        outcome: "success",
        receipt: {
          deliveryRef: {
            schemaVersion: 1,
            sinkId: this.sinkId,
            platform: this.platform,
            destinationId: "C123",
            messageId,
            conversationId: messageId,
          },
        },
      };
    }
    const ref = action.deliveryRef;
    if (ref === null) return { outcome: "failed", errorCode: "missing_delivery_ref" };
    return {
      outcome: "success",
      receipt: { deliveryRef: ref, externalEffectId: `reply-${action.actionId}` },
    };
  }
}

/**
 * A fake sink for a thread-less platform: parent messages return a
 * `DeliveryRef` WITHOUT `conversationId`, and replies relate on `messageId`
 * only. It never throws and records every call.
 */
export class ThreadlessFakeSink implements NotificationSink {
  readonly sinkId = "slack-primary";
  readonly platform: NotificationPlatform = "slack";
  readonly calls: FakeSinkCall[] = [];

  async execute(
    action: NotificationAction,
    mode: NotificationAttemptMode,
  ): Promise<NotificationActionResult> {
    this.calls.push({ action, mode });
    if (action.type === "post_parent") {
      return {
        outcome: "success",
        receipt: {
          deliveryRef: {
            schemaVersion: 1,
            sinkId: this.sinkId,
            platform: this.platform,
            destinationId: "C123",
            messageId: `ts-${action.actionId}`,
          },
        },
      };
    }
    const ref = action.deliveryRef;
    if (ref === null) return { outcome: "failed", errorCode: "missing_delivery_ref" };
    return {
      outcome: "success",
      receipt: { deliveryRef: ref, externalEffectId: `reply-${action.actionId}` },
    };
  }
}
