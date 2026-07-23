import { describe, expect, it } from "vitest";

import {
  DeploymentLookupError,
  DeploymentRegistryWriteError,
  InMemoryDeploymentRegistry,
  type DeploymentAttestationVerifier,
  type VerifiedDeploymentCommand,
} from "../src/registry";

const SHA = "a".repeat(40);
const DIGEST = `sha256:${"b".repeat(64)}`;

interface FakeAttestation {
  issuer: "trusted-ci" | "producer";
  command: VerifiedDeploymentCommand;
}

function verifier(calls: FakeAttestation[]): DeploymentAttestationVerifier<FakeAttestation> {
  return {
    verify(attestation) {
      calls.push(attestation);
      if (attestation.issuer !== "trusted-ci") {
        throw new Error("untrusted attestation identity");
      }
      return attestation.command;
    },
  };
}

function activation(
  overrides: Partial<
    Extract<VerifiedDeploymentCommand, { action: "activate" }>["deployment"]
  > = {},
): FakeAttestation {
  return {
    issuer: "trusted-ci",
    command: {
      action: "activate",
      deployment: {
        environment: "staging",
        service: "gateway",
        deploymentId: "deploy-123",
        artifactDigest: DIGEST,
        deploymentSha: SHA,
        activatedAt: 1_000,
        ...overrides,
      },
    },
  };
}

describe("DeploymentRegistry", () => {
  it("publishes only verifier-produced metadata and resolves an exact active identity", async () => {
    const calls: FakeAttestation[] = [];
    const registry = new InMemoryDeploymentRegistry(verifier(calls));

    const published = await registry.apply(activation());
    const resolved = registry.lookup({
      environment: "staging",
      service: "gateway",
      deploymentId: "deploy-123",
      artifactDigest: DIGEST,
    });

    expect(calls).toHaveLength(1);
    expect(resolved).toEqual({
      environment: "staging",
      service: "gateway",
      deploymentId: "deploy-123",
      artifactDigest: DIGEST,
      deploymentSha: SHA,
      activatedAt: 1_000,
      retiredAt: null,
      registryVersion: 1,
    });
    expect(resolved).toBe(published);
    expect(Object.isFrozen(resolved)).toBe(true);
  });

  it("makes concurrent retry-shaped activations idempotent", async () => {
    const calls: FakeAttestation[] = [];
    const registry = new InMemoryDeploymentRegistry(verifier(calls));

    const writes = await Promise.all(
      Array.from({ length: 25 }, () =>
        Promise.resolve().then(() => registry.apply(activation())),
      ),
    );

    expect(new Set(writes.map((record) => record.registryVersion))).toEqual(
      new Set([1]),
    );
    expect(registry.version()).toBe(1);
    expect(calls).toHaveLength(25);
  });

  it("rejects producer attestations before a registry write", async () => {
    const calls: FakeAttestation[] = [];
    const registry = new InMemoryDeploymentRegistry(verifier(calls));
    const forged = activation();
    forged.issuer = "producer";

    await expect(registry.apply(forged)).rejects.toThrow(
      "untrusted attestation identity",
    );
    expect(registry.version()).toBe(0);
    expect(calls).toEqual([forged]);
  });

  it("distinguishes unknown, retired, and mismatched runtime identities", async () => {
    const registry = new InMemoryDeploymentRegistry(verifier([]));
    await registry.apply(activation());

    expect(() =>
      registry.lookup({
        environment: "staging",
        service: "gateway",
        deploymentId: "never-published",
        artifactDigest: DIGEST,
      }),
    ).toThrowError(expect.objectContaining<Partial<DeploymentLookupError>>({
      code: "unknown_deployment",
    }));

    expect(() =>
      registry.lookup({
        environment: "production",
        service: "gateway",
        deploymentId: "deploy-123",
        artifactDigest: DIGEST,
      }),
    ).toThrowError(expect.objectContaining<Partial<DeploymentLookupError>>({
      code: "deployment_mismatch",
    }));

    await registry.apply({
      issuer: "trusted-ci",
      command: {
        action: "retire",
        deployment: {
          environment: "staging",
          service: "gateway",
          deploymentId: "deploy-123",
          artifactDigest: DIGEST,
        },
        retiredAt: 2_000,
      },
    });

    expect(() =>
      registry.lookup({
        environment: "staging",
        service: "gateway",
        deploymentId: "deploy-123",
        artifactDigest: DIGEST,
      }),
    ).toThrowError(expect.objectContaining<Partial<DeploymentLookupError>>({
      code: "retired_deployment",
    }));
  });

  it("retires idempotently and never reactivates the retired identity", async () => {
    const registry = new InMemoryDeploymentRegistry(verifier([]));
    await registry.apply(activation());
    const retirement: FakeAttestation = {
      issuer: "trusted-ci",
      command: {
        action: "retire",
        deployment: {
          environment: "staging",
          service: "gateway",
          deploymentId: "deploy-123",
          artifactDigest: DIGEST,
        },
        retiredAt: 2_000,
      },
    };

    expect((await registry.apply(retirement)).registryVersion).toBe(2);
    expect((await registry.apply(retirement)).registryVersion).toBe(2);
    expect(registry.version()).toBe(2);
    await expect(registry.apply(activation())).rejects.toEqual(
      expect.objectContaining<Partial<DeploymentRegistryWriteError>>({
        code: "deployment_retired",
      }),
    );
  });

  it("keeps rolling deployment records isolated by the full composite key", async () => {
    const registry = new InMemoryDeploymentRegistry(verifier([]));
    await registry.apply(activation());
    const otherDigest = `sha256:${"c".repeat(64)}`;
    const otherSha = "d".repeat(40);

    const second = await registry.apply(
      activation({
        artifactDigest: otherDigest,
        deploymentSha: otherSha,
        activatedAt: 1_001,
      }),
    );
    expect(second.registryVersion).toBe(2);
    expect(
      registry.lookup({
        environment: "staging",
        service: "gateway",
        deploymentId: "deploy-123",
        artifactDigest: otherDigest,
      }).deploymentSha,
    ).toBe(otherSha);
    expect(
      registry.lookup({
        environment: "staging",
        service: "gateway",
        deploymentId: "deploy-123",
        artifactDigest: DIGEST,
      }).deploymentSha,
    ).toBe(SHA);
  });

  it("does not rewrite an existing composite key with conflicting history", async () => {
    const registry = new InMemoryDeploymentRegistry(verifier([]));
    await registry.apply(activation());

    await expect(
      registry.apply(activation({ deploymentSha: "c".repeat(40) })),
    ).rejects.toEqual(
      expect.objectContaining<Partial<DeploymentRegistryWriteError>>({
        code: "deployment_conflict",
      }),
    );
    expect(registry.version()).toBe(1);
  });
});
