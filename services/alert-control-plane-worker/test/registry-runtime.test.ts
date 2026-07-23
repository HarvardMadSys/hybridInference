import { describe, expect, it, vi } from "vitest";

import {
  BoundDeploymentRegistryClient,
  handleDeploymentAttestationRequest,
} from "../src/registry-runtime";
import {
  DeploymentLookupError,
  type TrustedDeploymentMetadata,
} from "../src/registry";
import type { StagingIngressConfig } from "../src/runtime-config";

const NOW = Date.now();
const deployment: TrustedDeploymentMetadata = {
  environment: "staging",
  service: "synthetic-alert-producer",
  deploymentId: "run-123-attempt-1",
  artifactDigest: `sha256:${"a".repeat(64)}`,
  deploymentSha: "b".repeat(40),
  activatedAt: NOW,
  retiredAt: null,
  registryVersion: 1,
};

function namespace(
  fetch: (request: Request) => Promise<Response>,
): DurableObjectNamespace {
  return {
    idFromName: vi.fn().mockReturnValue({ toString: () => "registry-id" }),
    get: vi.fn().mockReturnValue({ fetch: vi.fn(fetch) }),
  } as unknown as DurableObjectNamespace;
}

function config(
  registryNamespace: DurableObjectNamespace,
): StagingIngressConfig {
  return {
    mode: "staging-ingress",
    routeKey: "route-key-material-with-at-least-32-bytes",
    slack: {
      botToken: "xoxb-unit-test-token-123456",
      channelId: "C123",
      sinkId: "slack-staging",
    },
    quota: {
      activeLimit: 10,
      pendingLeaseMs: 120_000,
      namespace: registryNamespace,
    },
    identity: {
      registryNamespace,
      producerTokenSigningKey:
        "producer-signing-key-material-with-at-least-32-bytes",
      producerTokenTtlSeconds: 900,
      githubOidc: {
        audience: "alert-control-plane-deployment-attestation",
        subject: "repo:HarvardMadSys/hybridInference:environment:staging",
        repository: "HarvardMadSys/hybridInference",
        repositoryId: "123",
        repositoryOwnerId: "456",
        workflowRef:
          "HarvardMadSys/hybridInference/.github/workflows/alert-control-plane-staging-lifecycle.yml@refs/heads/dev",
        ref: "refs/heads/dev",
        environment: "staging",
        eventName: "workflow_dispatch",
      },
    },
  };
}

function activateRequest(): Request {
  return new Request(
    "https://alerts.example.test/v1/deployments/attest",
    {
      method: "POST",
      headers: {
        authorization: "Bearer github.oidc.token",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        action: "activate",
        deployment: {
          environment: "staging",
          service: deployment.service,
          deployment_id: deployment.deploymentId,
          artifact_digest: deployment.artifactDigest,
        },
        deployment_sha: deployment.deploymentSha,
        activated_at: deployment.activatedAt,
        source: "gateway",
        principal: "staging-synthetic",
      }),
    },
  );
}

describe("deployment registry runtime", () => {
  it("forwards OIDC to the isolated registry object and returns a short-lived capability", async () => {
    const fetch = vi.fn(async (request: Request) => {
      expect(new URL(request.url).pathname).toBe("/internal/attest");
      expect(request.headers.get("authorization")).toBe(
        "Bearer github.oidc.token",
      );
      await expect(request.json()).resolves.toMatchObject({
        command: {
          action: "activate",
          deployment: {
            environment: "staging",
            service: deployment.service,
            deploymentId: deployment.deploymentId,
          },
        },
      });
      return new Response(JSON.stringify(deployment), {
        headers: { "content-type": "application/json" },
      });
    });
    const registryNamespace = namespace(fetch);

    const response = await handleDeploymentAttestationRequest(
      activateRequest(),
      config(registryNamespace),
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.deployment).toEqual(deployment);
    expect(body.expires_in).toBe(900);
    expect(body.producer_token).toEqual(expect.stringMatching(/^[^.]+\.[^.]+\.[^.]+$/));
    expect(registryNamespace.idFromName).toHaveBeenCalledWith(
      expect.stringMatching(/^[A-Za-z0-9_-]{43}$/),
    );
  });

  it("rejects malformed commands before touching a registry object", async () => {
    const registryNamespace = namespace(vi.fn());
    const response = await handleDeploymentAttestationRequest(
      new Request("https://alerts.example.test/v1/deployments/attest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          action: "activate",
          deployment: { environment: "production" },
        }),
      }),
      config(registryNamespace),
    );

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({
      error: "invalid_attestation",
    });
    expect(registryNamespace.get).not.toHaveBeenCalled();
  });

  it("does not mint broader producer identities from the staging lifecycle", async () => {
    const registryNamespace = namespace(vi.fn());
    const body = await activateRequest().json() as Record<string, unknown>;
    const response = await handleDeploymentAttestationRequest(
      new Request("https://alerts.example.test/v1/deployments/attest", {
        method: "POST",
        headers: {
          authorization: "Bearer github.oidc.token",
          "content-type": "application/json",
        },
        body: JSON.stringify({ ...body, source: "status-monitor" }),
      }),
      config(registryNamespace),
    );

    expect(response.status).toBe(401);
    expect(registryNamespace.get).not.toHaveBeenCalled();
  });

  it("maps stable registry lookup failures without exposing response bodies", async () => {
    const sensitive = "retired token body";
    const registryNamespace = namespace(async () =>
      new Response(
        JSON.stringify({
          error: "retired_deployment",
          detail: sensitive,
        }),
        {
          status: 404,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    const client = new BoundDeploymentRegistryClient(
      config(registryNamespace),
    );

    const error = await client.lookup(deployment).catch((caught) => caught);
    expect(error).toEqual(
      expect.objectContaining<Partial<DeploymentLookupError>>({
        code: "retired_deployment",
      }),
    );
    expect(String(error)).not.toContain(sensitive);
  });
});
