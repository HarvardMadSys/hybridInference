import { describe, expect, it, vi } from "vitest";

import {
  authenticateProducer,
  mintProducerCapability,
  type DeploymentRegistryLookup,
} from "../src/producer-identity";
import {
  DeploymentLookupError,
  type TrustedDeploymentMetadata,
} from "../src/registry";
import type { StagingIngressConfig } from "../src/runtime-config";

const NOW = Date.now();
const deployment: TrustedDeploymentMetadata = {
  environment: "staging",
  targetEnvironment: "staging",
  service: "synthetic-alert-producer",
  deploymentId: "run-123-attempt-1",
  artifactDigest: `sha256:${"a".repeat(64)}`,
  deploymentSha: "b".repeat(40),
  activatedAt: NOW,
  retiredAt: null,
  registryVersion: 7,
};

/**
 * Corrupt a JWT's signature in a way that always changes the decoded bytes.
 *
 * Mutating the final base64url character is not enough: an HS256 signature is
 * 32 bytes, so its last character carries only 4 significant bits plus 2
 * padding bits, and decoders discard those padding bits. Replacing a trailing
 * "w" with "x" therefore decodes to the identical signature and the "tampered"
 * token still verifies. The first signature character carries 6 significant
 * bits, so flipping it always yields a genuinely different signature.
 */
function tamperSignature(token: string): string {
  const [header, payload, signature] = token.split(".");
  const flipped = (signature[0] === "A" ? "B" : "A") + signature.slice(1);
  return `${header}.${payload}.${flipped}`;
}

function namespace(): DurableObjectNamespace {
  return {
    idFromName: vi.fn(),
    get: vi.fn(),
  } as unknown as DurableObjectNamespace;
}

function config(
  overrides: Partial<StagingIngressConfig["identity"]> = {},
): StagingIngressConfig {
  return {
    mode: "staging-ingress",
    routeKey: "a".repeat(32),
    slack: {
      botToken: "xoxb-unit-test-token-123456",
      channelId: "C123",
      sinkId: "slack-staging",
    },
    quota: {
      activeLimit: 10,
      pendingLeaseMs: 120_000,
      namespace: namespace(),
    },
    identity: {
      registryNamespace: namespace(),
      producerTokenSigningKey: "b".repeat(32),
      producerTokenTtlSeconds: 60,
      githubOidc: {
        audience: "alert-control-plane-deployment-attestation",
        subject: "repo:HarvardMadSys/hybridInference:environment:staging",
        repository: "HarvardMadSys/hybridInference",
        repositoryId: "123",
        repositoryOwnerId: "456",
        workflowRef:
          "HarvardMadSys/hybridInference/.github/workflows/alert-control-plane-staging-lifecycle.yml@refs/heads/dev",
        statusMonitorWorkflowRef:
          "HarvardMadSys/hybridInference/.github/workflows/deploy-status-monitor.yml@refs/heads/dev",
        ref: "refs/heads/dev",
        environment: "staging",
        eventName: "workflow_dispatch",
      },
      ...overrides,
    },
  };
}

function registry(
  implementation: DeploymentRegistryLookup["lookup"] = async () => deployment,
): DeploymentRegistryLookup {
  return { lookup: vi.fn(implementation) };
}

describe("producer deployment capability", () => {
  it("injects metadata only after an exact active-registry lookup", async () => {
    const runtime = config();
    const token = await mintProducerCapability(
      runtime,
      {
        deployment,
        source: "gateway",
        principal: "staging-synthetic",
      },
      NOW,
    );
    const deployments = registry();

    await expect(
      authenticateProducer(`Bearer ${token}`, runtime, deployments),
    ).resolves.toEqual({
      environment: "staging",
      target_environment: "staging",
      source: "gateway",
      principal: "staging-synthetic",
      deployment_id: deployment.deploymentId,
      deployment_sha: deployment.deploymentSha,
      artifact_digest: deployment.artifactDigest,
      registry_version: deployment.registryVersion,
    });
    expect(deployments.lookup).toHaveBeenCalledWith({
      environment: "staging",
      service: deployment.service,
      deploymentId: deployment.deploymentId,
      artifactDigest: deployment.artifactDigest,
    });
  });

  it("rejects malformed, tampered, and expired capabilities", async () => {
    const runtime = config();
    const token = await mintProducerCapability(
      runtime,
      {
        deployment,
        source: "gateway",
        principal: "staging-synthetic",
      },
      NOW,
    );
    const expired = await mintProducerCapability(
      runtime,
      {
        deployment,
        source: "gateway",
        principal: "staging-synthetic",
      },
      NOW - 2 * 60 * 60 * 1_000,
    );

    await expect(
      authenticateProducer(null, runtime, registry()),
    ).resolves.toBeNull();
    await expect(
      authenticateProducer(`Bearer ${tamperSignature(token)}`, runtime, registry()),
    ).resolves.toBeNull();
    await expect(
      authenticateProducer(`Bearer ${expired}`, runtime, registry()),
    ).resolves.toBeNull();
  });

  it("rejects retired deployments and registry-version drift", async () => {
    const runtime = config();
    const token = await mintProducerCapability(runtime, {
      deployment,
      source: "gateway",
      principal: "staging-synthetic",
    });

    await expect(
      authenticateProducer(
        `Bearer ${token}`,
        runtime,
        registry(async () => {
          throw new DeploymentLookupError("retired_deployment");
        }),
      ),
    ).resolves.toBeNull();
    await expect(
      authenticateProducer(
        `Bearer ${token}`,
        runtime,
        registry(async () => ({ ...deployment, registryVersion: 8 })),
      ),
    ).resolves.toBeNull();
  });

  it("does not misclassify a registry outage as invalid identity", async () => {
    const runtime = config();
    const token = await mintProducerCapability(runtime, {
      deployment,
      source: "gateway",
      principal: "staging-synthetic",
    });

    await expect(
      authenticateProducer(
        `Bearer ${token}`,
        runtime,
        registry(async () => {
          throw new Error("registry unavailable");
        }),
      ),
    ).rejects.toThrow("registry unavailable");
  });
});
