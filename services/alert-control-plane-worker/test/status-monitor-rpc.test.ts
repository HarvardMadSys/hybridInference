import { describe, expect, it, vi } from "vitest";

import type { IncidentAcknowledgement } from "../src/ingress";
import type { TrustedDeploymentMetadata } from "../src/registry";
import {
  submitStatusMonitorRpcEvent,
  type StatusMonitorRpcEnvironment,
} from "../src/status-monitor-rpc";

const VERSION_ID = "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908";
const deployment: TrustedDeploymentMetadata = {
  environment: "staging",
  service: "status-monitor",
  deploymentId: VERSION_ID,
  artifactDigest: `sha256:${"b".repeat(64)}`,
  deploymentSha: "a".repeat(40),
  activatedAt: Date.parse("2026-07-24T01:00:00Z"),
  retiredAt: null,
  registryVersion: 7,
};
const acknowledgement: IncidentAcknowledgement = {
  accepted: true,
  incident_id: "incident-1",
  generation: 1,
  lifecycle_state: "opening",
  action: "opened",
  occurrence_count: 1,
  state_version: 1,
};

function namespace(
  fetch: (request: Request) => Promise<Response>,
): DurableObjectNamespace {
  return {
    idFromName: vi.fn().mockReturnValue({ toString: () => "object-id" }),
    get: vi.fn().mockReturnValue({ fetch: vi.fn(fetch) }),
  } as unknown as DurableObjectNamespace;
}

function env(
  registry = namespace(async () =>
    new Response(JSON.stringify(deployment), {
      headers: { "content-type": "application/json" },
    }),
  ),
): StatusMonitorRpcEnvironment {
  return {
    CONTROL_PLANE_MODE: "staging-ingress",
    ROUTE_KEY_V1: "status-monitor-rpc-route-key-material-32-bytes",
    SLACK_BOT_TOKEN: "xoxb-unit-test-token-123456",
    SLACK_CHANNEL_ID: "C123",
    SLACK_SINK_ID: "slack-staging",
    PRINCIPAL_ACTIVE_LIMIT: "10",
    QUOTA_PENDING_LEASE_MS: "120000",
    PRINCIPAL_QUOTAS: namespace(vi.fn()),
    DEPLOYMENT_REGISTRIES: registry,
    INCIDENTS: namespace(vi.fn()),
    PRODUCER_TOKEN_SIGNING_KEY_V1:
      "producer-signing-key-material-with-at-least-32-bytes",
    PRODUCER_TOKEN_TTL_SECONDS: "900",
    GITHUB_OIDC_SUBJECT:
      "repo:HarvardMadSys/hybridInference:environment:staging",
    GITHUB_OIDC_REPOSITORY: "HarvardMadSys/hybridInference",
    GITHUB_OIDC_REPOSITORY_ID: "123",
    GITHUB_OIDC_REPOSITORY_OWNER_ID: "456",
    GITHUB_OIDC_WORKFLOW_REF:
      "HarvardMadSys/hybridInference/.github/workflows/alert-control-plane-staging-lifecycle.yml@refs/heads/dev",
    GITHUB_OIDC_STATUS_MONITOR_WORKFLOW_REF:
      "HarvardMadSys/hybridInference/.github/workflows/deploy-status-monitor.yml@refs/heads/dev",
    GITHUB_OIDC_REF: "refs/heads/dev",
    GITHUB_OIDC_ENVIRONMENT: "staging",
    GITHUB_OIDC_EVENT_NAME: "workflow_dispatch",
  };
}

function body(): string {
  return JSON.stringify({
    schema_version: 1,
    event_id: "status-monitor-event-1",
    alert_type: "model_unavailable",
    fingerprint: "status-monitor:model:deepseek-v3",
    status: "firing",
    severity: "error",
    title: "Model unavailable: deepseek-v3",
    occurred_at: new Date().toISOString(),
    summary: "deepseek-v3 failed 2 consecutive synthetic probes.",
    context: {
      model_id: "deepseek-v3",
      consecutive_failures: 2,
      failure_threshold: 2,
      reason: "timeout",
    },
    evidence_refs: [],
  });
}

describe("status-monitor role RPC", () => {
  it("injects only registry-backed identity and reaches incident submission", async () => {
    const registry = namespace(async (request) => {
      expect(new URL(request.url).pathname).toBe(
        "/internal/lookup-by-deployment-id",
      );
      await expect(request.json()).resolves.toEqual({
        identity: {
          environment: "staging",
          service: "status-monitor",
          deploymentId: VERSION_ID,
        },
      });
      return new Response(JSON.stringify(deployment), {
        headers: { "content-type": "application/json" },
      });
    });
    const submit = vi.fn().mockResolvedValue(acknowledgement);

    const result = await submitStatusMonitorRpcEvent(
      env(registry),
      body(),
      VERSION_ID,
      submit,
    );

    expect(result).toEqual({ accepted: true, acknowledgement });
    expect(submit).toHaveBeenCalledOnce();
    const [, incidentName, envelope, digest] = submit.mock.calls[0]!;
    expect(incidentName).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(digest).toMatch(/^sha256:[a-f0-9]{64}$/);
    expect(envelope.trusted).toEqual({
      environment: "staging",
      source: "status-monitor",
      principal: "staging-monitor",
      deployment_id: VERSION_ID,
      deployment_sha: deployment.deploymentSha,
      artifact_digest: deployment.artifactDigest,
      registry_version: 7,
    });
  });

  it.each(["version id with spaces", "opaque-but-not-a-worker-version"])(
    "rejects invalid identity %s before registry or incident access",
    async (invalidVersionId) => {
      const registryFetch = vi.fn();
      const submit = vi.fn();
      const result = await submitStatusMonitorRpcEvent(
        env(namespace(registryFetch)),
        body(),
        invalidVersionId,
        submit,
      );

      expect(result).toEqual({
        accepted: false,
        errorCode: "invalid_producer_identity",
      });
      expect(registryFetch).not.toHaveBeenCalled();
      expect(submit).not.toHaveBeenCalled();
    },
  );

  it("fails closed for a retired deployment and never submits", async () => {
    const registry = namespace(async () =>
      new Response(
        JSON.stringify({
          error: "retired_deployment",
          detail: "sensitive registry response",
        }),
        { status: 404, headers: { "content-type": "application/json" } },
      ),
    );
    const submit = vi.fn();

    await expect(
      submitStatusMonitorRpcEvent(env(registry), body(), VERSION_ID, submit),
    ).resolves.toEqual({
      accepted: false,
      errorCode: "retired_deployment",
    });
    expect(submit).not.toHaveBeenCalled();
  });

  it("does not trust mismatched metadata returned across the binding boundary", async () => {
    const registry = namespace(async () =>
      new Response(JSON.stringify({
        ...deployment,
        service: "synthetic-alert-producer",
      }), {
        headers: { "content-type": "application/json" },
      }),
    );
    const submit = vi.fn();

    await expect(
      submitStatusMonitorRpcEvent(env(registry), body(), VERSION_ID, submit),
    ).resolves.toEqual({
      accepted: false,
      errorCode: "deployment_mismatch",
    });
    expect(submit).not.toHaveBeenCalled();
  });

  it("stays dormant when the complete staging identity runtime is absent", async () => {
    const inactive = env() as StatusMonitorRpcEnvironment & {
      CONTROL_PLANE_MODE?: string;
    };
    delete inactive.CONTROL_PLANE_MODE;
    const submit = vi.fn();

    await expect(
      submitStatusMonitorRpcEvent(inactive, body(), VERSION_ID, submit),
    ).resolves.toEqual({
      accepted: false,
      errorCode: "control_plane_dormant",
    });
    expect(submit).not.toHaveBeenCalled();
  });

  it("accepts the role's monitoring_cycle_failure type", async () => {
    const submit = vi.fn().mockResolvedValue(acknowledgement);
    const cycleBody = JSON.stringify({
      schema_version: 1,
      event_id: "status-monitor-event-cycle-1",
      alert_type: "monitoring_cycle_failure",
      fingerprint: "status-monitor:cycle",
      status: "firing",
      severity: "critical",
      title: "Monitoring cycle failing",
      occurred_at: new Date().toISOString(),
      summary: "The monitoring cycle failed; no models could be probed.",
      context: { reason: "discovery_failed" },
      evidence_refs: [],
    });

    await expect(
      submitStatusMonitorRpcEvent(env(), cycleBody, VERSION_ID, submit),
    ).resolves.toEqual({ accepted: true, acknowledgement });
    expect(submit).toHaveBeenCalledOnce();
  });

  // The shared producer validator accepts provider_circuit_open too, and routing
  // keys on fingerprint rather than alert type — so without a role-specific guard
  // this body would drive the very same incident object as the model alerts and
  // carry free-text fields the model contract deliberately excludes.
  it("refuses an otherwise valid alert type this role may not submit", async () => {
    const submit = vi.fn();
    const providerCircuitBody = JSON.stringify({
      schema_version: 1,
      event_id: "status-monitor-event-provider-circuit",
      alert_type: "provider_circuit_open",
      fingerprint: "status-monitor:model:deepseek-v3",
      status: "firing",
      severity: "error",
      title: "Provider circuit opened",
      occurred_at: new Date().toISOString(),
      summary: "zhipu circuit opened after repeated upstream failures.",
      context: { provider: "zhipu", reason: "upstream_error" },
      evidence_refs: [],
    });

    await expect(
      submitStatusMonitorRpcEvent(env(), providerCircuitBody, VERSION_ID, submit),
    ).resolves.toEqual({
      accepted: false,
      errorCode: "invalid_event",
    });
    expect(submit).not.toHaveBeenCalled();
  });
});
