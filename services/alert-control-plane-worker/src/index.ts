import { createRuntimeActionExecutor } from "./action-executor";
import { EventIdConflictError, IncidentStateMachine } from "./incident";
import {
  type IncidentAcknowledgement,
  IngressConflictError,
  IngressUnavailableError,
  jsonResponse,
} from "./ingress";
import {
  type ActionClaim,
  type ActionExecutionResult,
  type ActionExecutor,
  OutboxRunner,
} from "./outbox";
import { PrincipalQuotaDurableObject } from "./quota-runtime";
import {
  parseRuntimeConfig,
  type RuntimeEnvironment,
} from "./runtime-config";
import { DurableObjectSqlStore, type SchedulerState } from "./store";
import type { CanonicalAlertEnvelope } from "./types";
import {
  canonicalEventDigest,
  createCanonicalEnvelope,
  ValidationError,
} from "./validation";

const MAX_INTERNAL_BODY_BYTES = 256 * 1024;

type AlarmScheduleSnapshot = Pick<
  SchedulerState,
  "desiredAlarmAtMs" | "schedulerEpoch"
>;

export interface ControlPlaneEnv extends RuntimeEnvironment {
  readonly INCIDENTS: DurableObjectNamespace;
}

interface InternalEventRequest {
  readonly envelope: CanonicalAlertEnvelope;
  readonly body_digest: string;
}

class DormantActionExecutor implements ActionExecutor {
  async execute(_claim: ActionClaim): Promise<ActionExecutionResult> {
    return {
      outcome: "manual_reconciliation_required",
      error: "phase1_external_actions_disabled",
    };
  }
}

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ValidationError("internal event must be an object");
  }
  return value as Record<string, unknown>;
}

async function internalEventBody(request: Request): Promise<InternalEventRequest> {
  const contentType = request.headers.get("content-type")?.split(";", 1)[0]?.trim();
  if (contentType !== "application/json") {
    throw new ValidationError("content-type must be application/json");
  }
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_INTERNAL_BODY_BYTES) {
    throw new ValidationError("internal event is too large");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    throw new ValidationError("internal event must be valid JSON");
  }
  const input = record(parsed);
  if (
    Object.keys(input).some(
      (key) => key !== "envelope" && key !== "body_digest",
    )
  ) {
    throw new ValidationError("internal event contains an unsupported field");
  }
  const envelopeInput = record(input.envelope);
  const envelope = createCanonicalEnvelope(
    envelopeInput.event,
    envelopeInput.trusted,
  );
  if (typeof input.body_digest !== "string") {
    throw new ValidationError("body_digest must be a string");
  }
  return { envelope, body_digest: input.body_digest };
}

function acknowledgement(value: unknown): IncidentAcknowledgement {
  const input = record(value);
  if (
    input.accepted !== true ||
    (input.incident_id !== null && typeof input.incident_id !== "string") ||
    (input.generation !== null && typeof input.generation !== "number") ||
    (input.lifecycle_state !== null && typeof input.lifecycle_state !== "string") ||
    typeof input.action !== "string" ||
    typeof input.occurrence_count !== "number" ||
    (input.state_version !== null && typeof input.state_version !== "number")
  ) {
    throw new IngressUnavailableError("invalid_incident_acknowledgement");
  }
  return input as unknown as IncidentAcknowledgement;
}

/** Per-incident state machine and outbox runtime. */
export class IncidentDurableObject {
  private readonly store: DurableObjectSqlStore;
  private readonly machine: IncidentStateMachine;
  private readonly outbox: OutboxRunner;
  private readonly ready: Promise<void>;

  constructor(
    private readonly state: DurableObjectState,
    env: ControlPlaneEnv,
  ) {
    this.store = new DurableObjectSqlStore(state.storage);
    this.machine = new IncidentStateMachine(this.store);
    const config = parseRuntimeConfig(env);
    const executor =
      config.mode === "staging-runtime"
        ? createRuntimeActionExecutor(this.store, config)
        : new DormantActionExecutor();
    this.outbox = new OutboxRunner(this.store, executor, {
      hooks: this.machine,
    });
    this.ready = state.blockConcurrencyWhile(async () => {
      this.store.initializeSchema();
      await this.rearm();
    });
  }

  async fetch(request: Request): Promise<Response> {
    await this.ready;
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/internal/events") {
      return jsonResponse({ error: "not_found" }, 404);
    }

    try {
      const body = await internalEventBody(request);
      const expectedDigest = await canonicalEventDigest(body.envelope.event);
      if (body.body_digest !== expectedDigest) {
        throw new ValidationError("body_digest does not match the canonical event");
      }
      const result = this.machine.applyEvent(body.envelope, expectedDigest, Date.now());
      await this.rearm();
      return jsonResponse(result, 202);
    } catch (error) {
      if (error instanceof EventIdConflictError) {
        return jsonResponse({ error: error.code }, 409);
      }
      if (error instanceof ValidationError || error instanceof TypeError) {
        return jsonResponse({ error: "invalid_internal_event" }, 400);
      }
      console.error("incident durable object request failed");
      return jsonResponse({ error: "internal_error" }, 500);
    }
  }

  async alarm(): Promise<void> {
    await this.ready;
    const desired = this.outbox.desiredAlarmAt();
    if (desired === null) return;

    // Alarms are at-least-once and only one can be scheduled per object. Ensure
    // a safety alarm before claiming anything so interruption cannot strand work.
    await this.rearm();
    try {
      await this.outbox.runOne(Date.now());
    } finally {
      await this.rearm();
    }
  }

  private async rearm(): Promise<void> {
    this.outbox.desiredAlarmAt();
    while (true) {
      const snapshot = this.alarmScheduleSnapshot();
      const current = await this.state.storage.getAlarm();
      // A newer scheduler epoch invalidates both reads and writes from this
      // snapshot. Retry until the persisted alarm and scheduler state converge.
      if (!this.alarmScheduleIsCurrent(snapshot)) continue;

      if (snapshot.desiredAlarmAtMs === null) {
        if (current !== null) await this.state.storage.deleteAlarm();
      } else if (current !== snapshot.desiredAlarmAtMs) {
        await this.state.storage.setAlarm(snapshot.desiredAlarmAtMs);
      }

      if (this.alarmScheduleIsCurrent(snapshot)) return;
    }
  }

  private alarmScheduleSnapshot(): AlarmScheduleSnapshot {
    const { desiredAlarmAtMs, schedulerEpoch } = this.store.getSchedulerState();
    return { desiredAlarmAtMs, schedulerEpoch };
  }

  private alarmScheduleIsCurrent(snapshot: AlarmScheduleSnapshot): boolean {
    const current = this.alarmScheduleSnapshot();
    return (
      current.schedulerEpoch === snapshot.schedulerEpoch &&
      current.desiredAlarmAtMs === snapshot.desiredAlarmAtMs
    );
  }
}

export { PrincipalQuotaDurableObject };

/** Submit an already authenticated envelope to its opaque incident object. */
export async function submitToIncident(
  namespace: DurableObjectNamespace,
  incidentName: string,
  envelope: CanonicalAlertEnvelope,
  bodyDigest: string,
): Promise<IncidentAcknowledgement> {
  const objectId = namespace.idFromName(incidentName);
  const response = await namespace.get(objectId).fetch(
    new Request("https://incident.internal/internal/events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ envelope, body_digest: bodyDigest }),
    }),
  );
  if (response.status === 409) throw new IngressConflictError();
  if (!response.ok) throw new IngressUnavailableError();
  return acknowledgement(await response.json());
}

const worker = {
  async fetch(
    request: Request,
    env?: ControlPlaneEnv,
  ): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/healthz") {
      const config = parseRuntimeConfig(env);
      if (config.mode === "invalid") {
        return jsonResponse({
          status: "configuration_error",
          ready: false,
          phase: "c1",
          accepts_events: false,
          external_actions_enabled: false,
          configuration_error: config.errorCode,
        }, 503);
      }
      if (config.mode === "staging-runtime") {
        return jsonResponse({
          status: "staging_runtime_configured",
          ready: false,
          phase: "c1",
          accepts_events: false,
          external_actions_enabled: true,
        });
      }
      return jsonResponse({
        status: "dormant",
        ready: false,
        phase: 1,
        accepts_events: false,
        external_actions_enabled: false,
      });
    }
    if (request.method === "POST" && url.pathname === "/v1/events") {
      return jsonResponse({ error: "control_plane_dormant" }, 503);
    }
    return jsonResponse({ error: "not_found" }, 404);
  },
} satisfies ExportedHandler<ControlPlaneEnv>;

export default worker;
