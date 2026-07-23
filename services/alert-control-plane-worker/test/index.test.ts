import { describe, expect, it, vi } from "vitest";

import worker, {
  IncidentDurableObject,
  submitToIncident,
} from "../src/index";
import { IngressConflictError, IngressUnavailableError } from "../src/ingress";
import type { SchedulerState } from "../src/store";
import type { CanonicalAlertEnvelope } from "../src/types";
import { deferred } from "./fakes";

const envelope: CanonicalAlertEnvelope = {
  event: {
    schema_version: 1,
    event_id: "event-1",
    alert_type: "provider_circuit_open",
    fingerprint: "provider-circuit:openai",
    status: "firing",
    severity: "error",
    title: "Provider circuit opened",
    occurred_at: "2026-07-20T06:00:00.000Z",
    summary: "Provider unavailable",
    context: { provider: "openai" },
    evidence_refs: [],
  },
  trusted: {
    environment: "staging",
    source: "gateway",
    principal: "staging-gateway",
    deployment_id: "deployment-1",
    deployment_sha: "a".repeat(40),
    artifact_digest: `sha256:${"b".repeat(64)}`,
    registry_version: 1,
  },
};

function namespace(response: Response): DurableObjectNamespace {
  const stub = { fetch: vi.fn().mockResolvedValue(response) };
  return {
    idFromName: vi.fn().mockReturnValue({ toString: () => "object-id" }),
    get: vi.fn().mockReturnValue(stub),
  } as unknown as DurableObjectNamespace;
}

function configuredEnv(
  overrides: Partial<Parameters<typeof worker.fetch>[1]> = {},
): NonNullable<Parameters<typeof worker.fetch>[1]> {
  const binding = namespace(new Response(null, { status: 500 }));
  return {
    INCIDENTS: binding,
    PRINCIPAL_QUOTAS: binding,
    CONTROL_PLANE_MODE: "staging-runtime",
    ROUTE_KEY_V1: "index-test-route-key-material-32-bytes-minimum",
    SLACK_BOT_TOKEN: "xoxb-unit-test-token-123456",
    SLACK_CHANNEL_ID: "C123",
    SLACK_SINK_ID: "slack-staging",
    PRINCIPAL_ACTIVE_LIMIT: "10",
    QUOTA_PENDING_LEASE_MS: "120000",
    ...overrides,
  };
}

function schedulerState(
  desiredAlarmAtMs: number | null,
  schedulerEpoch: number,
): SchedulerState {
  return {
    desiredAlarmAtMs,
    schedulerEpoch,
    lastRunAtMs: null,
    lastError: null,
    lifecycleHighWatermark: null,
  };
}

describe("Phase 1 runtime", () => {
  it("reports its dormant state without claiming readiness", async () => {
    const response = await worker.fetch(
      new Request("https://alerts.example.test/healthz"),
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      status: "dormant",
      ready: false,
      phase: 1,
      accepts_events: false,
      external_actions_enabled: false,
    });
  });

  it("cannot accept a real producer event in Phase 1", async () => {
    const response = await worker.fetch(
      new Request("https://alerts.example.test/v1/events", { method: "POST" }),
    );
    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({
      error: "control_plane_dormant",
    });
  });

  it("reports a configured C1 runtime without opening public ingress", async () => {
    const env = configuredEnv();
    const health = await worker.fetch(
      new Request("https://alerts.example.test/healthz"),
      env,
    );
    await expect(health.json()).resolves.toEqual({
      status: "staging_runtime_configured",
      ready: false,
      phase: "c1",
      accepts_events: false,
      external_actions_enabled: true,
    });

    const event = await worker.fetch(
      new Request("https://alerts.example.test/v1/events", { method: "POST" }),
      env,
    );
    expect(event.status).toBe(503);
    await expect(event.json()).resolves.toEqual({
      error: "control_plane_dormant",
    });
  });

  it("fails closed with a stable health error and no credential reflection", async () => {
    const token = "xoxb-sensitive-token-value-123456";
    const response = await worker.fetch(
      new Request("https://alerts.example.test/healthz"),
      configuredEnv({
        SLACK_BOT_TOKEN: token,
        SLACK_CHANNEL_ID: "invalid channel",
      }),
    );
    expect(response.status).toBe(503);
    const body = await response.text();
    expect(JSON.parse(body)).toEqual({
      status: "configuration_error",
      ready: false,
      phase: "c1",
      accepts_events: false,
      external_actions_enabled: false,
      configuration_error: "slack_channel_id_invalid",
    });
    expect(body).not.toContain(token);
  });

  it("submits an internal canonical envelope and validates its acknowledgement", async () => {
    const binding = namespace(
      new Response(
        JSON.stringify({
          accepted: true,
          incident_id: "incident-1",
          generation: 1,
          lifecycle_state: "opening",
          action: "opened",
          occurrence_count: 1,
          state_version: 1,
        }),
        { status: 202 },
      ),
    );

    await expect(
      submitToIncident(binding, "opaque", envelope, `sha256:${"c".repeat(64)}`),
    ).resolves.toMatchObject({ incident_id: "incident-1", generation: 1 });
    expect(binding.idFromName).toHaveBeenCalledWith("opaque");
  });

  it("maps incident conflicts and malformed acknowledgements without leaking bodies", async () => {
    await expect(
      submitToIncident(namespace(new Response("conflict", { status: 409 })), "opaque", envelope, "digest"),
    ).rejects.toBeInstanceOf(IngressConflictError);

    await expect(
      submitToIncident(
        namespace(new Response(JSON.stringify({ accepted: true }), { status: 202 })),
        "opaque",
        envelope,
        "digest",
      ),
    ).rejects.toBeInstanceOf(IngressUnavailableError);
  });

  it("does not let a stale alarm read overwrite a newer schedule", async () => {
    const firstAlarmRead = deferred<number | null>();
    let alarm: number | null = null;
    let schedule = schedulerState(100, 1);
    let getAlarmCalls = 0;
    const storage = {
      getAlarm: vi.fn(() => {
        getAlarmCalls += 1;
        return getAlarmCalls === 1
          ? firstAlarmRead.promise
          : Promise.resolve(alarm);
      }),
      setAlarm: vi.fn(async (value: number | Date) => {
        alarm = value instanceof Date ? value.getTime() : value;
      }),
      deleteAlarm: vi.fn(async () => {
        alarm = null;
      }),
    } as unknown as DurableObjectStorage;
    const object = Object.create(
      IncidentDurableObject.prototype,
    ) as IncidentDurableObject;
    Object.assign(object, {
      state: { storage },
      store: { getSchedulerState: () => structuredClone(schedule) },
      outbox: { desiredAlarmAt: () => schedule.desiredAlarmAtMs },
    });
    const rearm = (
      object as unknown as { rearm(): Promise<void> }
    ).rearm.bind(object);

    const staleRearm = rearm();
    expect(storage.getAlarm).toHaveBeenCalledTimes(1);

    schedule = schedulerState(50, 2);
    await rearm();
    expect(alarm).toBe(50);

    firstAlarmRead.resolve(null);
    await staleRearm;

    expect(alarm).toBe(50);
    expect(storage.setAlarm).toHaveBeenCalledTimes(1);
  });
});
