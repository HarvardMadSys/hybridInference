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
  supersedes = false,
): FakeAttestation {
  return {
    issuer: "trusted-ci",
    command: {
      action: "activate",
      supersedes,
      deployment: {
        environment: "staging",
        targetEnvironment: "staging",
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
      targetEnvironment: "staging",
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

  it("resolves a platform version ID only inside its fixed role", async () => {
    const registry = new InMemoryDeploymentRegistry(verifier([]));
    await registry.apply(activation({
      service: "status-monitor",
      deploymentId: "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908",
    }));

    expect(
      registry.lookupByDeploymentId({
        environment: "staging",
        service: "status-monitor",
        deploymentId: "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908",
      }),
    ).toMatchObject({
      service: "status-monitor",
      artifactDigest: DIGEST,
      retiredAt: null,
    });
    expect(() =>
      registry.lookupByDeploymentId({
        environment: "staging",
        service: "gateway",
        deploymentId: "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908",
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
          service: "status-monitor",
          deploymentId: "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908",
          artifactDigest: DIGEST,
        },
        retiredAt: 2_000,
      },
    });
    expect(() =>
      registry.lookupByDeploymentId({
        environment: "staging",
        service: "status-monitor",
        deploymentId: "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908",
      }),
    ).toThrowError(expect.objectContaining<Partial<DeploymentLookupError>>({
      code: "retired_deployment",
    }));
  });

  it("rejects an ambiguous reduced-key deployment lookup", async () => {
    const registry = new InMemoryDeploymentRegistry(verifier([]));
    const deploymentId = "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908";
    await registry.apply(activation({
      service: "status-monitor",
      deploymentId,
    }));
    await registry.apply(activation({
      service: "status-monitor",
      deploymentId,
      artifactDigest: `sha256:${"c".repeat(64)}`,
      deploymentSha: "d".repeat(40),
      activatedAt: 1_001,
    }));

    expect(() =>
      registry.lookupByDeploymentId({
        environment: "staging",
        service: "status-monitor",
        deploymentId,
      }),
    ).toThrowError(expect.objectContaining<Partial<DeploymentLookupError>>({
      code: "deployment_mismatch",
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

  describe("superseding activations", () => {
    // Only for a service where exactly one deployment can be serving. It is
    // what makes a long-lived capability safe: retiring the record it is
    // pinned to revokes it on the next event, without waiting out its expiry.
    const OTHER = {
      deploymentId: "deploy-456",
      artifactDigest: `sha256:${"e".repeat(64)}`,
      deploymentSha: "f".repeat(40),
      activatedAt: 2_000,
    };

    it("retires the record the previous deployment left active", async () => {
      const registry = new InMemoryDeploymentRegistry(verifier([]));
      const previous = await registry.apply(activation());

      await registry.apply(activation(OTHER, true));

      expect(() => registry.lookup(previous)).toThrow(
        expect.objectContaining({ code: "retired_deployment" }),
      );
    });

    it("leaves the deployment doing the superseding active", async () => {
      const registry = new InMemoryDeploymentRegistry(verifier([]));
      await registry.apply(activation());

      const current = await registry.apply(activation(OTHER, true));

      expect(registry.lookup(current).retiredAt).toBeNull();
    });

    it("does not supersede unless the activation asks to", async () => {
      const registry = new InMemoryDeploymentRegistry(verifier([]));
      const previous = await registry.apply(activation());

      await registry.apply(activation(OTHER));

      expect(registry.lookup(previous).retiredAt).toBeNull();
    });

    it("does not retire a deployment newer than the one superseding", async () => {
      // Attestations are only checked against a clock-skew window and carry no
      // ordering, so a delayed activation can land after a newer one. Without
      // the bound it would retire the deployment that superseded it and leave
      // the stale one as the sole survivor — killing the live capability.
      const registry = new InMemoryDeploymentRegistry(verifier([]));
      const current = await registry.apply(activation(OTHER, true));

      await registry.apply(activation({ activatedAt: 1_000 }, true));

      expect(registry.lookup(current).retiredAt).toBeNull();
    });

    it("never writes a retirement earlier than the record's own activation", async () => {
      const registry = new InMemoryDeploymentRegistry(verifier([]));
      const later = await registry.apply(activation(OTHER, true));

      await registry.apply(activation({ activatedAt: 1_000 }, true));

      const record = registry.lookup(later);
      expect(record.retiredAt === null || record.retiredAt >= record.activatedAt).toBe(
        true,
      );
    });

    it("applies a superseding retry to a deployment already registered", async () => {
      // The first attempt registered without superseding. Returning early on
      // the retry would leave the earlier deployments active until some later
      // deploy happened to supersede them.
      const registry = new InMemoryDeploymentRegistry(verifier([]));
      const previous = await registry.apply(activation());
      await registry.apply(activation(OTHER));

      await registry.apply(activation(OTHER, true));

      expect(() => registry.lookup(previous)).toThrow(
        expect.objectContaining({ code: "retired_deployment" }),
      );
    });

    it("spends no registry version when nothing was superseded", async () => {
      const registry = new InMemoryDeploymentRegistry(verifier([]));

      const only = await registry.apply(activation({}, true));

      // The version is what a capability is pinned to, so it must move only
      // when a record does.
      expect(only.registryVersion).toBe(1);
      expect(registry.version()).toBe(1);
    });

    it("is idempotent when the same deployment activates twice", async () => {
      const registry = new InMemoryDeploymentRegistry(verifier([]));
      const first = await registry.apply(activation({}, true));

      const again = await registry.apply(activation({}, true));

      expect(again).toEqual(first);
      expect(registry.lookup(first).retiredAt).toBeNull();
    });
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

  it("refuses to re-point an existing record at a different target", async () => {
    const registry = new InMemoryDeploymentRegistry(verifier([]));
    await registry.apply(activation());

    // Accepting this would silently relabel every alert the deployment has
    // already sent and move its incidents to a different Durable Object.
    await expect(
      registry.apply(activation({ targetEnvironment: "production" })),
    ).rejects.toEqual(
      expect.objectContaining<Partial<DeploymentRegistryWriteError>>({
        code: "deployment_conflict",
      }),
    );
    expect(registry.version()).toBe(1);
  });
});
