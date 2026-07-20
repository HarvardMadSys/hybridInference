import type { CanonicalAlertEnvelope } from "../src/types";
import type {
  ActionClaim,
  ActionExecutionResult,
  ActionExecutor,
} from "../src/outbox";
import type { PendingAction } from "../src/store";

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
