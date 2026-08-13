import { describe, expect, it, vi } from "vitest";

import {
  handleIngressRequest,
  IngressConflictError,
  type IncidentAcknowledgement,
  type IngressDependencies,
} from "../src/ingress";
import type { CanonicalAlertEnvelope, TrustedAlertMetadata } from "../src/types";

const NOW = Date.parse("2026-07-20T07:00:00Z");

const trusted: TrustedAlertMetadata = {
  environment: "staging",
  target_environment: "staging",
  source: "gateway",
  principal: "staging-gateway",
  deployment_id: "deploy-20260720-1",
  deployment_sha: "0123456789abcdef0123456789abcdef01234567",
  artifact_digest: `sha256:${"a".repeat(64)}`,
  registry_version: 7,
};

function event(): Record<string, unknown> {
  return {
    schema_version: 1,
    event_id: "018f-event-0001",
    alert_type: "provider_circuit_open",
    fingerprint: "provider-circuit:diffusiongemma:local-8002",
    status: "firing",
    severity: "error",
    title: "Provider circuit opened",
    occurred_at: "2026-07-20T06:00:00Z",
    summary: "Local provider refused connections",
    context: {
      provider: "diffusiongemma:local-8002",
      availability: 0,
      reason: "connection_refused",
    },
    evidence_refs: ["apps/backend/routing/routers.py"],
  };
}

function request(body: unknown = event()): Request {
  return new Request("https://alerts.example.test/v1/events", {
    method: "POST",
    headers: {
      authorization: "Bearer test-credential",
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

function dependencies(
  overrides: Partial<IngressDependencies> = {},
): IngressDependencies {
  const acknowledgement: IncidentAcknowledgement = {
    accepted: true,
    incident_id: "inc-1",
    generation: 1,
    lifecycle_state: "opening",
    action: "opened",
    occurrence_count: 1,
    state_version: 1,
  };
  return {
    authenticate: vi.fn().mockResolvedValue(trusted),
    incidentName: vi.fn().mockResolvedValue("opaque-route-name"),
    submit: vi.fn().mockResolvedValue(acknowledgement),
    now: () => NOW,
    ...overrides,
  };
}

describe("handleIngressRequest", () => {
  it("authenticates, canonicalizes, routes, and acknowledges only after persistence", async () => {
    let submitted: CanonicalAlertEnvelope | undefined;
    const submit = vi.fn(
      async (
        routeName: string,
        envelope: CanonicalAlertEnvelope,
        digest: string,
      ): Promise<IncidentAcknowledgement> => {
        expect(routeName).toBe("opaque-route-name");
        expect(digest).toMatch(/^sha256:[a-f0-9]{64}$/);
        submitted = envelope;
        return {
          accepted: true,
          incident_id: "inc-1",
          generation: 1,
          lifecycle_state: "opening",
          action: "opened",
          occurrence_count: 1,
          state_version: 1,
        };
      },
    );

    const response = await handleIngressRequest(request(), dependencies({ submit }));

    expect(response.status).toBe(202);
    await expect(response.json()).resolves.toMatchObject({
      accepted: true,
      incident_id: "inc-1",
    });
    expect(submitted?.trusted).toEqual(trusted);
    expect(submitted?.event).not.toHaveProperty("environment");
  });

  it("rejects before parsing when producer identity is invalid", async () => {
    const submit = vi.fn();
    const response = await handleIngressRequest(
      request("not-an-event"),
      dependencies({ authenticate: vi.fn().mockResolvedValue(null), submit }),
    );

    expect(response.status).toBe(401);
    expect(submit).not.toHaveBeenCalled();
  });

  it("rejects producer-supplied trusted fields", async () => {
    const submit = vi.fn();
    const response = await handleIngressRequest(
      request({ ...event(), environment: "production" }),
      dependencies({ submit }),
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ error: "invalid_event" });
    expect(submit).not.toHaveBeenCalled();
  });

  it("maps a same-shard event id digest conflict to 409", async () => {
    const response = await handleIngressRequest(
      request(),
      dependencies({
        submit: vi.fn().mockRejectedValue(new IngressConflictError()),
      }),
    );

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({ error: "event_id_conflict" });
  });

  it("enforces the configured body bound", async () => {
    const response = await handleIngressRequest(
      request(event()),
      dependencies({ maxBodyBytes: 16 }),
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      error: "invalid_event",
      detail: "request body is too large",
    });
  });

  it("does not expose the endpoint on another path", async () => {
    const response = await handleIngressRequest(
      new Request("https://alerts.example.test/other", { method: "POST" }),
      dependencies(),
    );
    expect(response.status).toBe(404);
  });
});
