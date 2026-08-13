import { describe, expect, it, vi } from "vitest";
import type { JWTPayload } from "jose";
import { GitHubOidcDeploymentVerifier } from "../src/github-oidc";
import {
  DeploymentRegistryWriteError,
  type VerifiedDeploymentCommand,
} from "../src/registry";
import type { StagingIngressConfig } from "../src/runtime-config";

const NOW = Date.parse("2026-07-23T12:00:00Z");
const oidc: StagingIngressConfig["identity"]["githubOidc"] = {
  audience: "alert-control-plane-deployment-attestation",
  subject: "repo:HarvardMadSys/hybridInference:environment:staging",
  repository: "HarvardMadSys/hybridInference",
  repositoryId: "12345",
  repositoryOwnerId: "67890",
  workflowRef:
    "HarvardMadSys/hybridInference/.github/workflows/alert-control-plane-staging-lifecycle.yml@refs/heads/dev",
  statusMonitorWorkflowRef:
    "HarvardMadSys/hybridInference/.github/workflows/deploy-status-monitor.yml@refs/heads/dev",
  ref: "refs/heads/dev",
  environment: "staging",
  eventName: "workflow_dispatch",
};

function claims(overrides: Record<string, unknown> = {}): JWTPayload {
  return {
    iss: "https://token.actions.githubusercontent.com",
    aud: oidc.audience,
    sub: oidc.subject,
    iat: Math.floor(NOW / 1_000) - 10,
    exp: Math.floor(NOW / 1_000) + 300,
    jti: "oidc-jti-1",
    repository: oidc.repository,
    repository_id: oidc.repositoryId,
    repository_owner_id: oidc.repositoryOwnerId,
    workflow_ref: oidc.workflowRef,
    sha: "a".repeat(40),
    ref: oidc.ref,
    environment: oidc.environment,
    event_name: oidc.eventName,
    actor_id: "111",
    run_id: "222",
    run_attempt: "1",
    runner_environment: "github-hosted",
    ...overrides,
  };
}

function command(
  overrides: Partial<
    Extract<VerifiedDeploymentCommand, { action: "activate" }>["deployment"]
  > = {},
): VerifiedDeploymentCommand {
  return {
    action: "activate",
    deployment: {
      environment: "staging",
      targetEnvironment: "staging",
      service: "synthetic-alert-producer",
      deploymentId: "github-222-1",
      artifactDigest: `sha256:${"b".repeat(64)}`,
      deploymentSha: "a".repeat(40),
      activatedAt: NOW,
      ...overrides,
    },
  };
}

describe("GitHubOidcDeploymentVerifier", () => {
  it("accepts only the reviewed staging workflow identity", async () => {
    const verifyToken = vi.fn().mockResolvedValue(claims());
    const verifier = new GitHubOidcDeploymentVerifier(
      oidc,
      verifyToken,
      () => NOW,
    );

    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: command(),
      }),
    ).resolves.toEqual(command());
    expect(verifyToken).toHaveBeenCalledWith(
      "signed.github.oidc",
      "alert-control-plane-deployment-attestation",
    );
  });

  it("accepts the exact self-hosted status-monitor deploy workflow for a Worker version", async () => {
    const statusCommand = command({
      service: "status-monitor",
      deploymentId: "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908",
    });
    const verifyToken = vi.fn().mockResolvedValue(claims({
      workflow_ref: oidc.statusMonitorWorkflowRef,
      event_name: "push",
      runner_environment: "self-hosted",
    }));
    const verifier = new GitHubOidcDeploymentVerifier(
      oidc,
      verifyToken,
      () => NOW,
    );

    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: statusCommand,
      }),
    ).resolves.toEqual(statusCommand);
  });

  it("does not let another workflow or runner attest a status-monitor version", async () => {
    const statusCommand = command({
      service: "status-monitor",
      deploymentId: "0198a3d0-4c2f-7db4-8c55-1f6bc62ee908",
    });
    for (const override of [
      { workflow_ref: oidc.workflowRef },
      { runner_environment: "github-hosted" },
      { event_name: "pull_request" },
    ]) {
      const verifier = new GitHubOidcDeploymentVerifier(
        oidc,
        vi.fn().mockResolvedValue(claims({
          workflow_ref: oidc.statusMonitorWorkflowRef,
          event_name: "workflow_dispatch",
          runner_environment: "self-hosted",
          ...override,
        })),
        () => NOW,
      );
      await expect(
        verifier.verify({
          authorization: "Bearer signed.github.oidc",
          command: statusCommand,
        }),
      ).rejects.toMatchObject({ code: "invalid_attestation" });
    }
  });

  it.each([
    ["issuer", { iss: "https://issuer.example" }],
    ["audience", { aud: "another-audience" }],
    ["subject", { sub: "repo:attacker/repo:environment:staging" }],
    ["repository", { repository: "attacker/repo" }],
    ["repository id", { repository_id: "999" }],
    ["owner id", { repository_owner_id: "999" }],
    ["workflow", { workflow_ref: "attacker/repo/.github/workflows/x.yml@refs/heads/dev" }],
    ["ref", { ref: "refs/heads/main" }],
    ["environment", { environment: "production" }],
    ["event", { event_name: "pull_request" }],
    ["runner", { runner_environment: "self-hosted" }],
    ["workflow sha", { sha: "not-a-sha" }],
  ])("rejects a wrong %s claim", async (_name, override) => {
    const verifier = new GitHubOidcDeploymentVerifier(
      oidc,
      vi.fn().mockResolvedValue(claims(override)),
      () => NOW,
    );

    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: command(),
      }),
    ).rejects.toEqual(
      expect.objectContaining<Partial<DeploymentRegistryWriteError>>({
        code: "invalid_attestation",
      }),
    );
  });

  it("rejects cross-environment and stale deployment commands", async () => {
    const verifier = new GitHubOidcDeploymentVerifier(
      oidc,
      vi.fn().mockResolvedValue(claims()),
      () => NOW,
    );

    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: command({ environment: "production" }),
      }),
    ).rejects.toMatchObject({ code: "invalid_attestation" });
    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: command({ activatedAt: NOW - 5 * 60 * 1_000 - 1 }),
      }),
    ).rejects.toMatchObject({ code: "invalid_attestation" });
    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: command({ deploymentSha: "f".repeat(40) }),
      }),
    ).rejects.toMatchObject({ code: "invalid_attestation" });
    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: command({ deploymentId: "github-999-1" }),
      }),
    ).rejects.toMatchObject({ code: "invalid_attestation" });
    await expect(
      verifier.verify({
        authorization: "Bearer signed.github.oidc",
        command: command({ service: "status-monitor" }),
      }),
    ).rejects.toMatchObject({ code: "invalid_attestation" });
  });

  it("collapses token and JWKS failures to a stable code", async () => {
    const sensitive = "upstream body contained signed.github.oidc";
    const verifier = new GitHubOidcDeploymentVerifier(
      oidc,
      vi.fn().mockRejectedValue(new Error(sensitive)),
      () => NOW,
    );

    const error = await verifier
      .verify({
        authorization: "Bearer signed.github.oidc",
        command: command(),
      })
      .catch((caught: unknown) => caught);
    expect(error).toMatchObject({ code: "invalid_attestation" });
    expect(String(error)).not.toContain(sensitive);
  });
});
